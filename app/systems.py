"""Connectors to the (simulated) enterprise systems.

Each class is the only code allowed to touch its system's tables. In production
each becomes an adapter over a real API (Workday/SAP SuccessFactors HRIS, a
leave/T&A product, ServiceNow/Camunda workflow, Jira/Asana projects...). The
rest of the application depends only on these interfaces, which is what keeps
the AI layer replaceable and the integrations independently maintainable.
"""
from __future__ import annotations

from datetime import date

from .db import Database


class HRIS:
    """System of record for people and reporting lines."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, employee_id: str) -> dict | None:
        return self.db.one("SELECT * FROM hris_employees WHERE id = ?", (employee_id,))

    def all(self) -> list[dict]:
        return self.db.query("SELECT * FROM hris_employees ORDER BY id")

    def direct_reports(self, manager_id: str) -> list[dict]:
        return self.db.query("SELECT * FROM hris_employees WHERE manager_id = ? ORDER BY id", (manager_id,))

    def department_head(self, department: str) -> dict | None:
        row = self.db.one("SELECT head_id FROM hris_departments WHERE name = ?", (department,))
        return self.get(row["head_id"]) if row else None

    def find_by_name(self, fragment: str) -> list[dict]:
        return self.db.query("SELECT * FROM hris_employees WHERE lower(name) LIKE ?", (f"%{fragment.lower()}%",))


class LeaveSystem:
    def __init__(self, db: Database) -> None:
        self.db = db

    def balance(self, employee_id: str, leave_type: str, year: int) -> dict | None:
        row = self.db.one(
            "SELECT * FROM leave_balances WHERE employee_id=? AND leave_type=? AND year=?",
            (employee_id, leave_type, year))
        if not row:
            return None
        pending = self.db.one(
            "SELECT COALESCE(SUM(working_days),0) AS d FROM leave_requests WHERE employee_id=? AND leave_type=?"
            " AND status='pending' AND substr(start_date,1,4)=?", (employee_id, leave_type, str(year)))["d"]
        row["pending"] = pending
        row["available"] = row["entitled"] + row["carried_over"] - row["used"] - pending
        return row

    def requests(self, employee_ids: list[str] | None = None, statuses: tuple[str, ...] = ("pending", "approved"),
                 start: date | None = None, end: date | None = None) -> list[dict]:
        sql = "SELECT * FROM leave_requests WHERE status IN (%s)" % ",".join("?" * len(statuses))
        params: list = list(statuses)
        if employee_ids is not None:
            sql += " AND employee_id IN (%s)" % ",".join("?" * len(employee_ids)) if employee_ids else " AND 0"
            params += employee_ids
        if start and end:
            sql += " AND start_date <= ? AND end_date >= ?"
            params += [end.isoformat(), start.isoformat()]
        return self.db.query(sql + " ORDER BY start_date, submitted_at", params)

    def create_request(self, employee_id: str, leave_type: str, start: date, end: date, working_days: int,
                       approver_id: str, submitted_at: date, reason: str, handover_note: str | None) -> int:
        return self.db.execute(
            "INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, working_days, status,"
            " approver_id, submitted_at, reason, handover_note) VALUES (?,?,?,?,?, 'pending', ?,?,?,?)",
            (employee_id, leave_type, start.isoformat(), end.isoformat(), working_days, approver_id,
             submitted_at.isoformat(), reason, handover_note))

    def reassign_pending(self, employee_id: str, new_approver: str) -> int:
        rows = self.db.query("SELECT id FROM leave_requests WHERE employee_id=? AND status='pending'"
                             " AND approver_id != ?", (employee_id, new_approver))
        self.db.execute("UPDATE leave_requests SET approver_id=? WHERE employee_id=? AND status='pending'",
                        (new_approver, employee_id))
        return len(rows)


class WorkflowEngine:
    """Approval routing. A *separate* system that is supposed to sync from HRIS."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def route(self, employee_id: str) -> dict | None:
        return self.db.one("SELECT * FROM wf_approval_routes WHERE employee_id = ?", (employee_id,))

    def all_routes(self) -> list[dict]:
        return self.db.query("SELECT * FROM wf_approval_routes")

    def set_route(self, employee_id: str, approver_id: str, when: date, source: str) -> None:
        self.db.execute(
            "INSERT INTO wf_approval_routes VALUES (?,?,?,?) ON CONFLICT(employee_id) DO UPDATE SET"
            " approver_id=excluded.approver_id, updated_at=excluded.updated_at, source=excluded.source",
            (employee_id, approver_id, when.isoformat(), source))


class ProjectSystem:
    def __init__(self, db: Database) -> None:
        self.db = db

    def assignments_for(self, employee_ids: list[str]) -> list[dict]:
        if not employee_ids:
            return []
        return self.db.query(
            "SELECT a.*, p.name AS project_name, p.critical, p.deadline, p.department AS project_department"
            " FROM proj_assignments a JOIN proj_projects p ON p.id = a.project_id"
            " WHERE a.employee_id IN (%s) ORDER BY p.deadline" % ",".join("?" * len(employee_ids)), employee_ids)


    def critical_assignees(self, start: date, end: date) -> set[str]:
        """Employees holding any assignment on a critical project with a deadline in [start, end]."""
        rows = self.db.query(
            "SELECT DISTINCT a.employee_id FROM proj_assignments a JOIN proj_projects p ON p.id = a.project_id"
            " WHERE p.critical = 1 AND p.deadline BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))
        return {r["employee_id"] for r in rows}


class Attendance:
    def __init__(self, db: Database) -> None:
        self.db = db

    def absences(self, start: date, end: date) -> list[dict]:
        return self.db.query("SELECT * FROM att_absences WHERE day BETWEEN ? AND ?",
                             (start.isoformat(), end.isoformat()))


class Systems:
    """Bundle of connectors handed to tools (dependency injection)."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.hris = HRIS(db)
        self.leave = LeaveSystem(db)
        self.workflow = WorkflowEngine(db)
        self.projects = ProjectSystem(db)
        self.attendance = Attendance(db)
