"""Decision-support analytics: the 'thinking' the LLM should not do by itself.

* TeamConflictAnalyzer  - constraint search over competing leave requests.
* AbsenceAnalyzer       - statistical pattern detection with k-anonymity.
* ConsistencyChecker    - cross-system reconciliation (HRIS vs workflow vs leave).

The LLM orchestrates these, explains them, and asks follow-ups; the numbers come
from code. That split is the main defence against convincing-but-wrong answers.
"""
from __future__ import annotations

import itertools
import math
from collections import defaultdict
from datetime import date, timedelta

from .calendar_utils import add_working_days, overlaps, week_bounds, working_days_between
from .policy.engine import CARRY_OVER_CAP, COVERAGE_MIN, LeavePolicyEngine, protected_window
from .security import untrusted
from .systems import Systems


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(x)


# =========================================================================== conflicts
class TeamConflictAnalyzer:
    MAX_EXACT = 12  # exhaustive search up to 2^12 subsets; greedy beyond

    def __init__(self, systems: Systems, engine: LeavePolicyEngine, today: date) -> None:
        self.s, self.engine, self.today = systems, engine, today

    def _busiest_window(self, team: list[str]) -> tuple[date, date] | None:
        pend = self.s.leave.requests(team, ("pending",), self.today, self.today + timedelta(days=120))
        if not pend:
            return None
        weeks: dict[date, set[str]] = defaultdict(set)
        for r in pend:
            for d in working_days_between(_d(r["start_date"]), _d(r["end_date"])):
                weeks[week_bounds(d)[0]].add(r["employee_id"])
        monday = max(weeks, key=lambda w: (len(weeks[w]), -w.toordinal()))
        return monday, monday + timedelta(days=4)

    def analyze(self, manager_id: str, start: date | None = None, end: date | None = None) -> dict:
        team_rows = self.s.hris.direct_reports(manager_id)
        team = [e["id"] for e in team_rows]
        names = {e["id"]: e["name"] for e in team_rows}
        if not team:
            return {"message": "This manager has no direct reports in HRIS."}
        if not (start and end):
            win = self._busiest_window(team)
            if not win:
                return {"message": "No pending leave requests for this team in the next 120 days."}
            start, end = win
        reqs = self.s.leave.requests(team, ("pending", "approved"), start, end)
        pending = [r for r in reqs if r["status"] == "pending"]
        approved = [r for r in reqs if r["status"] == "approved"]

        # Critical-role constraints (LV-3.4) relevant to this window.
        critical = []
        for a in self.s.projects.assignments_for(team):
            if a["critical"] and a["critical_role"]:
                ws, we = protected_window(_d(a["deadline"]))
                if overlaps(ws, we, start - timedelta(days=21), end + timedelta(days=21)):
                    critical.append({"employee_id": a["employee_id"], "project": a["project_name"],
                                     "role": a["project_role"], "deadline": a["deadline"],
                                     "protected_from": ws.isoformat(), "protected_to": we.isoformat()})
        deadlines = sorted({(a["project_name"], a["deadline"], bool(a["critical"]))
                            for a in self.s.projects.assignments_for(team)
                            if start - timedelta(days=7) <= _d(a["deadline"]) <= end + timedelta(days=7)})

        def violations(selected: list[dict]) -> list[str]:
            out = []
            lo, hi = min([start] + [_d(r["start_date"]) for r in selected]), max([end] + [_d(r["end_date"]) for r in selected])
            # include_pending=False: approved requests come from the DB, pending ones only if selected.
            cov = self.engine.daily_coverage(manager_id, lo, hi, extra=selected, include_pending=False)
            for c in cov:
                if c["coverage"] < COVERAGE_MIN:
                    out.append(f"coverage {round(c['coverage'] * 100)}% on {c['date']}")
            for r in selected:
                for c in critical:
                    if c["employee_id"] == r["employee_id"] and overlaps(
                            _d(r["start_date"]), _d(r["end_date"]), _d(c["protected_from"]), _d(c["protected_to"])):
                        out.append(f"{names[r['employee_id']]} is {c['role']} on {c['project']} "
                                   f"(protected {c['protected_from']}..{c['protected_to']})")
            return out

        # Priority per LV-3.2: (a) critical coverage handled as a hard constraint above,
        # (b) forfeiture risk, (c) submission order.
        by_submit = sorted(pending, key=lambda r: r["submitted_at"])
        info = {}
        for rank, r in enumerate(by_submit):
            bal = self.s.leave.balance(r["employee_id"], r["leave_type"], _d(r["start_date"]).year)
            # 'available' already nets out pending requests, i.e. the balance if everything is approved.
            remaining_if_approved = bal["available"] if bal else 0
            remaining_if_declined = remaining_if_approved + r["working_days"]
            forfeit_if_declined = max(0.0, remaining_if_declined - CARRY_OVER_CAP) \
                if _d(r["start_date"]).year == self.today.year else 0.0
            info[r["id"]] = {"submission_rank": rank + 1, "forfeit_if_declined": forfeit_if_declined}

        def priority(subset: tuple[dict, ...]) -> tuple:
            return (len(subset),
                    sum(info[r["id"]]["forfeit_if_declined"] for r in subset),
                    -sum(info[r["id"]]["submission_rank"] for r in subset))

        best: tuple[dict, ...] = ()
        if len(pending) <= self.MAX_EXACT:
            for k in range(len(pending), -1, -1):
                feasible = [c for c in itertools.combinations(pending, k) if not violations(list(c))]
                if feasible:
                    best = max(feasible, key=priority)
                    break
        else:  # greedy fallback for very large teams
            chosen: list[dict] = []
            for r in sorted(pending, key=lambda r: (-info[r["id"]]["forfeit_if_declined"], info[r["id"]]["submission_rank"])):
                if not violations(chosen + [r]):
                    chosen.append(r)
            best = tuple(chosen)
        best_ids = {r["id"] for r in best}

        assessments = []
        for r in by_submit:
            alone = violations([r])
            with_others = violations(list(best) + [r]) if r["id"] not in best_ids else []
            entry = {
                "request_id": r["id"], "employee": names[r["employee_id"]], "employee_id": r["employee_id"],
                "dates": f"{r['start_date']} to {r['end_date']}", "working_days": r["working_days"],
                "submitted_at": r["submitted_at"], "submission_rank": info[r["id"]]["submission_rank"],
                "forfeits_days_if_declined": info[r["id"]]["forfeit_if_declined"],
                "employee_provided_reason__untrusted_text": untrusted(r["reason"]),
                "critical_role": next((f"{c['role']} on {c['project']}" for c in critical
                                       if c["employee_id"] == r["employee_id"]), None),
            }
            if r["id"] in best_ids:
                entry["recommendation"] = "approve"
            else:
                entry["recommendation"] = "offer alternative dates" if not alone else "needs exception or new dates"
                entry["conflicts"] = with_others or alone
                entry["alternatives"] = self._alternatives(r, list(best), violations)
            assessments.append(entry)

        all_cov = self.engine.daily_coverage(manager_id, start, end)
        plan_cov = self.engine.daily_coverage(manager_id, start, end, extra=list(best), include_pending=False)
        return {
            "manager": self.s.hris.get(manager_id)["name"], "window": [start.isoformat(), end.isoformat()],
            "team_size": len(team), "coverage_threshold": COVERAGE_MIN,
            "project_deadlines_near_window": [{"project": p, "deadline": d, "critical": c} for p, d, c in deadlines],
            "critical_role_constraints": [dict(c, employee=names[c["employee_id"]]) for c in critical],
            "already_approved": [{"employee": names[r["employee_id"]], "dates": f"{r['start_date']} to {r['end_date']}"}
                                 for r in approved],
            "coverage_if_all_approved": [{"date": c["date"], "coverage": c["coverage"],
                                          "absent": [names.get(a, a) for a in c["absent"]]} for c in all_cov],
            "coverage_with_recommended_plan": [{"date": c["date"], "coverage": c["coverage"]} for c in plan_cov],
            "request_assessments": assessments,
            "method": "Exhaustive search over approve/decline combinations maximising approvals, then LV-3.2 priority "
                      "(forfeiture risk, submission order), subject to LV-3.1 coverage >= 60% and LV-3.4 critical-role "
                      "protection.",
            "clauses": ["LV-3.1", "LV-3.2", "LV-3.4", "LV-4.1", "DP-1.2"],
            "decision_owner": "The manager decides; this is a recommendation (DP-1.2).",
        }

    def _alternatives(self, r: dict, best: list[dict], violations) -> list[dict]:
        s0, e0 = _d(r["start_date"]), _d(r["end_date"])
        n = max(1, len(working_days_between(s0, e0)))
        options = []
        for shift in sorted(range(-10, 16), key=lambda k: (abs(k), -k)):
            if shift == 0:
                continue
            s = s0 + timedelta(days=shift)
            if s.weekday() >= 5 or s <= self.today + timedelta(days=3):
                continue
            e = add_working_days(s, n)
            cand = dict(r, start_date=s.isoformat(), end_date=e.isoformat())
            if not violations(best + [cand]):
                options.append({"start_date": s.isoformat(), "end_date": e.isoformat(), "working_days": n})
            if len(options) == 2:
                break
        return options


