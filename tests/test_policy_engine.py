from datetime import date


def checks_by_clause(ev):
    return {c["clause"]: c for c in ev["checks"]}


def test_two_weeks_next_month_is_blocked_by_notice_and_flags_critical_role(c):
    ev = c.engine.evaluate_leave("E1001", "annual", date(2026, 12, 1), date(2026, 12, 14))
    cl = checks_by_clause(ev)
    assert ev["working_days"] == 10
    assert ev["balance"]["available_before"] == 12 and ev["balance"]["available_after"] == 2
    assert cl["LV-2.1"]["status"] == "fail"                    # 13 days notice < 14
    assert ev["earliest_compliant_start"] == "2026-12-02"
    assert cl["LV-3.4"]["status"] == "warn"                    # backend lead on Atlas near deadline
    assert cl["LV-3.3"]["status"] == "warn"                    # deadline inside the leave
    assert cl["LV-3.1"]["status"] == "warn"                    # team coverage drops below 60%
    assert not ev["can_submit"]
    roles = [a["role"] for a in ev["required_approvals"]]
    assert roles[0] == "Reporting manager" and any("LV-3.4" in r for r in roles)


def test_leave_after_the_deadline_is_clean(c):
    ev = c.engine.evaluate_leave("E1001", "annual", date(2027, 1, 11), date(2027, 1, 13))
    assert ev["can_submit"]
    assert not ev["needs_attention"]


def test_insufficient_balance_fails(c):
    ev = c.engine.evaluate_leave("E1002", "annual", date(2026, 12, 14), date(2026, 12, 24))
    assert checks_by_clause(ev)["LV-1.2"]["status"] == "fail"


def test_routing_inconsistency_is_surfaced_during_eligibility(c):
    ev = c.engine.evaluate_leave("E1010", "annual", date(2027, 2, 1), date(2027, 2, 2))
    assert checks_by_clause(ev)["LV-5.1"]["status"] == "warn"


def test_policy_retrieval_and_citation_validation(c):
    top = c.kb.search("how much notice do I need before leave")[0]
    assert top["clause_id"] == "LV-2.1"
    valid, invalid = c.kb.validate_citations("See [LV-2.1] and [LV-9.9].")
    assert valid == ["LV-2.1"] and invalid == ["LV-9.9"]
