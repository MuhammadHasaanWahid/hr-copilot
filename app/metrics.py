"""Business-value and safety telemetry (persisted in SQLite).

Per assistant turn: business category, outcome, tools, tokens, cost, latency
and guard results. `snapshot()` aggregates it for the Value dashboard and
`prometheus()` exposes it for monitoring. Time saved is an *estimate* from the
explicit assumptions in config.MANUAL_EFFORT_MINUTES.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import MANUAL_EFFORT_MINUTES, Settings
from .db import Database


class Metrics:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db, self.settings = db, settings

    def record_turn(self, user_id: str, mode: str, category: str, outcome: str, tools: list[dict], usage: dict,
                    latency_ms: int, invalid_citations: int, unsupported_claims: int, false_action_claims: int) -> None:
        self.db.execute(
            "INSERT INTO app_turns (ts, user_id, mode, category, outcome, tools, denials, input_tokens, output_tokens,"
            " latency_ms, invalid_citations, unsupported_claims, false_action_claims) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), user_id, mode, category, outcome,
             json.dumps([t["tool"] for t in tools]), sum(t["status"] == "denied" for t in tools),
             usage.get("input_tokens", 0), usage.get("output_tokens", 0), latency_ms, invalid_citations,
             unsupported_claims, false_action_claims))

    def _cost(self, inp: int, out: int) -> float:
        return (inp * self.settings.price_input_per_mtok + out * self.settings.price_output_per_mtok) / 1e6

    def snapshot(self) -> dict:
        agg = self.db.one(
            "SELECT COUNT(*) AS turns, COALESCE(SUM(denials),0) AS denials, COALESCE(SUM(input_tokens),0) AS inp,"
            " COALESCE(SUM(output_tokens),0) AS outp, COALESCE(AVG(latency_ms),0) AS lat,"
            " COALESCE(SUM(invalid_citations),0) AS cit, COALESCE(SUM(unsupported_claims),0) AS unsup,"
            " COALESCE(SUM(false_action_claims),0) AS fac FROM app_turns")
        by_cat = {r["category"]: r["n"] for r in self.db.query(
            "SELECT category, COUNT(*) AS n FROM app_turns GROUP BY category ORDER BY n DESC")}
        outcomes = {r["outcome"]: r["n"] for r in self.db.query(
            "SELECT outcome, COUNT(*) AS n FROM app_turns GROUP BY outcome ORDER BY n DESC")}
        modes = {r["mode"]: r["n"] for r in self.db.query("SELECT mode, COUNT(*) AS n FROM app_turns GROUP BY mode")}
        tool_calls: dict[str, int] = {}
        for r in self.db.query("SELECT tools FROM app_turns"):
            for t in json.loads(r["tools"]):
                tool_calls[t] = tool_calls.get(t, 0) + 1
        cost = self._cost(agg["inp"], agg["outp"])
        minutes = sum(MANUAL_EFFORT_MINUTES.get(c, 5) * n for c, n in by_cat.items())
        return {
            "turns": agg["turns"], "by_category": by_cat, "outcomes": outcomes, "modes": modes,
            "tool_calls": tool_calls, "authorization_denials": agg["denials"],
            "invalid_policy_citations_caught": agg["cit"], "unsupported_claims_caught": agg["unsup"],
            "false_action_claims_caught": agg["fac"],
            "tokens": {"input": agg["inp"], "output": agg["outp"]}, "llm_cost_usd": round(cost, 4),
            "cost_per_turn_usd": round(cost / agg["turns"], 4) if agg["turns"] else 0.0,
            "avg_latency_ms": round(agg["lat"]),
            "estimated_manual_minutes_avoided": minutes,
            "assumptions_minutes_per_request": MANUAL_EFFORT_MINUTES,
            "note": "Time saved is an estimate from explicit, editable assumptions (app/config.py) - validate in a "
                    "pilot against ticket-handling baselines before using it in a business case.",
        }

    def prometheus(self) -> str:
        s = self.snapshot()
        lines = [
            "# HELP hrcopilot_turns_total Assistant turns handled.", "# TYPE hrcopilot_turns_total counter",
            f"hrcopilot_turns_total {s['turns']}",
            "# TYPE hrcopilot_tokens_total counter",
            f'hrcopilot_tokens_total{{direction="input"}} {s["tokens"]["input"]}',
            f'hrcopilot_tokens_total{{direction="output"}} {s["tokens"]["output"]}',
            "# TYPE hrcopilot_llm_cost_usd_total counter", f"hrcopilot_llm_cost_usd_total {s['llm_cost_usd']}",
            "# TYPE hrcopilot_authorization_denials_total counter",
            f"hrcopilot_authorization_denials_total {s['authorization_denials']}",
            "# TYPE hrcopilot_guard_triggers_total counter",
            f'hrcopilot_guard_triggers_total{{guard="citation"}} {s["invalid_policy_citations_caught"]}',
            f'hrcopilot_guard_triggers_total{{guard="grounding"}} {s["unsupported_claims_caught"]}',
            f'hrcopilot_guard_triggers_total{{guard="action_claim"}} {s["false_action_claims_caught"]}',
            "# TYPE hrcopilot_latency_ms_avg gauge", f"hrcopilot_latency_ms_avg {s['avg_latency_ms']}",
        ]
        for cat, n in s["by_category"].items():
            lines.append(f'hrcopilot_turns_by_category{{category="{cat}"}} {n}')
        return "\n".join(lines) + "\n"
