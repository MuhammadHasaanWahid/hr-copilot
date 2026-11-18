"""Thin thread-safe SQLite wrapper.

The PoC keeps all simulated enterprise systems in one in-memory SQLite database,
but each system owns its own tables (hris_*, leave_*, wf_*, proj_*, att_*) and is
only ever accessed through its own connector in app/systems/. That boundary is
what would become a real network integration in production.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any
from collections.abc import Iterable


class Database:
    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # Autocommit mode: each statement commits on its own unless wrapped in
        # an explicit transaction() block (BEGIN ... COMMIT/ROLLBACK).
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()

    def has_table(self, name: str) -> bool:
        return bool(self.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))

    def close(self) -> None:
        self._conn.close()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return cur.lastrowid

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, [tuple(r) for r in rows])

    def script(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)

    def transaction(self):
        """Context manager: `with db.transaction(): ...` commits or rolls back atomically."""
        return _Tx(self)


class _Tx:
    def __init__(self, db: Database) -> None:
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db._conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        finally:
            self.db._lock.release()
        return False


SCHEMA = """
-- ===== HRIS (system of record for people & reporting lines) =====
CREATE TABLE hris_departments (name TEXT PRIMARY KEY, head_id TEXT);
CREATE TABLE hris_employees (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, title TEXT,
    department TEXT NOT NULL, team TEXT, location TEXT, role TEXT NOT NULL,
    manager_id TEXT, hire_date TEXT, department_changed_on TEXT,
    salary_annual INTEGER, updated_at TEXT
);

-- ===== Leave management system =====
CREATE TABLE leave_balances (
    employee_id TEXT, leave_type TEXT, year INTEGER,
    entitled REAL, used REAL, carried_over REAL,
    PRIMARY KEY (employee_id, leave_type, year)
);
CREATE TABLE leave_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id TEXT, leave_type TEXT,
    start_date TEXT, end_date TEXT, working_days REAL, status TEXT,
    approver_id TEXT, submitted_at TEXT, reason TEXT, handover_note TEXT,
    decided_by TEXT, decided_at TEXT, decision_comment TEXT
);

-- ===== Workflow / approval engine (separate system, synced from HRIS) =====
CREATE TABLE wf_approval_routes (
    employee_id TEXT PRIMARY KEY, approver_id TEXT, updated_at TEXT, source TEXT
);

-- ===== Project management system =====
CREATE TABLE proj_projects (
    id TEXT PRIMARY KEY, name TEXT, department TEXT, critical INTEGER,
    deadline TEXT, lead_id TEXT
);
CREATE TABLE proj_assignments (
    project_id TEXT, employee_id TEXT, project_role TEXT, allocation REAL,
    critical_role INTEGER, PRIMARY KEY (project_id, employee_id)
);

-- ===== Time & attendance =====
CREATE TABLE att_absences (
    id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id TEXT, day TEXT,
    kind TEXT, notice_hours REAL
);
CREATE INDEX idx_abs_day ON att_absences(day);
CREATE INDEX idx_abs_emp ON att_absences(employee_id);
CREATE INDEX idx_emp_mgr ON hris_employees(manager_id);
CREATE INDEX idx_req_emp ON leave_requests(employee_id);
CREATE INDEX idx_asg_emp ON proj_assignments(employee_id);
"""

# Application state owned by HR Copilot itself (not a simulated enterprise system).
APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS app_audit (
    seq INTEGER PRIMARY KEY, ts TEXT, actor TEXT, event TEXT, details TEXT,
    prev_hash TEXT, hash TEXT
);
CREATE TABLE IF NOT EXISTS app_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT, args TEXT, requester_id TEXT, risk TEXT,
    summary TEXT, preview TEXT, approver_mode TEXT, approval_permission TEXT, status TEXT,
    created_at TEXT, decided_by TEXT, decided_at TEXT, comment TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS app_sessions (
    id TEXT PRIMARY KEY, owner TEXT, history TEXT, state TEXT, tokens_used INTEGER DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS app_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id TEXT, mode TEXT, category TEXT,
    outcome TEXT, tools TEXT, denials INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    latency_ms INTEGER, invalid_citations INTEGER, unsupported_claims INTEGER, false_action_claims INTEGER
);
"""