# ============================================================================ absences
class AbsenceAnalyzer:
    # We test ~12 groups x 2 hypotheses at once, so a plain z>=2 (p~0.05) would
    # flag noise. z>=2.5 is a pragmatic multiple-comparison guard for a PoC.
    Z_THRESHOLD = 2.5

    def __init__(self, systems: Systems, today: date, min_group: int = 5) -> None:
        self.s, self.today, self.min_group = systems, today, min_group

    @staticmethod
    def _z_two_prop(x1: int, n1: int, x2: int, n2: int) -> float:
        if not n1 or not n2:
            return 0.0
        p = (x1 + x2) / (n1 + n2)
        se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) or 1e-9
        return (x1 / n1 - x2 / n2) / se

    def analyze(self, department: str | None = None, recent_weeks: int = 4, baseline_weeks: int = 20) -> dict:
        recent_start = self.today - timedelta(weeks=recent_weeks)
        base_start = recent_start - timedelta(weeks=baseline_weeks)
        recent_days = working_days_between(recent_start, self.today - timedelta(days=1))
        base_days = working_days_between(base_start, recent_start - timedelta(days=1))
        employees = {e["id"]: e for e in self.s.hris.all()}
        absences = self.s.attendance.absences(base_start, self.today)

        # ---------------- data quality (incomplete / inconsistent source data)
        approved = self.s.leave.requests(None, ("approved",))
        on_leave = {(r["employee_id"], d.isoformat()) for r in approved
                    for d in working_days_between(_d(r["start_date"]), _d(r["end_date"]))}
        dq = {
            "absences_recorded_during_approved_leave": sum((a["employee_id"], a["day"]) in on_leave for a in absences),
            "records_missing_notice_time": sum(a["notice_hours"] is None for a in absences),
            "records_for_unknown_employees": sum(a["employee_id"] not in employees for a in absences),
        }
        clean = [a for a in absences if a["employee_id"] in employees and (a["employee_id"], a["day"]) not in on_leave]

        groups: dict[str, set[str]] = defaultdict(set)
        for e in employees.values():
            if department and e["department"].lower() != department.lower():
                continue
            groups[e["department"]].add(e["id"])
            if e["team"]:
                groups[f"{e['department']} / {e['team']}"].add(e["id"])
        if not groups:
            return {"message": f"No employees found for department '{department}'."}

        crit_soon = self.s.projects.critical_assignees(self.today - timedelta(days=30), self.today + timedelta(days=45))

        # One pass over absences -> per-employee counters, so group aggregation is
        # O(absences + headcount) and stays fast at 50k+ employees.
        rec_start_iso = recent_start.isoformat()
        stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])  # recent, baseline, mon/fri, short-notice recent
        for a in clean:
            st = stats[a["employee_id"]]
            is_recent = a["day"] >= rec_start_iso
            st[0 if is_recent else 1] += 1
            st[2] += _d(a["day"]).weekday() in (0, 4)
            st[3] += is_recent and a["notice_hours"] is not None and a["notice_hours"] < 2

        results, suppressed, findings = [], [], []
        for g, members in sorted(groups.items()):
            if len(members) < self.min_group:
                suppressed.append(g)
                continue
            n_r = sum(stats[m][0] for m in members if m in stats)
            n_b = sum(stats[m][1] for m in members if m in stats)
            mf = sum(stats[m][2] for m in members if m in stats)
            short = sum(stats[m][3] for m in members if m in stats)
            n_rec, n_base = len(members) * len(recent_days), len(members) * len(base_days)
            r_rate, b_rate = n_r / n_rec, n_b / max(1, n_base)
            z = self._z_two_prop(n_r, n_rec, n_b, n_base)
            total = n_r + n_b
            mf_share = mf / total if total else 0
            mf_z = (mf - 0.4 * total) / math.sqrt(total * 0.4 * 0.6) if total else 0
            crit_load = sum(1 for m in members if m in crit_soon) / len(members)
            row = {"group": g, "headcount": len(members), "recent_absence_rate_pct": round(100 * r_rate, 2),
                   "baseline_absence_rate_pct": round(100 * b_rate, 2),
                   "change_ratio": round(r_rate / b_rate, 2) if b_rate else None, "z_score": round(z, 2),
                   "monday_friday_share_pct": round(100 * mf_share, 1),
                   "short_notice_share_recent_pct": round(100 * short / n_r, 1) if n_r else 0,
                   "share_on_critical_project_near_deadline_pct": round(100 * crit_load)}
            results.append(row)
            is_team = " / " in g
            if z >= self.Z_THRESHOLD and b_rate and r_rate / b_rate >= 1.5:
                sev = "high" if r_rate / b_rate >= 2.5 else "medium"
                f = {"type": "rising_absence", "group": g, "severity": sev,
                     "evidence": f"Absence rate {row['recent_absence_rate_pct']}% in the last {recent_weeks} weeks vs "
                                 f"{row['baseline_absence_rate_pct']}% baseline (x{row['change_ratio']}, z={row['z_score']}).",
                     "level": "team" if is_team else "department"}
                if crit_load >= 0.5:
                    f["hypothesis"] = (f"{row['share_on_critical_project_near_deadline_pct']}% of this group works on a "
                                       "business-critical project with a deadline in the next 45 days, and "
                                       f"{row['short_notice_share_recent_pct']}% of recent absences were short-notice. "
                                       "Pattern is consistent with workload pressure / burnout risk (hypothesis, not proven).")
                    f["recommended_actions"] = [
                        "HRBP to review workload and staffing with the manager before the deadline (AT-1.2).",
                        "Offer wellbeing check-ins and confirm cover for critical roles (LV-3.4).",
                        "Re-run this analysis 2 weeks after the deadline to see if the rate normalises."]
                else:
                    f["recommended_actions"] = [
                        "HRBP to discuss with the department manager to understand local causes (AT-1.2).",
                        "Check for seasonal illness or a single long-term case inflating the rate."]
                findings.append(f)
            if total >= 15 and mf_share >= 0.55 and mf_z >= self.Z_THRESHOLD:
                findings.append({
                    "type": "monday_friday_pattern", "group": g, "severity": "medium",
                    "level": "team" if is_team else "department",
                    "evidence": f"{row['monday_friday_share_pct']}% of absences fall on Mondays/Fridays vs ~40% expected "
                                f"(z={round(mf_z, 2)}, n={total}).",
                    "hypothesis": "Possible engagement or scheduling issue (e.g. rota fatigue, weekend shift pattern). "
                                  "Not evidence of misconduct about any individual.",
                    "recommended_actions": [
                        "Review rota / shift design and workload distribution with the team lead (AT-1.2).",
                        "Consider flexible-start or compressed-week pilots and measure impact.",
                        "Run an anonymous pulse survey before any individual-level review."]})

        tot_rec = sum(1 for a in clean if a["day"] >= rec_start_iso)
        tot_base = len(clean) - tot_rec
        hc = len(employees)
        overall = {"recent_rate_pct": round(100 * tot_rec / (hc * len(recent_days)), 2),
                   "baseline_rate_pct": round(100 * tot_base / max(1, hc * len(base_days)), 2)}
        findings.sort(key=lambda f: {"high": 0, "medium": 1}.get(f["severity"], 2))
        return {
            "period": {"recent": [recent_start.isoformat(), (self.today - timedelta(days=1)).isoformat()],
                       "baseline": [base_start.isoformat(), (recent_start - timedelta(days=1)).isoformat()]},
            "organisation": overall, "groups": results, "findings": findings,
            "suppressed_groups_below_min_size": suppressed, "min_group_size": self.min_group,
            "data_quality": dq,
            "caveats": ["Aggregated data only; no individual is identified (AT-1.1).",
                        "Correlation is not causation; hypotheses must be validated with managers.",
                        "Headcount uses current HRIS team membership; movers may slightly skew team rates.",
                        f"{sum(dq.values())} source records were excluded or flagged for data-quality reasons."],
            "clauses": ["AT-1.1", "AT-1.2"],
        }


