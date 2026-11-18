"""Exercises the Claude tool-use loop with a stub client (no network, no key):
tool round-trips, history persistence, guards + self-correction, and degraded-mode fallback."""
from types import SimpleNamespace as NS

import anthropic
import httpx

from app.agent.claude_agent import ClaudeAgent
from app.agent.orchestrator import Orchestrator


def usage():
    return NS(input_tokens=100, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def text(t):
    return NS(type="text", text=t, model_dump=lambda **kw: {"type": "text", "text": t})


def tool_use(i, name, inp):
    return NS(type="tool_use", id=i, name=name, input=inp,
              model_dump=lambda **kw: {"type": "tool_use", "id": i, "name": name, "input": inp})


class StubMessages:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})  # snapshot; the agent keeps appending
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def orchestrator_with(c, script):
    stub = StubMessages(script)
    agent = ClaudeAgent(c.settings, c.gateway.tool_definitions(), client=NS(beta=NS(messages=stub)))
    return Orchestrator(c, llm=agent), stub


def test_tool_loop_guard_and_self_correction(c):
    tool_turn = NS(stop_reason="tool_use", usage=usage(), content=[
        tool_use("tu1", "get_leave_balance", {}),
        tool_use("tu2", "get_employee_profile", {"employee_id": "E1002"})])
    bad = NS(stop_reason="end_turn", usage=usage(), content=[
        text("You have 12 days [LV-1.2], 97 of them carried over. Also see [LV-7.7]. Your request has been submitted.")])
    good = NS(stop_reason="end_turn", usage=usage(), content=[text("You have 12 days of annual leave available [LV-1.2].")])
    o, stub = orchestrator_with(c, [tool_turn, bad, good])
    r = o.chat(c.principal("E1001"), "s", "How much leave do I have?")
    assert [t["status"] for t in r["trace"]] == ["ok", "denied"]          # gateway still enforces access
    results = stub.calls[1]["messages"][-1]["content"]
    assert len(results) == 2 and results[1]["is_error"] is True          # both results in ONE user message
    correction = stub.calls[2]["messages"][-1]["content"]
    assert "97" in correction and "LV-7.7" in correction and "submitted" in correction
    assert r["self_corrected"] and r["guard"]["verified"] and r["reply"].startswith("You have 12 days")
    assert stub.calls[0]["thinking"] == {"type": "adaptive"} and stub.calls[0]["fallbacks"] == "default"
    assert r["usage"]["input_tokens"] == 300
    # history was persisted as JSON and is replayed on the next turn
    stub.script.append(NS(stop_reason="end_turn", usage=usage(), content=[text("Anything else?")]))
    o.chat(c.principal("E1001"), "s", "thanks")
    assert len(stub.calls[3]["messages"]) > 5


def test_uncorrectable_answer_gets_visible_warning(c):
    bad = NS(stop_reason="end_turn", usage=usage(), content=[text("Your balance is 99 days.")])
    o, _ = orchestrator_with(c, [bad, bad])
    r = o.chat(c.principal("E1001"), "s", "balance?")
    assert not r["guard"]["verified"] and "Automatic check" in r["reply"] and "99" in r["reply"]


def test_outage_degrades_to_offline_planner(c):
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    o, _ = orchestrator_with(c, [err])
    r = o.chat(c.principal("E1001"), "s", "How much leave balance do I have?")
    assert r["mode"] == "offline (degraded)" and "12" in r["reply"]


def test_circuit_breaker_opens_after_repeated_failures(c):
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    o, stub = orchestrator_with(c, [err, err, err])
    for _ in range(4):
        r = o.chat(c.principal("E1001"), "s", "How much leave balance do I have?")
    assert len(stub.calls) == 3 and "circuit breaker" in r["notice"]
