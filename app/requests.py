"""Employee requests and their review workflow.

Every request has exactly one accountable reviewer role:

    Leave                      -> the employee's reporting manager (policy LV-2.2); HR may also decide
    Attendance correction      -> HR
    Personal details update    -> HR
    Employment letter          -> HR
    Other HR request           -> HR
    Approval-routing fix       -> HR systems administrator (system configuration, high risk)

Requests are created by the employee directly (form) or drafted by the AI
assistant and confirmed by the employee. Only a human reviewer can approve or
reject; nobody can review their own request; every decision re-checks the
current state and is written to the audit log.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .actions import ActionService
from .audit import AuditLog
from .calendar_utils import working_days_between
from .policy.engine import LeavePolicyEngine
from .security import AuthorizationError, Principal, looks_like_injection, require
from .systems import Systems

CaseType = Literal["attendance_correction", "personal_details", "employment_letter", "general"]

CASE_TYPES: dict[str, dict] = {
    "attendance_correction": {"label": "Attendance correction", "reviewer": "hr.review",
                              "help": "An absence was recorded for a day you actually worked."},
    "personal_details": {"label": "Personal details update", "reviewer": "hr.review",
                         "help": "Update your work location or other personal details."},
    "employment_letter": {"label": "Employment letter", "reviewer": "hr.review",
                          "help": "Proof of employment for a visa, bank or landlord."},
    "general": {"label": "Other HR request", "reviewer": "hr.review", "help": "Anything else for HR."},
    "request_routing_fix": {"label": "Approval routing fix", "reviewer": "routing.fix.approve", "help": ""},
}
REQUEST_LABELS = {k: v["label"] for k, v in CASE_TYPES.items()} | {"leave": "Leave"}


class CaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: CaseType
    subject: str = Field(min_length=3, max_length=120)
    details: str = Field("", max_length=1500)
    day: date | None = None                       # attendance correction: the day in question
    new_location: str | None = Field(None, max_length=60)  # personal details


class LeaveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date
    end_date: date
    leave_type: Literal["annual", "sick"] = "annual"
    reason: str = Field("", max_length=300)
    handover_note: str | None = Field(None, max_length=1000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RequestService:
    def __init__(self, s: Systems, engine: LeavePolicyEngine, actions: ActionService, audit: AuditLog,
                 today: date) -> None:
        self.s, self.engine, self.actions, self.audit, self.today = s, engine, actions, audit, today
        for t in ("attendance_correction", "personal_details", "employment_letter", "general"):
            actions.register_executor(t, self._execute_case)

    # ================================================================ leave
    def submit_leave(self, p: Principal, body: LeaveIn) -> dict:
        require(p, "leave.submit.self")
        ev = self.engine.evaluate_leave(p.id, body.leave_type, body.start_date, body.end_date)
        if not ev["can_submit"]:
            raise ValueError("This request breaks a policy rule: " + " ".join(ev["blocking_issues"]))
        if any(c["clause"] == "LV-3.3" for c in ev["checks"]) and not (body.handover_note or "").strip():
            raise ValueError("A handover note is required because a project deadline falls inside the leave (LV-3.3).")
        route = self.s.workflow.route(p.id)
        rid = self.s.leave.create_request(p.id, body.leave_type, body.start_date, body.end_date, ev["working_days"],
                                          route["approver_id"], self.today, body.reason, body.handover_note)
        self.audit.record(p.id, "leave.submitted", request_id=rid, start=body.start_date.isoformat(),
                          end=body.end_date.isoformat(), days=ev["working_days"])
        return {"request_id": rid, "status": "pending", "reviewer": self._name(route["approver_id"]),
                "warnings": ev["needs_attention"]}

    def check_leave(self, p: Principal, body: LeaveIn) -> dict:
        require(p, "leave.submit.self")
        return self.engine.evaluate_leave(p.id, body.leave_type, body.start_date, body.end_date)

    def _can_review_leave(self, p: Principal, r: dict) -> bool:
        if r["status"] != "pending" or r["employee_id"] == p.id:
            return False
        return r["approver_id"] == p.id or p.can("leave.review.all")

    def decide_leave(self, p: Principal, request_id: int, approve: bool, comment: str | None = None) -> dict:
        with self.s.db.transaction():
            r = self.s.db.one("SELECT * FROM leave_requests WHERE id = ?", (request_id,))
            if not r:
                raise KeyError(request_id)
            if not self._can_review_leave(p, r):
                self.audit.record(p.id, "leave.decision_denied", request_id=request_id)
                raise AuthorizationError("You can't decide on this request (not the assigned reviewer, already "
                                         "decided, or it's your own request).")
            if approve:
                bal = self.s.leave.balance(r["employee_id"], r["leave_type"], int(r["start_date"][:4]))
                if bal is None or bal["available"] < 0:
                    raise ValueError("Cannot approve: the employee no longer has enough leave balance.")
                self.s.db.execute("UPDATE leave_balances SET used = used + ? WHERE employee_id=? AND leave_type=? "
                                  "AND year=?", (r["working_days"], r["employee_id"], r["leave_type"],
                                                 int(r["start_date"][:4])))
            self.s.db.execute("UPDATE leave_requests SET status=?, decided_by=?, decided_at=?, decision_comment=? "
                              "WHERE id=? AND status='pending'",
                              ("approved" if approve else "rejected", p.id, _now(), comment, request_id))
        self.audit.record(p.id, "leave.approved" if approve else "leave.rejected", request_id=request_id,
                          on_behalf_of=r["employee_id"], comment=comment,
                          as_hr_override=r["approver_id"] != p.id)
        return {"request_id": request_id, "status": "approved" if approve else "rejected"}

    # ================================================================ cases
    def submit_case(self, p: Principal, body: CaseIn) -> dict:
        require(p, "request.submit.self")
        meta = CASE_TYPES[body.type]
        preview: dict = {"subject": body.subject, "details": body.details}
        if body.type == "attendance_correction":
            if not body.day:
                raise ValueError("Please give the date that should be corrected.")
            rec = self.s.db.one("SELECT * FROM att_absences WHERE employee_id=? AND day=?", (p.id, body.day.isoformat()))
            preview["day"] = body.day.isoformat()
            preview["system_check"] = (f"Attendance system shows a '{rec['kind']}' absence on {rec['day']}."
                                       if rec else f"No absence is recorded on {body.day.isoformat()} - nothing to correct.")
        if body.type == "personal_details" and body.new_location:
            emp = self.s.hris.get(p.id)
            preview["system_check"] = f"Work location: {emp['location']} -> {body.new_location}"
            preview["new_location"] = body.new_location
        if looks_like_injection(body.subject + " " + body.details):
            preview["flag"] = "Contains instruction-like text - review carefully."
        args = body.model_dump(mode="json", exclude_none=True)
        p_ = self.actions.create(body.type, args, p, "low", f"{meta['label']}: {body.subject}", preview,
                                 "permission", meta["reviewer"])
        return {"request_id": p_["id"], "status": "pending", "reviewer": "HR"}

    def _execute_case(self, requester: Principal, args: dict, approver: Principal) -> dict:
        t = args["type"]
        if t == "attendance_correction":
            n = self.s.db.one("SELECT COUNT(*) AS n FROM att_absences WHERE employee_id=? AND day=?",
                              (requester.id, args["day"]))["n"]
            self.s.db.execute("DELETE FROM att_absences WHERE employee_id=? AND day=?", (requester.id, args["day"]))
            return {"outcome": f"{n} absence record(s) on {args['day']} removed."}
        if t == "personal_details" and args.get("new_location"):
            self.s.db.execute("UPDATE hris_employees SET location=?, updated_at=? WHERE id=?",
                              (args["new_location"], self.today.isoformat(), requester.id))
            return {"outcome": f"Work location updated to {args['new_location']}."}
        if t == "employment_letter":
            return {"outcome": f"Letter HR-L-{requester.id}-{self.today.strftime('%Y%m%d')} issued to the employee."}
        return {"outcome": "Resolved by HR."}

    def decide_case(self, p: Principal, request_id: int, approve: bool, comment: str | None = None) -> dict:
        return self.actions.decide(p, request_id, approve, comment)

    # ================================================================ views
    def _name(self, eid: str | None) -> str | None:
        e = self.s.hris.get(eid) if eid else None
        return e["name"] if e else eid

    def _leave_item(self, r: dict) -> dict:
        return {"kind": "leave", "id": r["id"], "type": "leave", "type_label": f"{r['leave_type'].title()} leave",
                "title": f"{r['start_date']} to {r['end_date']} ({r['working_days']:g} working days)",
                "requester": {"id": r["employee_id"], "name": self._name(r["employee_id"])},
                "reviewer": self._name(r["approver_id"]), "status": r["status"], "submitted_at": r["submitted_at"],
                "reason": r["reason"], "handover_note": r["handover_note"],
                "flag": "Reason contains instruction-like text - possible manipulation attempt."
                if looks_like_injection(r["reason"]) else None,
                "decided_by": self._name(r["decided_by"]), "decision_comment": r["decision_comment"]}

    def _case_item(self, c: dict) -> dict:
        return {"kind": "case", "id": c["id"], "type": c["tool"], "type_label": REQUEST_LABELS.get(c["tool"], c["tool"]),
                "title": c["summary"], "requester": c["requester"], "reviewer": "HR systems administrator"
                if c["tool"] == "request_routing_fix" else "HR", "status": c["status"],
                "submitted_at": (c["created_at"] or "")[:10], "preview": c["preview"], "result": c["result"],
                "decided_by": self._name(c["decided_by"]), "decision_comment": c["comment"],
                "can_decide": c.get("can_decide", False), "flag": (c["preview"] or {}).get("flag")}

    def my_requests(self, p: Principal) -> dict:
        leave = [self._leave_item(r) for r in self.s.db.query(
            "SELECT * FROM leave_requests WHERE employee_id=? ORDER BY submitted_at DESC, id DESC", (p.id,))]
        all_props = self.actions.visible_to(p)
        drafts = [d for d in all_props if d["requester"]["id"] == p.id and d["status"] == "pending"
                  and d["awaiting"] == "requester confirmation"]
        cases = [self._case_item(c) for c in all_props if c["requester"]["id"] == p.id
                 and c["awaiting"] != "requester confirmation"]
        return {"drafts": drafts, "requests": sorted(leave + cases, key=lambda x: (x["submitted_at"] or ""), reverse=True)}

    def inbox(self, p: Principal) -> dict:
        if p.can("leave.review.all"):
            rows = self.s.db.query("SELECT * FROM leave_requests WHERE status='pending' ORDER BY start_date")
        else:
            rows = self.s.db.query("SELECT * FROM leave_requests WHERE status='pending' AND approver_id=? "
                                   "ORDER BY start_date", (p.id,))
        leave = []
        for r in rows:
            if not self._can_review_leave(p, r):
                continue
            item = self._leave_item(r)
            item["assessment"] = self.engine.assess_request(r["id"])
            item["as_override"] = r["approver_id"] != p.id
            leave.append(item)
        cases = [self._case_item(c) for c in self.actions.visible_to(p)
                 if c["can_decide"] and c["awaiting"] != "requester confirmation"]
        decided = [self._case_item(c) for c in self.actions.visible_to(p)
                   if c["requester"]["id"] != p.id and c["status"] in ("executed", "rejected", "failed")][:15]
        return {"leave": leave, "cases": cases, "recently_decided": decided}

    def overview(self, p: Principal) -> dict:
        """'My leave & attendance' page."""
        year = self.today.year
        bal = {lt: self.s.leave.balance(p.id, lt, year) for lt in ("annual", "sick")}
        upcoming = [self._leave_item(r) for r in self.s.db.query(
            "SELECT * FROM leave_requests WHERE employee_id=? AND end_date >= ? AND status IN ('pending','approved') "
            "ORDER BY start_date", (p.id, self.today.isoformat()))]
        since = self.today - timedelta(days=90)
        absences = self.s.db.query("SELECT day, kind, notice_hours FROM att_absences WHERE employee_id=? AND day >= ? "
                                   "ORDER BY day DESC", (p.id, since.isoformat()))
        working_days = len(working_days_between(since, self.today - timedelta(days=1)))
        emp = self.s.hris.get(p.id)
        return {"employee": {"name": emp["name"], "title": emp["title"], "department": emp["department"],
                             "team": emp["team"], "location": emp["location"], "manager": self._name(emp["manager_id"]),
                             "hire_date": emp["hire_date"]},
                "balances": bal, "year": year, "upcoming_leave": upcoming,
                "absences_last_90_days": absences, "working_days_in_period": working_days}

    def team(self, p: Principal) -> dict:
        require(p, "leave.read.team")
        members = []
        for e in self.s.hris.direct_reports(p.id):
            b = self.s.leave.balance(e["id"], "annual", self.today.year)
            nxt = self.s.db.one("SELECT start_date, end_date, status FROM leave_requests WHERE employee_id=? AND "
                                "end_date >= ? AND status IN ('pending','approved') ORDER BY start_date LIMIT 1",
                                (e["id"], self.today.isoformat()))
            members.append({"id": e["id"], "name": e["name"], "title": e["title"],
                            "annual_available": b["available"] if b else None,
                            "next_leave": f"{nxt['start_date']} to {nxt['end_date']} ({nxt['status']})" if nxt else "-"})
        return {"members": members}
