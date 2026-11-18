"""Orchestrator: one assistant turn, end to end.

    user text -> limits (rate, budget) -> planner (Claude, or offline fallback)
              -> ToolGateway (authz / risk / audit) -> answer
              -> guards (grounding, action claims, citations; one correction round)
              -> metrics + audit + session persistence

Resilience: if Claude is unavailable, over budget, or the circuit breaker is
open, the turn degrades to the offline planner, so HR self-service keeps working.
"""
from __future__ import annotations

import json
import logging
import time

from ..container import Container
from ..limits import RateLimitExceeded
from ..security import Principal
from . import guards
from .llm_common import LLMUnavailable
from .offline_agent import OfflineAgent

log = logging.getLogger("hrcopilot.orchestrator")

CATEGORY_PRIORITY = ["absence_analytics", "team_conflict_analysis", "routing_diagnosis", "leave_planning",
                     "policy_question", "profile_lookup"]
MAX_EVIDENCE_ITEMS = 60


def make_agent(settings, tool_defs):
    """Pick the LLM planner for the configured provider (imports are lazy: only the chosen SDK loads)."""
    if settings.llm_provider == "gemini":
        from .gemini_agent import GeminiAgent
        return GeminiAgent(settings, tool_defs)
    from .claude_agent import ClaudeAgent
    return ClaudeAgent(settings, tool_defs)


class Orchestrator:
    def __init__(self, c: Container, llm=None) -> None:
        self.c = c
        self.offline = OfflineAgent(c.settings.today)
        self.llm = llm or (make_agent(c.settings, c.gateway.tool_definitions()) if c.settings.use_llm else None)

    @property
    def mode(self) -> str:
        return self.llm.name if self.llm else "offline"

    def reset_session(self, principal: Principal, session_id: str) -> None:
        self.c.sessions.delete(f"{principal.id}:{session_id}")

    def chat(self, principal: Principal, session_id: str, text: str) -> dict:
        t0 = time.perf_counter()
        self.c.limits.check_rate(principal.id)  # raises RateLimitExceeded
        sid = f"{principal.id}:{session_id}"     # session bound to the authenticated identity
        session = self.c.sessions.load(sid, principal.id)
        trace: list[dict] = []
        evidence: list = session["state"].get("evidence", [])

        def call_tool(name: str, args: dict) -> dict:
            started = time.perf_counter()
            out = self.c.gateway.call(principal, name, args)
            payload = out.get("data") or out.get("preview") or out.get("result") or out.get("error") or out.get("reason")
            evidence.append(payload)
            trace.append({"tool": name, "args": args, "status": out.get("status"),
                          "ms": round((time.perf_counter() - started) * 1000),
                          "risk": self.c.gateway.specs[name].risk if name in self.c.gateway.specs else None,
                          "result_preview": json.dumps(payload, default=str)[:1500]})
            return out

        mode, notice = self.mode, None
        usage = {"input_tokens": 0, "output_tokens": 0}
        use_llm = self.llm is not None
        if use_llm and not self.c.limits.llm_allowed():
            use_llm, mode = False, "offline (budget)"
            notice = "Daily AI budget reached; answered by the rule-based planner."
        elif use_llm and session["tokens_used"] >= self.c.settings.session_token_budget:
            use_llm, mode = False, "offline (budget)"
            notice = "This conversation reached its AI token budget; answered by the rule-based planner. Start a new chat."

        context_evidence = [text, principal.name, principal.id, self.c.settings.today.isoformat(),
                            self.c.settings.today.year]
        corrected = False
        if use_llm:
            try:
                reply, usage, history = self.llm.run(principal, session["history"], text, call_tool)
                mode = f"{self.llm.name} ({self.llm.model})"
                session["history"] = history
            except LLMUnavailable as exc:
                log.warning("llm_unavailable", extra={"reason": str(exc)})
                mode, notice = "offline (degraded)", f"AI model unavailable ({exc}); answered by the rule-based fallback."
                reply = self.offline.run(principal, session["state"], text, call_tool)
            else:
                report = guards.check(reply, context_evidence + evidence, self.c.employee_names,
                                      sum(t["status"] == "executed" for t in trace), self.c.kb)
                if not report.ok:   # one automatic correction round; if it fails, keep the first answer
                    try:
                        reply2, usage2, history2 = self.llm.run(principal, history, guards.correction_prompt(report),
                                                                call_tool)
                        usage = {k: usage[k] + usage2[k] for k in usage}
                        reply, corrected, session["history"] = reply2, True, history2
                    except LLMUnavailable as exc:
                        log.warning("correction_round_failed", extra={"reason": str(exc)})
        else:
            reply = self.offline.run(principal, session["state"], text, call_tool)

        executed = sum(t["status"] == "executed" for t in trace)
        report = guards.check(reply, context_evidence + evidence, self.c.employee_names, executed, self.c.kb)
        if not report.ok:
            notes = []
            if report.unsupported:
                notes.append("could not verify " + ", ".join(report.unsupported[:8]) + " against system data")
            if report.false_action_claims:
                notes.append("an action described as done was not executed - check the Actions tab")
            if report.invalid_citations:
                notes.append("policy reference(s) " + ", ".join(report.invalid_citations) + " do not exist")
            reply += "\n\n> ⚠️ *Automatic check: " + "; ".join(notes) + ". Treat these parts with caution.*"

        tokens = usage["input_tokens"] + usage["output_tokens"]
        self.c.limits.add_tokens(tokens)
        session["state"]["evidence"] = evidence[-MAX_EVIDENCE_ITEMS:]
        self.c.sessions.save(sid, principal.id, session["history"], session["state"], session["tokens_used"] + tokens)

        cats = {self.c.gateway.category(t["tool"]) for t in trace}
        category = next((c for c in CATEGORY_PRIORITY if c in cats), "other")
        statuses = {t["status"] for t in trace}
        outcome = ("action_proposed" if "proposed" in statuses else "action_executed" if "executed" in statuses
                   else "denied" if statuses == {"denied"} else "answered")
        latency = round((time.perf_counter() - t0) * 1000)
        self.c.metrics.record_turn(principal.id, mode, category, outcome, trace, usage, latency,
                                   len(report.invalid_citations), len(report.unsupported),
                                   len(report.false_action_claims))
        self.c.audit.record(principal.id, "assistant.turn", mode=mode, category=category, outcome=outcome,
                            tools=[t["tool"] for t in trace], guard=report.as_dict(), self_corrected=corrected)
        log.info("turn", extra={"user": principal.id, "role": principal.role, "mode": mode, "category": category,
                                "outcome": outcome, "tools": len(trace), "latency_ms": latency, "tokens": tokens})
        return {"reply": reply, "mode": mode, "notice": notice, "trace": trace, "category": category,
                "outcome": outcome, "guard": report.as_dict(), "self_corrected": corrected,
                "proposals_created": sum(t["status"] == "proposed" for t in trace), "usage": usage,
                "latency_ms": latency}


__all__ = ["Orchestrator", "RateLimitExceeded"]
