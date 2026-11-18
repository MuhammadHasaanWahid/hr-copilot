"""Append-only, hash-chained audit log (persisted in SQLite).

Every tool call, authorisation decision, proposal, approval and assistant turn
is recorded. Each entry contains the hash of the previous one, so any
after-the-fact edit breaks the chain and `verify()` detects it. In production
this would also be shipped to a WORM store / SIEM; the interface stays the same.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone

from .db import Database


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.Lock()

    @staticmethod
    def _hash(entry: dict) -> str:
        body = {k: entry[k] for k in ("seq", "ts", "actor", "event", "details", "prev_hash")}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _row(r: dict) -> dict:
        return dict(r, details=json.loads(r["details"]))

    def record(self, actor: str, event: str, **details) -> dict:
        with self._lock:
            last = self.db.one("SELECT seq, hash FROM app_audit ORDER BY seq DESC LIMIT 1")
            entry = {
                "seq": (last["seq"] + 1) if last else 1,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "actor": actor, "event": event,
                "details": json.loads(json.dumps(details, default=str)),
                "prev_hash": last["hash"] if last else "GENESIS",
            }
            entry["hash"] = self._hash(entry)
            self.db.execute("INSERT INTO app_audit VALUES (?,?,?,?,?,?,?)",
                            (entry["seq"], entry["ts"], actor, event, json.dumps(entry["details"], sort_keys=True),
                             entry["prev_hash"], entry["hash"]))
            return entry

    def entries(self, actor: str | None = None, limit: int = 200) -> list[dict]:
        if actor is None:
            rows = self.db.query("SELECT * FROM app_audit ORDER BY seq DESC LIMIT ?", (limit,))
        else:
            rows = self.db.query(
                "SELECT * FROM app_audit WHERE actor = ? OR json_extract(details, '$.on_behalf_of') = ?"
                " ORDER BY seq DESC LIMIT ?", (actor, actor, limit))
        return [self._row(r) for r in reversed(rows)]

    def count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM app_audit")["n"]

    def verify(self) -> bool:
        prev = "GENESIS"
        for r in self.db.query("SELECT * FROM app_audit ORDER BY seq"):
            e = self._row(r)
            if e["prev_hash"] != prev or self._hash(e) != e["hash"]:
                return False
            prev = e["hash"]
        return True
