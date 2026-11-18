# Security

**Design principle:** assume the model will sometimes be wrong or manipulated, and make that harmless. Authority lives in code paths the model cannot change.

## Threat model (what can go wrong → control → proof)

| Threat | Control | Proof |
|---|---|---|
| User reads data outside their role | Gateway: permission + data scope (self / direct reports / org) resolved from HRIS; salary redacted unless `compensation.read` | `tests/test_security_and_governance.py`, evals A1–A6 |
| Name lookup leaks who exists | `find_employee` silently drops people outside scope | test + eval A1 |
| Forged identity | JWT (HS256, `exp`/`iat`/`iss` required); the old `X-User-Id` header is ignored | `test_http_api_with_jwt`, `test_expired_and_tampered_tokens_rejected` |
| Prompt injection in user text | No tool can approve or change routing arbitrarily; high-risk writes always need another human | evals I1, I2, I4 |
| Indirect injection (employee-typed text in data) | Text wrapped in `[EMPLOYEE_TEXT]` markers, control/bidi chars stripped, length capped; system prompt treats it as data; seeded attack in a leave reason | `test_untrusted_text_is_spotlighted`, eval S2, I3 |
| Excessive agency | Risk tiers + autonomy dial; high risk never auto-executes; segregation of duties; LLM has no approve tool | governance tests |
| Approval race / double execution | Atomic claim (`pending → executing`) inside a lock; re-validation against live state before commit | `test_proposal_is_revalidated_at_execution_time` |
| Hallucinated facts or "done" claims | Grounding, action-claim and citation guards; one self-correction round, then a visible warning | `test_claude_loop.py`, eval "verified" column |
| Tampering with history | Hash-chained audit log; `verify()` detects edits | `test_every_call_is_audited_and_chain_is_tamper_evident` |
| Cost abuse / denial of wallet | Per-user rate limit, per-session token budget, daily token budget, circuit breaker; offline fallback | `app/limits.py`, `test_circuit_breaker_opens_after_repeated_failures` |
| Secrets leakage | Keys only from env / Streamlit Secrets; `.env` git-ignored; logs scrub `sk-ant-…` and emails | `app/logging_setup.py` |
| XSS through answers | Streamlit renders markdown without raw HTML; API returns JSON | — |
| Oversized / malformed input | Pydantic schemas with `extra="forbid"`, length limits, 64 KB request cap, session-id pattern | tests |
| Vulnerable dependencies | Pinned versions; `pip-audit` + `bandit` + ruff security rules in CI | `.github/workflows/ci.yml` |

## OWASP Top 10 for LLM Applications (2026) mapping

| Risk | Status in this PoC |
|---|---|
| Prompt injection | Mitigated by architecture (gateway, no approve tool, spotlighting); red-team eval cases |
| Sensitive information disclosure | Role scoping, redaction, k-anonymity (min group 5), log scrubbing |
| Excessive agency | Risk tiers, autonomy dial, segregation of duties |
| Supply chain | Pinned dependencies, pip-audit in CI |
| Data and model poisoning | Policy handbook is versioned in git and reviewed like code |
| Improper output handling | No HTML rendering; tool inputs validated by schema |
| Unbounded consumption | Rate limits, token budgets, circuit breaker |
| Misinformation | Engines decide; grounding / citation / action-claim guards; evals |

## Isolation layers

| Layer | Implementation |
|---|---|
| Identity | JWT; principal re-resolved from HRIS on every call |
| Role / data | Gateway scopes + redaction |
| Session | Server-side sessions keyed by the authenticated user, owner-checked, 8 h TTL |
| Visitor (live demo) | Private in-memory sandbox per Streamlit session |
| Model context | Only this user's tool results; employee text spotlighted as untrusted |
| Environment | `APP_ENV=prod` disables demo login and reset, and requires `JWT_SECRET` |

## Production hardening still required

OIDC/SAML SSO (RS256 + JWKS) · secrets manager · Postgres row-level security as a second layer behind the gateway · Redis-backed limits · audit shipped to WORM storage / SIEM · network egress allow-list (only the model endpoint) · penetration test · regular red-team eval runs.
