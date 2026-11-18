"""Server-side conversation sessions (persisted in SQLite).

A session is bound to exactly one identity: the session key is derived from the
authenticated user, and a stored session whose owner differs is never returned
(isolation). Sessions expire after `ttl_hours` of inactivity.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .db import Database


class SessionStore:
    def __init__(self, db: Database, ttl_hours: int) -> None:
        self.db, self.ttl = db, timedelta(hours=ttl_hours)

    def load(self, sid: str, owner: str) -> dict:
        row = self.db.one("SELECT * FROM app_sessions WHERE id = ?", (sid,))
        fresh = {"history": [], "state": {}, "tokens_used": 0}
        if not row or row["owner"] != owner:
            return fresh
        if datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"]) > self.ttl:
            self.delete(sid)
            return fresh
        return {"history": json.loads(row["history"]), "state": json.loads(row["state"]),
                "tokens_used": row["tokens_used"]}

    def save(self, sid: str, owner: str, history: list, state: dict, tokens_used: int) -> None:
        self.db.execute(
            "INSERT INTO app_sessions VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,"
            " history=excluded.history, state=excluded.state, tokens_used=excluded.tokens_used,"
            " updated_at=excluded.updated_at",
            (sid, owner, json.dumps(history, default=str), json.dumps(state, default=str), tokens_used,
             datetime.now(timezone.utc).isoformat()))

    def delete(self, sid: str) -> None:
        self.db.execute("DELETE FROM app_sessions WHERE id = ?", (sid,))
