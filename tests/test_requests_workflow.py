"""Request workflow: employees submit, the right reviewer decides, nobody reviews their own request."""
from datetime import date

import pytest

from app.requests import CaseIn, LeaveIn
from app.security import AuthorizationError


def test_hr_case_flow_attendance_correction(c):
    emp = c.principal("E1006")
    day = c.db.one("SELECT day FROM att_absences WHERE employee_id='E1006' ORDER BY day DESC LIMIT 1")["day"]
    r = c.requests.submit_case(emp, CaseIn(type="attendance_correction", subject="I was working that day",
                                           day=date.fromisoformat(day)))
    rid = r["request_id"]
    # employee and manager cannot decide; HR sees it in the inbox with the system check
    assert not any(x["id"] == rid for x in c.requests.inbox(c.principal("E0102"))["cases"])
    with pytest.raises(AuthorizationError):
        c.requests.decide_case(emp, rid, True)
    item = next(x for x in c.requests.inbox(c.principal("E0501"))["cases"] if x["id"] == rid)
    assert "absence on" in item["preview"]["system_check"]
    out = c.requests.decide_case(c.principal("E0501"), rid, True, "Confirmed with timesheet")
    assert out["status"] == "executed"
    assert not c.db.one("SELECT 1 FROM att_absences WHERE employee_id='E1006' AND day=?", (day,))
    mine = c.requests.my_requests(emp)["requests"]
    assert mine[0]["status"] == "executed" and mine[0]["decision_comment"] == "Confirmed with timesheet"


def test_leave_flow_manager_approves_and_balance_moves(c):
    ayesha, omar = c.principal("E1001"), c.principal("E0102")
    before = c.systems.leave.balance("E1001", "annual", 2027)["used"]
    res = c.requests.submit_leave(ayesha, LeaveIn(start_date=date(2027, 1, 11), end_date=date(2027, 1, 13)))
    rid = res["request_id"]
    assert res["reviewer"] == "Omar Farooq"
    item = next(x for x in c.requests.inbox(omar)["leave"] if x["id"] == rid)
    assert item["assessment"]["recommendation"] == "approve"
    with pytest.raises(AuthorizationError):
        c.requests.decide_leave(ayesha, rid, True)                      # cannot approve own request
    assert c.requests.decide_leave(omar, rid, True)["status"] == "approved"
    assert c.systems.leave.balance("E1001", "annual", 2027)["used"] == before + 3
    with pytest.raises(AuthorizationError):
        c.requests.decide_leave(omar, rid, False)                        # already decided


def test_leave_policy_breach_cannot_be_submitted(c):
    with pytest.raises(ValueError, match="policy"):
        c.requests.submit_leave(c.principal("E1002"), LeaveIn(start_date=date(2026, 12, 14), end_date=date(2026, 12, 24)))


def test_manager_inbox_assessment_and_injection_flag(c):
    items = {x["requester"]["name"]: x for x in c.requests.inbox(c.principal("E0102"))["leave"]}
    assert {"Lucas Meyer", "Fatima Noor", "Kenji Watanabe"} <= set(items)
    assert items["Lucas Meyer"]["flag"]                                    # injected instruction flagged in code
    kenji = items["Kenji Watanabe"]["assessment"]
    assert any(ch["clause"] == "LV-3.4" and ch["status"] == "warn" for ch in kenji["checks"])


def test_misrouted_request_reaches_new_manager_only_after_fix(c):
    omar, hina, admin = c.principal("E0102"), c.principal("E0201"), c.principal("E0502")
    assert "Bilal Ahmed" not in {x["requester"]["name"] for x in c.requests.inbox(omar)["leave"]}
    assert "Bilal Ahmed" in {x["requester"]["name"] for x in c.requests.inbox(hina)["leave"]}
    pid = c.gateway.call(c.principal("E1010"), "request_routing_fix", {})["proposal_id"]
    assert any(x["id"] == pid for x in c.requests.inbox(admin)["cases"])
    c.requests.decide_case(admin, pid, True)
    assert "Bilal Ahmed" in {x["requester"]["name"] for x in c.requests.inbox(omar)["leave"]}


def test_hr_can_override_leave_but_employee_cannot_see_inbox_items(c):
    hr = c.principal("E0501")
    lucas = next(x for x in c.requests.inbox(hr)["leave"] if x["requester"]["name"] == "Lucas Meyer")
    assert lucas["as_override"]
    assert c.requests.inbox(c.principal("E1001")) == {"leave": [], "cases": [], "recently_decided": []}


def test_ai_drafted_hr_request_needs_employee_confirmation(c):
    emp = c.principal("E1001")
    r = c.gateway.call(emp, "submit_hr_request", {"type": "employment_letter", "subject": "Visa application letter"})
    assert r["status"] == "proposed"
    drafts = c.requests.my_requests(emp)["drafts"]
    assert len(drafts) == 1
    c.actions.decide(emp, drafts[0]["id"], True)                          # employee confirms the draft
    mine = c.requests.my_requests(emp)
    assert not mine["drafts"] and mine["requests"][0]["type"] == "employment_letter"
    assert mine["requests"][0]["status"] == "pending"


def test_overview_and_team_views(c):
    ov = c.requests.overview(c.principal("E1001"))
    assert ov["balances"]["annual"]["available"] == 12 and ov["employee"]["manager"] == "Omar Farooq"
    team = c.requests.team(c.principal("E0102"))
    assert len(team["members"]) == 7
    with pytest.raises(AuthorizationError):
        c.requests.team(c.principal("E1001"))
