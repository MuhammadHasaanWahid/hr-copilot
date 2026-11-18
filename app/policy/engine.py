"""Deterministic policy engine ("rules as code").

The LLM never decides whether something complies with policy. It asks this
engine, which returns a verdict per rule together with the clause it came from.
That gives three properties management asked for: answers are reproducible,
auditable (clause IDs), and testable in CI like any other business logic.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta

from ..calendar_utils import is_working_day, overlaps, working_days_between
from ..systems import Systems

COVERAGE_MIN = 0.60
CARRY_OVER_CAP = 5
CRITICAL_WINDOW_DAYS = 5


@dataclass
class Check:
    rule: str
    clause: str
    status: str  # pass | warn | fail | info
    detail: str


def protected_window(deadline: date) -> tuple[date, date]:
    """The CRITICAL_WINDOW_DAYS working days up to and including `deadline` (LV-3.4)."""
    days, cur = [], deadline
    while len(days) < CRITICAL_WINDOW_DAYS:
        if is_working_day(cur):
            days.append(cur)
        cur -= timedelta(days=1)
    return days[-1], deadline


class LeavePolicyEngine:
    def __init__(self, systems: Systems, today: date) -> None:
        self.s = systems
        self.today = today

    # ------------------------------------------------------------ coverage
    def daily_coverage(self, manager_id: str, start: date, end: date,
                       extra: list[dict] | None = None, include_pending: bool = True,
                       exclude_request_ids: set[int] | None = None) -> list[dict]:
        """Coverage of `manager_id`'s direct team for each working day in [start, end].
        `extra` = hypothetical absences [{employee_id, start_date, end_date}]."""
        team = [e["id"] for e in self.s.hris.direct_reports(manager_id)]
        statuses = ("pending", "approved") if include_pending else ("approved",)
        reqs = [r for r in self.s.leave.requests(team, statuses, start, end)
                if not exclude_request_ids or r["id"] not in exclude_request_ids]
        reqs += extra or []
        out = []
        for d in working_days_between(start, end):
            away = sorted({r["employee_id"] for r in reqs
                           if date.fromisoformat(str(r["start_date"])) <= d <= date.fromisoformat(str(r["end_date"]))})
            size = len(team)
            out.append({"date": d.isoformat(), "team_size": size, "absent": away,
                        "coverage": round((size - len(away)) / size, 2) if size else 1.0})
        return out

    # ------------------------------------------------------- eligibility
    def evaluate_leave(self, employee_id: str, leave_type: str, start: date, end: date) -> dict:
        emp = self.s.hris.get(employee_id)
        if not emp:
            raise ValueError(f"Unknown employee {employee_id}")
        checks: list[Check] = []
        approvals: list[dict] = []
        days = working_days_between(start, end)
        n = len(days)
        result: dict = {"employee_id": employee_id, "employee_name": emp["name"], "leave_type": leave_type,
                        "start_date": start.isoformat(), "end_date": end.isoformat(), "working_days": n}

        if end < start or n == 0:
            checks.append(Check("Valid date range", "LV-1.2", "fail",
                                "The range contains no working days (weekends/public holidays excluded)."))
            return self._finish(result, checks, approvals, None)
        if start <= self.today and leave_type != "sick":
            checks.append(Check("Future dates", "LV-2.1", "fail", "Annual leave must start in the future."))

        # LV-1.2 balance
        bal = self.s.leave.balance(employee_id, leave_type, start.year)
        if bal is None:
            checks.append(Check("Balance", "LV-1.2", "fail", f"No {leave_type} balance found for {start.year}."))
        else:
            after = bal["available"] - n
            result["balance"] = {"year": start.year, "entitled": bal["entitled"], "carried_over": bal["carried_over"],
                                 "used": bal["used"], "pending": bal["pending"],
                                 "available_before": bal["available"], "available_after": after}
            checks.append(Check("Sufficient balance", "LV-1.2", "pass" if after >= 0 else "fail",
                                f"{bal['available']:g} days available, request uses {n}; {after:g} left."
                                if after >= 0 else
                                f"Request needs {n} days but only {bal['available']:g} are available "
                                f"(short by {-after:g}). Consider fewer days or unpaid leave via HR."))
            if leave_type == "annual" and start.year == self.today.year and after > CARRY_OVER_CAP:
                checks.append(Check("Year-end forfeiture", "LV-4.1", "info",
                                    f"After this request {after:g} days remain; {after - CARRY_OVER_CAP:g} would be "
                                    f"forfeited on 31 Dec unless used (max {CARRY_OVER_CAP} carry over)."))

        # LV-2.1 notice
        earliest = None
        if leave_type != "sick":
            required = 14 if n > 5 else 3
            notice = (start - self.today).days
            if notice >= required:
                checks.append(Check("Notice period", "LV-2.1", "pass",
                                    f"{notice} days notice given; {required} required for a {n}-day request."))
            else:
                earliest = self.today + timedelta(days=required)
                while not is_working_day(earliest):
                    earliest += timedelta(days=1)
                checks.append(Check("Notice period", "LV-2.1", "fail",
                                    f"Only {notice} days notice; {required} required for a {n}-day request. "
                                    f"Earliest compliant start date: {earliest.isoformat()}."))

        # LV-2.2 approval chain (HRIS is the source of truth)
        mgr = self.s.hris.get(emp["manager_id"]) if emp["manager_id"] else None
        if mgr:
            approvals.append({"role": "Reporting manager", "employee_id": mgr["id"], "name": mgr["name"],
                              "clause": "LV-2.2"})
        head = self.s.hris.department_head(emp["department"])
        if n > 10 and head and head["id"] != employee_id:
            approvals.append({"role": "Department head", "employee_id": head["id"], "name": head["name"],
                              "clause": "LV-2.2"})

        # LV-5.1 routing consistency between HRIS and workflow engine
        route = self.s.workflow.route(employee_id)
        if mgr and route and route["approver_id"] != mgr["id"]:
            wrong = self.s.hris.get(route["approver_id"])
            checks.append(Check("Approval routing", "LV-5.1", "warn",
                                f"The workflow engine would route this request to {wrong['name'] if wrong else route['approver_id']}"
                                f", but your HRIS manager is {mgr['name']}. Fix routing before submitting."))
        elif mgr:
            checks.append(Check("Approval routing", "LV-5.1", "pass", f"Requests route to {mgr['name']}."))

        # LV-3.3 / LV-3.4 project commitments
        for a in self.s.projects.assignments_for([employee_id]):
            dl = date.fromisoformat(a["deadline"])
            if start <= dl <= end:
                checks.append(Check("Project handover", "LV-3.3", "warn",
                                    f"'{a['project_name']}' deadline {dl.isoformat()} falls inside your leave "
                                    f"({a['project_role']}). A handover note naming your cover is required."))
            if a["critical"] and a["critical_role"]:
                ws, we = protected_window(dl)
                if overlaps(start, end, ws, we):
                    checks.append(Check("Critical role near deadline", "LV-3.4", "warn",
                                        f"You hold a critical role ({a['project_role']}) on '{a['project_name']}'. "
                                        f"Absence during {ws.isoformat()} to {we.isoformat()} needs a department-head "
                                        f"exception."))
                    if head and head["id"] not in {x["employee_id"] for x in approvals}:
                        approvals.append({"role": "Department head (LV-3.4 exception)", "employee_id": head["id"],
                                          "name": head["name"], "clause": "LV-3.4"})

        # LV-3.1 team coverage, including this hypothetical request
        if mgr:
            cov = self.daily_coverage(mgr["id"], start, end,
                                      extra=[{"employee_id": employee_id, "start_date": start, "end_date": end}])
            low = [c for c in cov if c["coverage"] < COVERAGE_MIN]
            if low:
                worst = min(low, key=lambda c: c["coverage"])
                checks.append(Check("Team coverage", "LV-3.1", "warn",
                                    f"On {len(low)} day(s) team availability would drop below 60% "
                                    f"(lowest {round(worst['coverage'] * 100)}% on {worst['date']}), counting pending and "
                                    f"approved requests. Your manager will need a coverage plan."))
                result["low_coverage_days"] = [c["date"] for c in low]
            else:
                checks.append(Check("Team coverage", "LV-3.1", "pass", "Team stays at or above 60% availability."))

        return self._finish(result, checks, approvals, earliest)

    @staticmethod
    def _finish(result: dict, checks: list[Check], approvals: list[dict], earliest: date | None) -> dict:
        result["checks"] = [asdict(c) for c in checks]
        result["required_approvals"] = approvals
        result["can_submit"] = not any(c.status == "fail" for c in checks)
        result["blocking_issues"] = [c.detail for c in checks if c.status == "fail"]
        result["needs_attention"] = [c.detail for c in checks if c.status == "warn"]
        result["clauses"] = sorted({c.clause for c in checks})
        if earliest:
            result["earliest_compliant_start"] = earliest.isoformat()
        return result
