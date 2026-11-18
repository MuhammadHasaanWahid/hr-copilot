import json


def test_team_conflict_plan(call):
    d = call("E0102", "analyze_team_leave_conflicts")["data"]
    assert d["window"] == ["2026-12-07", "2026-12-11"]
    rec = {a["employee"]: a for a in d["request_assessments"]}
    assert rec["Lucas Meyer"]["recommendation"] == "approve"
    assert rec["Fatima Noor"]["recommendation"] == "approve"
    kenji = rec["Kenji Watanabe"]
    assert kenji["recommendation"] != "approve"
    assert any("Tech lead" in x for x in kenji["conflicts"])
    assert kenji["alternatives"] and all(a["start_date"] > "2026-12-11" or a["end_date"] < "2026-12-07"
                                         for a in kenji["alternatives"])
    assert min(x["coverage"] for x in d["coverage_with_recommended_plan"]) >= 0.6
    assert min(x["coverage"] for x in d["coverage_if_all_approved"]) < 0.6


def test_absence_patterns_found_and_anonymised(call):
    d = call("E0501", "analyze_absence_patterns")["data"]
    kinds = {(f["type"], f["group"]) for f in d["findings"]}
    assert ("rising_absence", "Engineering / Platform") in kinds
    assert ("monday_friday_pattern", "Customer Support") in kinds
    assert "People & HR / HR" in d["suppressed_groups_below_min_size"]
    assert d["data_quality"]["absences_recorded_during_approved_leave"] == 1
    assert d["data_quality"]["records_missing_notice_time"] == 2
    blob = json.dumps(d)
    for name in ("Ayesha", "Kenji", "Lucas", "Fatima"):
        assert name not in blob                                    # no individual is identified


def test_routing_diagnosis_root_cause(call):
    d = call("E1010", "diagnose_approval_routing")["data"]
    assert d["status"] == "inconsistent"
    assert d["hris_manager"]["name"] == "Omar Farooq" and d["workflow_approver"]["name"] == "Hina Shah"
    assert "before the department change" in d["findings"][0]["root_cause"]
    assert d["proposed_fix"]["reassign_pending_requests"]


def test_consistency_scan_finds_both_seeded_mismatches(call):
    rows = call("E0501", "scan_data_consistency")["data"]["routing_mismatches"]
    assert {"E1010"} <= {r["employee_id"] for r in rows} and len(rows) == 2
