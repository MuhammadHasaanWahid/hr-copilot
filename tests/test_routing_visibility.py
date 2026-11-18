"""The UI must explain approval-routing errors to both sides."""


def test_employee_sees_routing_issue_until_fixed(c):
    bilal = c.principal("E1010")
    issue = c.requests.my_requests(bilal)["routing_issue"]
    assert issue == {"current_manager": "Omar Farooq", "routed_to": "Hina Shah", "fix_requested": False}
    pid = c.gateway.call(bilal, "request_routing_fix", {})["proposal_id"]
    assert c.requests.my_requests(bilal)["routing_issue"]["fix_requested"] is True
    c.requests.decide_case(c.principal("E0502"), pid, True)
    assert c.requests.my_requests(bilal)["routing_issue"] is None
    assert c.requests.my_requests(c.principal("E1001"))["routing_issue"] is None


def test_manager_is_told_about_stuck_requests(c):
    team = c.requests.team(c.principal("E0102"))
    assert team["misrouted"] == [{"employee": "Bilal Ahmed", "routed_to": "Hina Shah", "requests": 1}]
