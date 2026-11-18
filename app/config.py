"""Runtime configuration, driven by environment variables (and an optional
`.env` file in the project root, which is never committed).

The same build can run as a read-only advisor in one business unit and with
more autonomy in another (see the 'autonomy dial' in README)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

AUTONOMY_LEVELS = ("advisory", "assisted", "autonomous")
ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader (KEY=VALUE lines). Real environment variables win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():  # -sig: tolerate a BOM (PowerShell/Notepad)
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in ('"', "'") and value[1:].find(value[0]) >= 0:   # quoted value: keep '#' inside quotes
            value = value[1:1 + value[1:].find(value[0])]
        elif value.startswith("#"):
            value = ""
        else:
            value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()  # strip inline comment
        key = key.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


load_dotenv()


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def _today() -> date:
    raw = os.getenv("HR_DEMO_TODAY", "").strip()
    return date.fromisoformat(raw) if raw else date.today()


@dataclass
class Settings:
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "demo").lower())       # demo | prod
    llm_mode: str = field(default_factory=lambda: _env("HR_LLM_MODE", "auto").lower())   # auto | llm | offline
    # gemini (default: free tier for the demo) | anthropic | bedrock (enterprise paths)
    llm_provider: str = field(default_factory=lambda: _env("HR_LLM_PROVIDER", "gemini").lower())
    gemini_model: str = field(default_factory=lambda: _env("HR_GEMINI_MODEL", "gemini-3.5-flash-lite"))
    # Tried in order when the primary model is rate-limited / out of free quota / unavailable.
    gemini_fallback_models: str = field(default_factory=lambda: _env("HR_GEMINI_FALLBACK_MODELS",
                                                                     "gemini-3.1-flash-lite,gemini-2.5-flash"))
    claude_model: str = field(default_factory=lambda: _env("HR_CLAUDE_MODEL", _env("HR_MODEL", "claude-opus-5-5")))
    effort: str = field(default_factory=lambda: _env("HR_EFFORT", "medium"))
    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", "eu-west-1"))
    autonomy: str = field(default_factory=lambda: _env("HR_AUTONOMY", "assisted").lower())
    today: date = field(default_factory=_today)
    db_path: str = field(default_factory=lambda: _env("HR_DB_PATH", str(ROOT / "data" / "hr_copilot.db")))
    jwt_secret: str = field(default_factory=lambda: os.getenv("JWT_SECRET", "").strip())
    jwt_ttl_minutes: int = field(default_factory=lambda: int(_env("JWT_TTL_MINUTES", "480")))
    demo_passcode: str = field(default_factory=lambda: os.getenv("DEMO_PASSCODE", "").strip())
    cors_origins: str = field(default_factory=lambda: _env("CORS_ORIGINS", "http://localhost:8501"))
    # Abuse / cost controls
    rate_limit_per_minute: int = field(default_factory=lambda: int(_env("RATE_LIMIT_PER_MINUTE", "12")))
    session_token_budget: int = field(default_factory=lambda: int(_env("SESSION_TOKEN_BUDGET", "300000")))
    daily_token_budget: int = field(default_factory=lambda: int(_env("DAILY_TOKEN_BUDGET", "3000000")))
    session_ttl_hours: int = 8
    max_agent_steps: int = 10
    # USD per million tokens for the cost/value dashboard. Defaults by provider (Gemini free tier = 0;
    # Claude Opus 5.5 list price = 4 / 20); override with HR_PRICE_IN / HR_PRICE_OUT.
    price_input_per_mtok: float = -1.0
    price_output_per_mtok: float = -1.0
    # Minimum group size for any aggregate people analytics (k-anonymity).
    min_group_size: int = 5

    def __post_init__(self) -> None:
        if self.autonomy not in AUTONOMY_LEVELS:
            raise ValueError(f"HR_AUTONOMY must be one of {AUTONOMY_LEVELS}")
        if self.llm_provider not in ("gemini", "anthropic", "bedrock"):
            raise ValueError("HR_LLM_PROVIDER must be gemini, anthropic or bedrock")
        if not self.jwt_secret:
            if self.app_env == "prod":
                raise ValueError("JWT_SECRET must be set when APP_ENV=prod")
            self.jwt_secret = secrets.token_urlsafe(32)  # demo: per-process secret
        default_prices = {"gemini": (0.0, 0.0), "anthropic": (4.0, 20.0), "bedrock": (4.0, 20.0)}[self.llm_provider]
        if self.price_input_per_mtok < 0:
            self.price_input_per_mtok = float(os.getenv("HR_PRICE_IN", default_prices[0]))
        if self.price_output_per_mtok < 0:
            self.price_output_per_mtok = float(os.getenv("HR_PRICE_OUT", default_prices[1]))

    @property
    def is_demo(self) -> bool:
        return self.app_env != "prod"

    @property
    def model(self) -> str:
        return self.gemini_model if self.llm_provider == "gemini" else self.claude_model

    @property
    def gemini_models(self) -> list[str]:
        extra = [m.strip() for m in self.gemini_fallback_models.split(",") if m.strip()]
        return [self.gemini_model] + [m for m in extra if m != self.gemini_model]

    @property
    def api_key_present(self) -> bool:
        if self.llm_provider == "gemini":
            return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
        if self.llm_provider == "anthropic":
            return bool(os.getenv("ANTHROPIC_API_KEY"))
        return True  # bedrock: AWS credential chain

    @property
    def use_llm(self) -> bool:
        if self.llm_mode == "offline":
            return False
        return self.llm_mode in ("llm", "claude", "gemini") or self.api_key_present


# Assumptions behind the "business value" dashboard: minutes of human effort a
# request of each kind costs today (HR ticket handling, manager analysis, ...).
# These are deliberately explicit and editable - finance should challenge them.
MANUAL_EFFORT_MINUTES = {
    "leave_planning": 15,
    "team_conflict_analysis": 45,
    "absence_analytics": 240,
    "routing_diagnosis": 35,
    "policy_question": 10,
    "profile_lookup": 5,
    "other": 5,
}