# ========================================================================= consistency
class ConsistencyChecker:
    def __init__(self, systems: Systems, today: date) -> None:
        self.s, self.today = systems, today

    def diagnose(self, employee_id: str) -> dict:
        emp = self.s.hris.get(employee_id)
        if not emp:
            raise ValueError(f"Unknown employee {employee_id}")
        mgr = self.s.hris.get(emp["manager_id"]) if emp["manager_id"] else None
        route = self.s.workflow.route(employee_id)
        router = self.s.hris.get(route["approver_id"]) if route else None
        pending = self.s.leave.requests([employee_id], ("pending",))
        findings = []
        if not route:
            findings.append({"severity": "high", "system": "Workflow engine",
                             "issue": "No approval route exists; requests cannot be routed."})
        elif mgr and route["approver_id"] != mgr["id"]:
            stale = emp["department_changed_on"] and route["updated_at"] < emp["department_changed_on"]
            findings.append({
                "severity": "high", "system": "Workflow engine",
                "issue": f"Approval route points to {router['name'] if router else route['approver_id']}"
                         f" ({router['title'] if router else 'unknown'}), but HRIS reporting manager is "
                         f"{mgr['name']}.",
                "evidence": {"hris_manager": mgr["id"], "workflow_approver": route["approver_id"],
                             "route_last_updated": route["updated_at"], "route_source": route["source"],
                             "hris_department_changed_on": emp["department_changed_on"]},
                "root_cause": ("Route was last synced on " + route["updated_at"] + ", before the department change on "
                               + emp["department_changed_on"] + " - the HRIS-to-workflow sync did not run for this change.")
                if stale else "Route differs from HRIS (manual override or failed sync)."})
        for r in pending:
            if mgr and r["approver_id"] != mgr["id"]:
                who = self.s.hris.get(r["approver_id"])
                findings.append({"severity": "high", "system": "Leave system",
                                 "issue": f"Pending request #{r['id']} ({r['start_date']} to {r['end_date']}) is waiting "
                                          f"on {who['name'] if who else r['approver_id']}, who is no longer your manager.",
                                 "evidence": {"request_id": r["id"], "approver_id": r["approver_id"],
                                              "submitted_at": r["submitted_at"]}})
        fix = None
        if findings and mgr:
            fix = {"action": "fix_approval_routing", "set_route_to": {"id": mgr["id"], "name": mgr["name"]},
                   "reassign_pending_requests": [r["id"] for r in pending if r["approver_id"] != mgr["id"]],
                   "risk": "high", "who_can_execute": "HR systems administrator (LV-5.1)"}
        return {"employee": emp["name"], "employee_id": employee_id, "department": emp["department"],
                "department_changed_on": emp["department_changed_on"],
                "hris_manager": mgr and {"id": mgr["id"], "name": mgr["name"]},
                "workflow_approver": router and {"id": router["id"], "name": router["name"]},
                "status": "inconsistent" if findings else "consistent", "findings": findings,
                "proposed_fix": fix, "clauses": ["LV-5.1"]}

    def scan(self, limit: int = 200) -> dict:
        """One set-based query (scales to large headcounts) instead of a lookup per employee."""
        rows = self.s.db.query(
            "SELECT e.id AS employee_id, e.name AS employee, e.department, e.manager_id AS hris_manager,"
            " r.approver_id AS workflow_approver, r.updated_at AS route_updated_at"
            " FROM hris_employees e LEFT JOIN wf_approval_routes r ON r.employee_id = e.id"
            " WHERE e.manager_id IS NOT NULL AND (r.approver_id IS NULL OR r.approver_id != e.manager_id)"
            " ORDER BY e.id")
        total = self.s.db.one("SELECT COUNT(*) AS n FROM hris_employees")["n"]
        return {"employees_checked": total, "routing_mismatch_count": len(rows),
                "routing_mismatches": rows[:limit], "clauses": ["LV-5.1"]}
