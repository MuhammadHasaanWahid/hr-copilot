# Runbook

## Run

| What | Command |
|---|---|
| UI (in-process backend) | `python -m streamlit run frontend/streamlit_app.py` |
| REST API | `python -m uvicorn app.main:app --port 8000` (docs at `/docs`) |
| UI against a separate API | `API_URL=http://localhost:8000` then run the UI |
| Tests | `python -m pytest -q` |
| Offline evals (free) | `python evals/run_evals.py --mode offline` |
| Live evals (Gemini free tier) | `python evals/run_evals.py --mode live --pause 20` |
| Check Gemini key and models | `python scripts/check_gemini.py` |
| Scale test | `python scripts/scale_test.py --sizes 5000 50000` |
| Lint / security | `python -m ruff check .` · `python -m bandit -r app frontend -c pyproject.toml` · `python -m pip_audit -r requirements.txt` |

## Configuration

All settings are environment variables, from `.env` locally or Secrets on Streamlit Cloud. See `.env.example`. The important ones:
- `GEMINI_API_KEY` (or `ANTHROPIC_API_KEY`)
- `HR_LLM_PROVIDER` (gemini | anthropic | bedrock)
- `HR_GEMINI_MODEL`, `HR_GEMINI_FALLBACK_MODELS`
- `HR_AUTONOMY` (advisory | assisted | autonomous)
- `APP_ENV` (demo | prod)
- `JWT_SECRET`
- Budgets: `RATE_LIMIT_PER_MINUTE`, `SESSION_TOKEN_BUDGET`, `DAILY_TOKEN_BUDGET`

## Health and monitoring

- `GET /health`: mode, provider, autonomy, business date, audit chain validity.
- `GET /ready`: database reachable.
- `GET /metrics`: Prometheus format. Turns, tokens, cost, denials, guard triggers (citation / grounding / action-claim), latency.
- Logs: JSON lines on stdout with `request_id`, employee ID, role, category, outcome, latency. No message text or names; emails and API keys are scrubbed.

**Alert on:**
- Any audit chain failure.
- Guard trigger rate above the weekly baseline.
- Authorisation denials spiking for one user.
- Circuit-breaker opens.
- Daily token budget at 80%.

## Incidents

| Symptom | Action |
|---|---|
| Answers say "answered by the rule-based fallback" | Free-tier quota exhausted on all models, provider outage, open circuit breaker (retries after 60 s) or budget reached. Check `/health` and logs for `llm_unavailable` / `gemini_model_skipped`. Service continues in offline mode. |
| `API key rejected` | Rotate the key in `.env` / Secrets, restart. |
| Audit chain invalid | Treat as a security incident: freeze the database file, compare with backups, investigate access. |
| Suspected prompt-injection abuse | Search the audit for `tool.denied` / `proposal.decision_denied` by user; no write can bypass approvals. |
| Costs higher than expected | Lower `HR_EFFORT`, `DAILY_TOKEN_BUDGET`, or set `DEMO_PASSCODE`; check cost per turn on the Value tab. |

## Key rotation

1. Create a new key in Google AI Studio (or the Anthropic console).
2. Update `.env` / Streamlit Secrets.
3. Restart (Streamlit Cloud: Reboot app).
4. Revoke the old key.

Changing `JWT_SECRET` invalidates all sessions, by design.

## Data reset (demo only)

The **Reset data** button, or `POST /api/reset`, re-seeds every synthetic system for today's date. Disabled when `APP_ENV=prod`.
