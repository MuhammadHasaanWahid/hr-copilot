from conftest import make


def test_employee_cannot_read_a_colleague(call):
    assert call("E1001", "get_employee_profile", {"employee_id": "E1002"})["status"] == "denied"
    assert call("E1001", "get_leave_balance", {"employee_id": "E1002"})["status"] == "denied"


def test_find_employee_does_not_leak_people_outside_scope(call):
    assert call("E1001", "find_employee", {"name": "Lucas"})["data"]["matches"] == []
    assert call("E0102", "find_employee", {"name": "Lucas"})["data"]["matches"][0]["employee_id"] == "E1002"


def test_salary_is_redacted_unless_hr_admin(call):
    assert "salary_annual" not in call("E0102", "get_employee_profile", {"employee_id": "E1002"})["data"]
    assert "salary_annual" not in call("E0501", "get_employee_profile", {"employee_id": "E1002"})["data"]
    assert "salary_annual" in call("E0502", "get_employee_profile", {"employee_id": "E1002"})["data"]


def test_manager_cannot_run_org_analytics_or_other_teams(call):
    assert call("E0102", "analyze_absence_patterns")["status"] == "denied"
    assert call("E0102", "analyze_team_leave_conflicts", {"manager_id": "E0201"})["status"] == "denied"
    assert call("E1001", "analyze_team_leave_conflicts")["status"] == "denied"


def test_invalid_and_unknown_tools_are_rejected(call):
    assert call("E1001", "approve_everything")["status"] == "error"
    r = call("E1001", "check_leave_eligibility", {"start_date": "not-a-date", "end_date": "2026-12-01"})
    assert r["status"] == "error" and "start_date" in r["error"]
    assert call("E1001", "get_leave_balance", {"sneaky": 1})["status"] == "error"   # extra fields forbidden


def test_high_risk_fix_requires_a_different_authorised_approver(c, call):
    r = call("E1010", "request_routing_fix")
    assert r["status"] == "proposed"
    pid = r["proposal_id"]
    # requester can't approve, HR partner lacks permission, HR admin can.
    for who in ("E1010", "E0501", "E0102"):
        assert not c.actions.can_decide(c.principal(who), c.actions.get(pid))
    out = c.actions.decide(c.principal("E0502"), pid, approve=True)
    assert out["status"] == "executed" and out["result"]["pending_requests_reassigned"] == 1
    assert c.systems.workflow.route("E1010")["approver_id"] == "E0102"
    assert all(r["approver_id"] == "E0102" for r in c.systems.leave.requests(["E1010"], ("pending",)))
    assert call("E1010", "diagnose_approval_routing")["data"]["status"] == "consistent"
    # cannot be executed twice
    assert not c.actions.can_decide(c.principal("E0502"), c.actions.get(pid))


def test_hr_admin_cannot_self_approve_their_own_high_risk_request(c, call):
    pid = call("E0502", "request_routing_fix", {"employee_id": "E1010"})["proposal_id"]
    assert not c.actions.can_decide(c.principal("E0502"), c.actions.get(pid))
    assert c.actions.can_decide(c.principal("E0500"), c.actions.get(pid))


def test_proposal_is_revalidated_at_execution_time(c, call):
    p1 = call("E1010", "request_routing_fix")["proposal_id"]
    p2 = call("E1010", "request_routing_fix")["proposal_id"]
    assert c.actions.decide(c.principal("E0502"), p1, True)["status"] == "executed"
    assert c.actions.decide(c.principal("E0502"), p2, True)["status"] == "failed"   # nothing left to fix


def test_autonomy_dial():
    adv = make("advisory")
    r = adv.gateway.call(adv.principal("E1010"), "request_routing_fix", {})
    assert r["status"] == "blocked"

    auto = make("autonomous")
    low = auto.gateway.call(auto.principal("E1001"), "submit_leave_request",
                            {"start_date": "2027-01-11", "end_date": "2027-01-13", "reason": "Trip"})
    assert low["status"] == "executed"
    high = auto.gateway.call(auto.principal("E1010"), "request_routing_fix", {})
    assert high["status"] == "proposed"        # high risk is NEVER auto-executed


def test_submit_requires_handover_when_deadline_inside_leave(call):
    r = call("E1001", "submit_leave_request", {"start_date": "2026-12-02", "end_date": "2026-12-15"})
    assert r["status"] == "error" and "handover" in r["error"].lower()
    r = call("E1001", "submit_leave_request", {"start_date": "2026-12-02", "end_date": "2026-12-15",
                                               "handover_note": "Lucas covers backend"})
    assert r["status"] == "proposed"


def test_every_call_is_audited_and_chain_is_tamper_evident(c, call):
    call("E1001", "get_leave_balance")
    call("E1001", "get_employee_profile", {"employee_id": "E1002"})
    events = [e["event"] for e in c.audit.entries()]
    assert "tool.read" in events and "tool.denied" in events
    assert c.audit.verify()
    c.db.execute("UPDATE app_audit SET details = '{\"tool\": \"tampered\"}' WHERE seq = 1")
    assert not c.audit.verify()


def test_untrusted_text_is_spotlighted(call):
    reqs = call("E0102", "list_leave_requests", {"team": True, "status": "pending"})["data"]["requests"]
    lucas = next(r for r in reqs if r["employee_id"] == "E1002")
    text = lucas["employee_provided_reason__untrusted_text"]
    assert text.startswith("[EMPLOYEE_TEXT]") and text.endswith("[/EMPLOYEE_TEXT]")


def test_proposals_survive_restart(tmp_path):
    from conftest import TODAY
    from app.config import Settings
    from app.container import Container
    db = str(tmp_path / "hr.db")
    c1 = Container(Settings(llm_mode="offline", today=TODAY), db_path=db)
    pid = c1.gateway.call(c1.principal("E1010"), "request_routing_fix", {})["proposal_id"]
    c1.db.close()
    c2 = Container(Settings(llm_mode="offline", today=TODAY), db_path=db)
    assert c2.actions.get(pid)["status"] == "pending"
    assert c2.actions.decide(c2.principal("E0502"), pid, True)["status"] == "executed"
    events = [e["event"] for e in c2.audit.entries()]
    assert c2.audit.verify() and events == ["proposal.created", "proposal.executed"]
