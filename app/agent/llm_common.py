"""Pieces shared by every LLM planner (Gemini, Claude)."""
from __future__ import annotations

import threading
import time


class LLMUnavailable(Exception):
    """The model provider cannot serve this turn (outage, quota, bad key, open breaker)."""


class CircuitBreaker:
    """After `threshold` consecutive failures, stop calling the provider for `cooldown` seconds."""

    def __init__(self, threshold: int = 3, cooldown: float = 60.0) -> None:
        self.threshold, self.cooldown = threshold, cooldown
        self.failures, self.open_until = 0, 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        return time.monotonic() >= self.open_until

    def success(self) -> None:
        with self._lock:
            self.failures = 0

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.open_until = time.monotonic() + self.cooldown
                self.failures = 0
