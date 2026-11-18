import os
import sys
from datetime import date
from pathlib import Path

import pytest

# Deterministic, offline, in-memory test environment (set before the app is imported).
os.environ["HR_DEMO_TODAY"] = "2026-11-18"
os.environ["HR_LLM_MODE"] = "offline"
os.environ["HR_DB_PATH"] = ":memory:"
os.environ["RATE_LIMIT_PER_MINUTE"] = "1000"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.container import Container  # noqa: E402

TODAY = date(2026, 11, 18)


def make(autonomy: str = "assisted") -> Container:
    return Container(Settings(llm_mode="offline", today=TODAY, autonomy=autonomy), db_path=":memory:")


@pytest.fixture
def c() -> Container:
    return make()


@pytest.fixture
def call(c):
    def _call(user: str, tool: str, args: dict | None = None) -> dict:
        return c.gateway.call(c.principal(user), tool, args or {})
    return _call
