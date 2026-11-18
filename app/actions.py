"""Human-in-the-loop action proposals (persisted in SQLite).

The agent never performs a state-changing action directly unless the autonomy
policy explicitly allows it. Otherwise it creates a *proposal* that a human
decides on. Invariants enforced here, in code:

1. Segregation of duties - a high-risk proposal can never be approved by the
   person who requested it, and the LLM has no tool that approves anything.
2. Re-validation at execution time - the proposal is re-checked against the
   current state of the systems before it is committed.
3. Exactly-once - a proposal is claimed atomically (pending -> executing), so a
   double click or two approvers at once cannot execute it twice.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from collections.abc import Callable

from .audit import AuditLog
from .db import Database
from .security import AuthorizationError, Principal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ActionService:
    def __init__(self, db: Database, audit: AuditLog, resolve: Callable[[str], Principal]) -> None:
        self.db, self.audit, self.resolve = db, audit, resolve
        self._lock = threading.Lock()
        # tool name -> callable(requester_principal, args, approver_principal) -> result dict
        self._executors: dict[str, Callable[[Principal, dict, Principal], dict]] = {}

    def register_executor(self, tool: str, fn: Callable[[Principal, dict, Principal], dict]) -> None:
        self._executors[tool] = fn

    @staticmethod
    def _load(row: dict | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for k in ("args", "preview", "result"):
            out[k] = json.loads(out[k]) if out[k] else None
        return out

    def create(self, tool: str, args: dict, requester: Principal, risk: str, summary: str, preview: dict,
               approver_mode: str, approval_permission: str | None) -> dict:
        pid = self.db.execute(
            "INSERT INTO app_proposals (tool, args, requester_id, risk, summary, preview, approver_mode,"
            " approval_permission, status, created_at) VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
            (tool, json.dumps(args), requester.id, risk, summary, json.dumps(preview, default=str), approver_mode,
             approval_permission, _now()))
        p = self.get(pid)
        self.audit.record(requester.id, "proposal.created", proposal_id=pid, tool=tool, args=args, risk=risk,
                          awaiting=self.public(p)["awaiting"])
        return p

    def get(self, pid: int) -> dict | None:
        return self._load(self.db.one("SELECT * FROM app_proposals WHERE id = ?", (pid,)))

    def public(self, p: dict) -> dict:
        requester = self.db.one("SELECT name FROM hris_employees WHERE id = ?", (p["requester_id"],))
        return {"id": p["id"], "tool": p["tool"], "args": p["args"], "risk": p["risk"], "summary": p["summary"],
                "preview": p["preview"], "requester": {"id": p["requester_id"],
                                                       "name": requester["name"] if requester else p["requester_id"]},
                "awaiting": "requester confirmation" if p["approver_mode"] == "requester"
                else f"approver with '{p['approval_permission']}'",
                "status": p["status"], "created_at": p["created_at"], "decided_by": p["decided_by"],
                "decided_at": p["decided_at"], "comment": p["comment"], "result": p["result"]}

    def can_decide(self, principal: Principal, p: dict) -> bool:
        if p["status"] != "pending":
            return False
        if p["approver_mode"] == "requester":
            return principal.id == p["requester_id"]
        return bool(p["approval_permission"] and principal.can(p["approval_permission"])
                    and principal.id != p["requester_id"])  # segregation of duties

    def visible_to(self, principal: Principal) -> list[dict]:
        out = []
        for row in self.db.query("SELECT * FROM app_proposals ORDER BY id DESC LIMIT 100"):
            p = self._load(row)
            if p["requester_id"] == principal.id or (p["approval_permission"] and principal.can(p["approval_permission"])):
                d = self.public(p)
                d["can_decide"] = self.can_decide(principal, p)
                out.append(d)
        return out

    def decide(self, principal: Principal, pid: int, approve: bool, comment: str | None = None) -> dict:
        with self._lock:
            p = self.get(pid)
            if not p:
                raise KeyError(f"Proposal {pid} not found")
            if not self.can_decide(principal, p):
                self.audit.record(principal.id, "proposal.decision_denied", proposal_id=pid)
                raise AuthorizationError("You are not allowed to decide on this proposal "
                                         "(wrong approver, already decided, or you requested it yourself).")
            # Atomic claim: only one decision can move it out of 'pending'.
            self.db.execute(
                "UPDATE app_proposals SET status=?, decided_by=?, decided_at=?, comment=? WHERE id=? AND status='pending'",
                ("executing" if approve else "rejected", principal.id, _now(), comment, pid))
            if self.get(pid)["decided_by"] != principal.id:
                raise AuthorizationError("This proposal was decided by someone else in the meantime.")
        if not approve:
            self.audit.record(principal.id, "proposal.rejected", proposal_id=pid, comment=comment)
            return self.public(self.get(pid))
        try:
            result = self._executors[p["tool"]](self.resolve(p["requester_id"]), p["args"], principal)
            status = "executed"
            self.audit.record(principal.id, "proposal.executed", proposal_id=pid, tool=p["tool"],
                              on_behalf_of=p["requester_id"], result=result)
        except (ValueError, AuthorizationError) as exc:  # re-validation failed at execution time
            result, status = {"error": str(exc)}, "failed"
            self.audit.record(principal.id, "proposal.failed", proposal_id=pid, error=str(exc))
        self.db.execute("UPDATE app_proposals SET status=?, result=? WHERE id=?",
                        (status, json.dumps(result, default=str), pid))
        return self.public(self.get(pid))
