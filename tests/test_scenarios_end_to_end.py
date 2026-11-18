"""The four scenarios from the assessment brief, end to end through the
orchestrator (offline planner -> gateway -> systems)."""
from fastapi.testclient import TestClient

from app.agent.orchestrator import Orchestrator

S1 = ("I'm planning to take two weeks off next month, but I'm not sure how much leave I have remaining. I also have an "
      "important project deadline during that period. Can you help me figure out whether I can take the leave, what "
      "approvals I need, and what I should do before submitting the request?")
S2 = ("Three members of my team have requested leave during the same week, and we have a critical project delivery "
      "coming up. Can you help me understand the potential impact, identify scheduling conflicts, and suggest a "
      "reasonable approach without violating company policies?")
S3 = ("We've noticed an increase in employee absences across several departments. I want to understand whether there "
      "are any meaningful patterns, identify potential operational concerns, and determine what actions we should consider.")
S4 = ("I recently changed departments, my reporting manager has changed, and I think my leave approval is still going "
      "to my previous manager. Can you check what might be wrong and help me resolve it?")


def test_scenario_1_employee_leave_planning(c):
    o = Orchestrator(c)
    r = o.chat(c.principal("E1001"), "s", S1)
    tools = [t["tool"] for t in r["trace"]]
    assert {"get_leave_balance", "get_project_commitments", "check_leave_eligibility"} <= set(tools)
    assert "2026-12-02" in r["reply"] and "Omar Farooq" in r["reply"] and "Priya Nair" in r["reply"]
    assert r["guard"]["verified"], r["guard"]
    # follow-up: submit with handover -> proposal awaiting the user's own confirmation
    r2 = o.chat(c.principal("E1001"), "s", "submit, handover: Lucas covers backend reviews")
    assert r2["outcome"] == "action_proposed" and "not submitted yet" in r2["reply"]
    pid = c.actions.visible_to(c.principal("E1001"))[0]["id"]
    assert c.actions.decide(c.principal("E1001"), pid, True)["status"] == "executed"


def test_scenario_2_manager_conflicts_and_prompt_injection(c):
    r = Orchestrator(c).chat(c.principal("E0102"), "s", S2)
    assert "Kenji Watanabe" in r["reply"] and "Approve" in r["reply"]
    assert "ignored it" in r["reply"]                                    # injection flagged
    assert r["outcome"] == "answered"                                    # nothing was executed or proposed
    statuses = {x["status"] for x in c.systems.leave.requests(["E1002", "E1003", "E1004"], ("pending", "approved"))}
    assert statuses == {"pending"}


def test_scenario_3_hr_absence_analysis(c):
    r = Orchestrator(c).chat(c.principal("E0501"), "s", S3)
    assert "Engineering / Platform" in r["reply"] and "Customer Support" in r["reply"]
    assert r["category"] == "absence_analytics" and r["guard"]["verified"], r["guard"]
    # the same question from a regular employee is refused by the gateway
    r2 = Orchestrator(c).chat(c.principal("E1001"), "s2", S3)
    assert r2["outcome"] == "denied" and "🔒" in r2["reply"]


def test_scenario_4_routing_fix_flow(c):
    r = Orchestrator(c).chat(c.principal("E1010"), "s", S4)
    assert "Hina Shah" in r["reply"] and "Omar Farooq" in r["reply"]
    assert r["outcome"] == "action_proposed"
    pid = c.actions.visible_to(c.principal("E0502"))[0]["id"]
    assert c.actions.decide(c.principal("E0502"), pid, True)["status"] == "executed"
    assert c.systems.workflow.route("E1010")["approver_id"] == "E0102"


def test_metrics_capture_value_and_cost(c):
    o = Orchestrator(c)
    o.chat(c.principal("E0501"), "s", S3)
    o.chat(c.principal("E1001"), "s", "What is Lucas Meyer's salary?")
    m = c.metrics.snapshot()
    assert m["turns"] == 2 and m["estimated_manual_minutes_avoided"] >= 240


def test_http_api_with_jwt():
    from app.main import app
    client = TestClient(app)
    client.post("/api/reset", headers=_auth(client, "E0502"))
    h = client.get("/health")
    assert h.json()["mode"] == "offline" and h.headers["X-Content-Type-Options"] == "nosniff"
    # no token / forged header / bad token are all rejected
    assert client.post("/api/chat", json={"message": "hi", "session_id": "x"}).status_code == 401
    assert client.post("/api/chat", json={"message": "hi", "session_id": "x"},
                       headers={"X-User-Id": "E0502"}).status_code == 401
    assert client.post("/api/chat", json={"message": "hi", "session_id": "x"},
                       headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401
    assert client.post("/api/auth/login", json={"employee_id": "E9999"}).status_code == 401
    bilal, admin = _auth(client, "E1010"), _auth(client, "E0502")
    assert client.get("/api/auth/me", headers=bilal).json()["role"] == "EMPLOYEE"
    r = client.post("/api/chat", json={"message": S4, "session_id": "x"}, headers=bilal).json()
    assert r["outcome"] == "action_proposed"
    acts = client.get("/api/actions", headers=bilal).json()["actions"]
    assert client.post(f"/api/actions/{acts[0]['id']}/decide", json={"approve": True},
                       headers=bilal).status_code == 403          # self-approval refused
    assert client.post(f"/api/actions/{acts[0]['id']}/decide", json={"approve": True},
                       headers=admin).json()["status"] == "executed"
    assert client.get("/api/audit", headers=admin).json()["chain_valid"]
    assert "hrcopilot_turns_total" in client.get("/metrics").text


def _auth(client, employee_id):
    token = client.post("/api/auth/login", json={"employee_id": employee_id}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_expired_and_tampered_tokens_rejected():
    import pytest as _pytest
    from app import auth
    from app.config import Settings
    s = Settings(jwt_ttl_minutes=-1)
    with _pytest.raises(auth.AuthError):
        auth.verify(s, auth.issue(s, "E1001"))
    s2 = Settings()
    tok = auth.issue(s2, "E1001")
    with _pytest.raises(auth.AuthError):
        auth.verify(Settings(), tok)                               # signed with a different secret


def test_change_of_mind_and_policy_conflict(c):
    o = Orchestrator(c)
    o.chat(c.principal("E1001"), "m", S1)
    r = o.chat(c.principal("E1001"), "m", "Actually, what about 2027-01-11 to 2027-01-13 instead?")
    assert "2027-01-11" in r["reply"] and "Looks good" in r["reply"]
    # asking to submit leave that breaks a hard rule (not enough balance) is refused by the engine
    r = o.chat(c.principal("E1002"), "p", "I want leave from 2026-12-14 to 2026-12-24")
    assert "Not submittable" in r["reply"]
    r = o.chat(c.principal("E1002"), "p", "submit it, handover: Ayesha covers")
    assert "couldn't prepare" in r["reply"] and r["outcome"] != "action_proposed"
