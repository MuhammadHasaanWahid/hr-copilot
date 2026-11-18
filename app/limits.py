"""Process-wide abuse and cost controls (OWASP LLM: unbounded consumption).

* Per-user sliding-window rate limit on assistant turns.
* A daily token budget shared by the whole process. When it is exhausted the
  assistant keeps working through the deterministic offline planner instead of
  calling the paid model - important for a public live demo.

Kept in memory on purpose: one process in the PoC. In production this moves to
Redis (shared across replicas) - the interface stays the same.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import date


class RateLimitExceeded(Exception):
    pass


class Limits:
    def __init__(self, per_minute: int, daily_tokens: int) -> None:
        self.per_minute, self.daily_tokens = per_minute, daily_tokens
        self._hits: dict[str, deque] = defaultdict(deque)
        self._day, self._used = date.today(), 0
        self._lock = threading.Lock()

    def check_rate(self, user_key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[user_key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                raise RateLimitExceeded(f"Too many requests: limit is {self.per_minute} per minute. Please wait a moment.")
            q.append(now)

    def _roll(self) -> None:
        if date.today() != self._day:
            self._day, self._used = date.today(), 0

    def llm_allowed(self) -> bool:
        with self._lock:
            self._roll()
            return self._used < self.daily_tokens

    def add_tokens(self, n: int) -> None:
        with self._lock:
            self._roll()
            self._used += n

    @property
    def used_today(self) -> int:
        return self._used


_shared: dict[tuple, Limits] = {}
_shared_lock = threading.Lock()


def shared_limits(per_minute: int, daily_tokens: int) -> Limits:
    """One Limits object per process, so per-visitor sandboxes share the daily budget."""
    with _shared_lock:
        key = (per_minute, daily_tokens)
        if key not in _shared:
            _shared[key] = Limits(per_minute, daily_tokens)
        return _shared[key]
