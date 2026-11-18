"""Gemini function-calling loop with a stub client (no network, no key):
tool round-trips through the gateway, history persistence, free-tier quota
fallback across models, and degradation to the offline planner."""
from types import SimpleNamespace as NS

from google.genai import errors, types

from app.agent.gemini_agent import GeminiAgent
from app.agent.orchestrator import Orchestrator


def usage(p=100, c=50):
    return types.GenerateContentResponseUsageMetadata(prompt_token_count=p, candidates_token_count=c)


def reply(*parts, reason=types.FinishReason.STOP):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)), finish_reason=reason)],
        usage_metadata=usage())


def call(name, args, cid):
    return types.Part(function_call=types.FunctionCall(name=name, args=args, id=cid), thought_signature=b"sig")


def text(t):
    return types.Part(text=t)


def quota(day=False):
    msg = "Quota exceeded for requests per day" if day else "Quota exceeded for requests per minute"
    return errors.ClientError(429, {"error": {"code": 429, "message": msg, "status": "RESOURCE_EXHAUSTED"}})


class StubModels:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": list(contents), "config": config})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def orchestrator_with(c, script, models="gemini-a,gemini-b"):
    c.settings.llm_provider = "gemini"
    c.settings.gemini_model, c.settings.gemini_fallback_models = models.split(",")[0], models
    stub = StubModels(script)
    agent = GeminiAgent(c.settings, c.gateway.tool_definitions(), client=NS(models=stub))
    return Orchestrator(c, llm=agent), stub


def test_function_calling_loop_through_gateway(c):
    o, stub = orchestrator_with(c, [
        reply(call("get_leave_balance", {}, "c1"), call("get_employee_profile", {"employee_id": "E1002"}, "c2")),
        reply(text("You have 12 days of annual leave available [LV-1.2]."))])
    r = o.chat(c.principal("E1001"), "s", "How much leave do I have?")
    assert [t["status"] for t in r["trace"]] == ["ok", "denied"]           # gateway still enforces access
    cfg = stub.calls[0]["config"]
    assert cfg.automatic_function_calling.disable is True                  # SDK never runs tools itself
    assert {d.name for d in cfg.tools[0].function_declarations} >= {"check_leave_eligibility", "request_routing_fix"}
    model_turn, results = stub.calls[1]["contents"][-2], stub.calls[1]["contents"][-1]
    assert model_turn.parts[0].thought_signature == b"sig"                 # model turn replayed unchanged
    assert results.role == "user" and [p.function_response.id for p in results.parts] == ["c1", "c2"]
    assert results.parts[1].function_response.response["result"]["status"] == "denied"
    assert r["guard"]["verified"] and r["mode"] == "gemini (gemini-a)" and r["usage"]["input_tokens"] == 200
    # history persisted as JSON and replayed next turn
    stub.script.append(reply(text("Anything else?")))
    o.chat(c.principal("E1001"), "s", "thanks")
    assert len(stub.calls[2]["contents"]) == 5


def test_free_tier_quota_falls_back_to_next_model(c):
    o, stub = orchestrator_with(c, [quota(), reply(text("Hello - how can I help with your leave?"))])
    r = o.chat(c.principal("E1001"), "s", "hi")
    assert [x["model"] for x in stub.calls] == ["gemini-a", "gemini-b"]
    assert r["mode"] == "gemini (gemini-b)"
    # the exhausted model is skipped on the next request (cool-down)
    stub.script.append(reply(text("Sure.")))
    o.chat(c.principal("E1001"), "s", "ok")
    assert stub.calls[-1]["model"] == "gemini-b"


def test_all_models_exhausted_degrades_to_offline(c):
    o, _ = orchestrator_with(c, [quota(day=True), quota(day=True)])
    r = o.chat(c.principal("E1001"), "s", "How much annual leave do I have left?")
    assert r["mode"] == "offline (degraded)" and "12" in r["reply"] and "rate-limited" not in r["reply"]
    assert "rule-based" in r["notice"]


def test_unknown_model_is_skipped(c):
    nf = errors.ClientError(404, {"error": {"code": 404, "message": "model not found", "status": "NOT_FOUND"}})
    o, stub = orchestrator_with(c, [nf, reply(text("Hello."))])
    o.chat(c.principal("E1001"), "s", "hi")
    assert [x["model"] for x in stub.calls] == ["gemini-a", "gemini-b"]


def test_blocked_response_is_handled(c):
    blocked = types.GenerateContentResponse(candidates=[types.Candidate(finish_reason=types.FinishReason.SAFETY)])
    o, _ = orchestrator_with(c, [blocked])
    r = o.chat(c.principal("E1001"), "s", "something")
    assert "can't help" in r["reply"]
