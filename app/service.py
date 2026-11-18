"""Application service layer: the single entry point used by every frontend.

FastAPI (`app/main.py`) exposes it over HTTP; the Streamlit app can call it over
HTTP (API_URL set) or in-process (a self-contained live demo). Either way every
call authenticates with a JWT and goes through the same orchestrator, gateway,
guards and audit - the UI holds no business logic and no authority.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from . import auth
from .agent.orchestrator import Orchestrator
from .config import Settings
from .container import DEMO_PERSONAS, Container
from .security import AuthorizationError, Principal


class HRCopilotService:
    def __init__(self, settings: Settings | None = None, db_path: str | None = None) -> None:
        self.settings = settings or Settings()
        self._db_path = db_path or self.settings.db_path
        self._lock = threading.Lock()
        self._build()

    def _build(self) -> None:
        self.c = Container(self.settings, self._db_path)
        self.orchestrator = Orchestrator(self.c)

    # ------------------------------------------------------------------ auth
    def login(self, employee_id: str, passcode: str | None = None) -> dict:
        if not self.settings.is_demo:
            raise auth.AuthError("Password login is disabled in production; sign in through the company SSO.")
        auth.check_passcode(self.settings, passcode)
        try:
            p = self.c.principal(employee_id.strip().upper())
        except KeyError:
            raise auth.AuthError("Unknown employee ID.") from None
        self.c.audit.record(p.id, "auth.login", method="demo")
        return {"token": auth.issue(self.settings, p.id), "user": self.whoami(p)}

    def principal(self, token: str) -> Principal:
        employee_id = auth.verify(self.settings, token)
        try:
            return self.c.principal(employee_id)
        except KeyError:
            raise auth.AuthError("User no longer exists.") from None

    @staticmethod
    def whoami(p: Principal) -> dict:
        return {"id": p.id, "name": p.name, "role": p.role, "department": p.department, "team": p.team,
                "direct_reports": len(p.direct_reports), "permissions": sorted(p.permissions)}

    # ------------------------------------------------------------- features
    def personas(self) -> dict:
        sc = self.c.scenario
        return {"personas": self.c.personas(), "passcode_required": bool(self.settings.demo_passcode),
                "scenario": {"atlas_deadline": sc.atlas_deadline.isoformat(),
                             "next_month_start": sc.next_month_start.isoformat()}}

    def health(self) -> dict:
        return {"status": "ok", "mode": self.orchestrator.mode,
                "provider": self.settings.llm_provider if self.orchestrator.llm else None,
                "model": self.orchestrator.llm.model if self.orchestrator.llm else None,
                "autonomy": self.settings.autonomy, "env": self.settings.app_env,
                "business_date": self.settings.today.isoformat(), "audit_chain_valid": self.c.audit.verify()}

    def ready(self) -> dict:
        self.c.db.one("SELECT 1 AS ok")
        return {"ready": True}

    def chat(self, p: Principal, session_id: str, message: str) -> dict:
        return self.orchestrator.chat(p, session_id, message)

    def new_chat(self, p: Principal, session_id: str) -> None:
        self.orchestrator.reset_session(p, session_id)

    def actions(self, p: Principal) -> list[dict]:
        return self.c.actions.visible_to(p)

    def decide(self, p: Principal, proposal_id: int, approve: bool, comment: str | None = None) -> dict:
        return self.c.actions.decide(p, proposal_id, approve, comment)

    # ----------------------------------------------------------- requests
    def request_types(self) -> dict:
        from .requests import CASE_TYPES
        return {k: {"label": v["label"], "help": v["help"]} for k, v in CASE_TYPES.items() if k != "request_routing_fix"}

    def my_requests(self, p: Principal) -> dict:
        return self.c.requests.my_requests(p)

    def check_leave(self, p: Principal, payload: dict) -> dict:
        from .requests import LeaveIn
        return self.c.requests.check_leave(p, LeaveIn(**payload))

    def submit_leave(self, p: Principal, payload: dict) -> dict:
        from .requests import LeaveIn
        return self.c.requests.submit_leave(p, LeaveIn(**payload))

    def submit_case(self, p: Principal, payload: dict) -> dict:
        from .requests import CaseIn
        return self.c.requests.submit_case(p, CaseIn(**payload))

    def inbox(self, p: Principal) -> dict:
        return self.c.requests.inbox(p)

    def decide_leave(self, p: Principal, request_id: int, approve: bool, comment: str | None = None) -> dict:
        return self.c.requests.decide_leave(p, request_id, approve, comment)

    def overview(self, p: Principal) -> dict:
        return self.c.requests.overview(p)

    def team(self, p: Principal) -> dict:
        out = self.c.requests.team(p)
        plan = self.c.gateway.call(p, "analyze_team_leave_conflicts", {})
        out["coverage_plan"] = plan.get("data") if plan["status"] == "ok" else None
        return out

    def insights(self, p: Principal) -> dict:
        """HR dashboard: absence analytics + data consistency, through the gateway (authorised and audited)."""
        a = self.c.gateway.call(p, "analyze_absence_patterns", {})
        s = self.c.gateway.call(p, "scan_data_consistency", {})
        if a["status"] != "ok":
            raise AuthorizationError(a.get("error", "Not authorised"))
        return {"absences": a["data"], "consistency": s.get("data")}

    def raise_routing_fix(self, p: Principal, employee_id: str) -> dict:
        r = self.c.gateway.call(p, "request_routing_fix", {"employee_id": employee_id})
        if r["status"] == "denied":
            raise AuthorizationError(r["error"])
        if r["status"] == "error":
            raise ValueError(r["error"])
        return r

    def audit(self, p: Principal, limit: int = 200) -> dict:
        # HR admins see the full log; everyone else only their own events / events on their behalf.
        rows = self.c.audit.entries(limit=limit) if p.can("audit.read.all") else self.c.audit.entries(p.id, limit)
        return {"chain_valid": self.c.audit.verify(), "total_entries": self.c.audit.count(), "entries": rows[::-1]}

    def metrics(self) -> dict:
        m = self.c.metrics.snapshot()
        m["daily_tokens_used"] = self.c.limits.used_today
        m["daily_token_budget"] = self.settings.daily_token_budget
        return m

    def reset(self, p: Principal | None = None) -> None:
        """Demo only: re-seed every simulated system for today's date."""
        if not self.settings.is_demo:
            raise AuthorizationError("Reset is disabled outside demo mode.")
        with self._lock:
            self.c.db.close()
            if self._db_path != ":memory:":
                for suffix in ("", "-wal", "-shm"):
                    f = Path(self._db_path + suffix)
                    if f.exists():
                        os.remove(f)
            self.settings.today = Settings().today
            self._build()


__all__ = ["HRCopilotService", "DEMO_PERSONAS"]
