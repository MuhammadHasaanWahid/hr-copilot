"""Check the Gemini setup: list available Flash models and ping the configured ones.

    python scripts/check_gemini.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google.genai import errors  # noqa: E402

from app.agent.gemini_agent import make_gemini_client  # noqa: E402
from app.config import Settings  # noqa: E402


def main() -> int:
    s = Settings()
    if not s.api_key_present:
        print("GEMINI_API_KEY is not set (.env).")
        return 2
    client = make_gemini_client()
    names = sorted(m.name.removeprefix("models/") for m in client.models.list()
                   if "flash" in (m.name or "") and "generateContent" in (m.supported_actions or []))
    print("Flash models visible to this key:\n  " + "\n  ".join(names))
    for model in s.gemini_models:
        try:
            r = client.models.generate_content(model=model, contents="Reply with the single word: ok")
            print(f"OK   {model}: {(r.text or '').strip()[:20]!r}")
        except errors.APIError as exc:
            print(f"FAIL {model}: {exc.code} {exc.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
