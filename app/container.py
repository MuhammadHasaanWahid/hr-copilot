"""Composition root: builds and wires every component once."""
from __future__ import annotations

from .actions import ActionService
from .analytics import AbsenceAnalyzer, ConsistencyChecker, TeamConflictAnalyzer
from .audit import AuditLog
from .config import Settings
from .limits import shared_limits
from .metrics import Metrics
from .policy.engine import LeavePolicyEngine
from .policy.knowledge import PolicyKnowledgeBase
from .requests import RequestService
from .security import Principal
from .seed import open_database
from .sessions import SessionStore
from .systems import Systems
from .tools import Ctx, ToolGateway

DEMO_PERSONAS = [
    ("E1001", "Employee - wants two weeks off next month"),
    ("E0102", "Manager - three overlapping leave requests"),
    ("E0501", "HR Business Partner - absence trends"),
    ("E1010", "Employee - changed department, approvals going to old manager"),
    ("E0502", "HR Systems Admin - approves high-risk fixes"),
]


class Container:
    def __init__(self, settings: Settings | None = None, db_path: str | None = None) -> None:
        self.settings = settings or Settings()
        path = db_path or self.settings.db_path
        self.db, self.scenario, seeded = open_database(path, self.settings.today)
        # The simulated world has a business date: the day it was seeded. Keeping it
        # fixed means a persisted demo stays internally consistent across restarts.
        self.settings.today = seeded
        self.systems = Systems(self.db)
        self.kb = PolicyKnowledgeBase()
        self.engine = LeavePolicyEngine(self.systems, self.settings.today)
        self.conflicts = TeamConflictAnalyzer(self.systems, self.engine, self.settings.today)
        self.absences = AbsenceAnalyzer(self.systems, self.settings.today, self.settings.min_group_size)
        self.consistency = ConsistencyChecker(self.systems, self.settings.today)
        self.audit = AuditLog(self.db)
        self.actions = ActionService(self.db, self.audit, self.principal)
        self.requests = RequestService(self.systems, self.engine, self.actions, self.audit, self.settings.today)
        self.metrics = Metrics(self.db, self.settings)
        self.sessions = SessionStore(self.db, self.settings.session_ttl_hours)
        self.limits = shared_limits(self.settings.rate_limit_per_minute, self.settings.daily_token_budget)
        self.gateway = ToolGateway(self.ctx, self.actions, self.audit, self.settings)
        self._names = [r["name"] for r in self.db.query("SELECT name FROM hris_employees")]

    def principal(self, employee_id: str) -> Principal:
        """Resolve identity + role from HRIS (the source of truth), never from the client."""
        e = self.systems.hris.get(employee_id)
        if not e:
            raise KeyError(employee_id)
        reports = frozenset(r["id"] for r in self.systems.hris.direct_reports(employee_id))
        return Principal(e["id"], e["name"], e["role"], e["department"], e["team"], reports)

    def ctx(self, principal: Principal) -> Ctx:
        return Ctx(principal, self.systems, self.engine, self.conflicts, self.absences, self.consistency,
                   self.kb, self.settings, self.requests)

    @property
    def employee_names(self) -> list[str]:
        return self._names

    def personas(self) -> list[dict]:
        out = []
        for eid, story in DEMO_PERSONAS:
            e = self.systems.hris.get(eid)
            out.append({"id": eid, "name": e["name"], "title": e["title"], "role": e["role"], "story": story})
        return out
