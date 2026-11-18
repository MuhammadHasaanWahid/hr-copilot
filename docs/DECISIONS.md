# Decision log

Short ADR-style notes on the trade-offs behind the PoC.

## ADR-1: Scope: depth over breadth
**Context:** a 3-hour PoC; the brief warns against "the largest number of features".
**Decision:** build one capability fully: cross-system investigation, plus policy-grounded decision support, plus governed actions. It covers all four brief scenarios.
**Consequence:** no payroll, recruitment or performance modules. The architecture (tool catalogue + gateway + connectors) is how they would be added.

## ADR-2: Who decides: LLM or code?
**Decision:** code decides (policy engine, optimiser, statistics); the LLM plans, gathers context and explains.
**Why:** reproducibility, testability, auditability, and protection against confident hallucination. Business rules change through reviewed code and tests, not prompt edits.
**Trade-off:** each new policy domain needs engineering work. Mitigation: a policy-as-code DSL owned jointly with HR (roadmap).

## ADR-3: Where security lives
**Decision:** in the `ToolGateway` and `ActionService`, never in the prompt.
**Why:** prompt injection is unsolved; an architecture that is safe *even when the model is fooled* is required for HR data.
**Evidence:** `tests/test_security_and_governance.py`, and the seeded injection in a leave reason (scenario 2).

## ADR-4: Autonomy as configuration
**Decision:** three levels (advisory / assisted / autonomous) × three risk tiers. High-risk actions always need a different, authorised human.
**Why:** resolves the leadership disagreement by making autonomy a measurable, reversible business setting rather than an architectural fork.

## ADR-5: Agent loop hosting
**Options:** (a) manual Messages API loop in-process; (b) SDK tool runner; (c) a hosted agent runtime (Managed Agents).
**Decision:** (a).
**Why:** tool execution must stay behind the in-process gateway; we need per-call trace data, append-only history handling, and a drop-in offline fallback. The loop is small and fully tested with a stub client.
**Revisit when:** long-running, file-heavy or scheduled agent jobs appear (e.g. monthly workforce reports), where a hosted runtime removes infrastructure work.

## ADR-6: Model, effort, cost
**Decision:** `claude-opus-5-5`, adaptive thinking, effort `medium`; prompt caching on the static prompt and tools; server-side refusal fallback.
**Why:** the multi-step investigation benefits from a strong planner. Medium effort keeps latency and cost reasonable.
**Next:** build an eval set, then test `low` effort or a smaller model for simple routes; choose per route by measured quality.

## ADR-7: Storage
**Decision:** SQLite. The simulated systems are partitioned by table prefix and accessed only via per-system connectors. HR Copilot's own state (audit chain, proposals, sessions, metrics) lives in `app_*` tables. The API server uses a file (`HR_DB_PATH`), so proposals and audit survive restarts. The public Streamlit demo gives each visitor an in-memory sandbox.
**Why:** zero setup (Docker isn't available on the build machine; reviewers need nothing installed), while the persistence boundary stays explicit.
**Revisit:** Postgres for multi-process deployments; it's a connection change plus row-level security.

## ADR-8: Statistics thresholds
**Decision:** z ≥ 2.5 for both absence tests, minimum 15 events for the day-of-week test, minimum group size 5.
**Why:** multiple comparisons across groups would otherwise produce false alarms that erode HR's trust in the tool. Findings are framed as hypotheses with supportive actions.

## ADR-9: Degraded mode
**Decision:** on an LLM outage or error, an open circuit breaker (3 consecutive failures → 60 s pause), or an exhausted token budget, answer with the deterministic planner through the same gateway, and show a notice.
**Why:** reliability: HR self-service should not depend on a single external provider being up, and a public demo must not have an unbounded bill.

## ADR-10: Frontend: Streamlit as a thin client
**Decision:** Streamlit UI that talks only to the service layer: in-process for the hosted live demo, or over HTTP to FastAPI when `API_URL` is set. The earlier HTML page was removed to keep one UI.
**Why:** Python-only, fast to build, free public hosting on Streamlit Community Cloud from the GitHub repo.
**Trade-off:** Streamlit reruns the script on every interaction and has basic session handling, so it's not the long-term enterprise UI. Because it holds no logic or authority, a React frontend can replace it against the same API.

## ADR-11: Authentication
**Decision:** JWT (HS256) issued by a demo login; every call re-resolves the principal from HRIS.
**Why:** same token shape as enterprise SSO. Moving to OIDC (RS256 + JWKS) changes only `auth.verify`. `APP_ENV=prod` disables demo login and requires a real secret.

## ADR-12: LLM provider
**Options:** Gemini API free tier · Anthropic API · Bedrock / Vertex AI · OpenRouter.
**Decision:** the **Gemini free tier** (`HR_LLM_PROVIDER=gemini`, google-genai SDK) powers the public demo at zero cost. Claude (Anthropic API, or Bedrock) is implemented and selectable by one environment variable.
**Free-tier constraints and how they're handled:**
- **Low per-model rate limits** (the newest Flash model allows only a handful of requests a day). Defaults are Flash-Lite models; a model that returns 429 or 404 is skipped for a cool-down and the next model in `HR_GEMINI_FALLBACK_MODELS` is used. When all are exhausted, the offline planner answers.
- **Data use:** prompts on unpaid quota may be used by Google to improve its products. Acceptable here only because all data is synthetic. Real HR data requires a paid (billing-enabled) Gemini or Vertex AI project, or Claude, with a DPA.
- **Model portability:** the planners share one contract (`run(principal, history, text, call_tool)`), one tool schema (JSON Schema from Pydantic), the same gateway, guards and evals. Switching provider changes no business logic.

**Why not OpenRouter:** it adds another processor of employee data (opting out of training and zero data retention are left to the customer), charges platform fees, and its generic API drops provider-specific features.

## ADR-13: Hallucination guards
**Decision:** deterministic post-checks:
- Grounding: numbers, ISO dates and employee names must appear in this session's tool results or the user's message.
- Action claims: "done" only if an action executed.
- Citations: clause IDs must exist.

On failure: one automatic correction round, then a visible warning.
**Why:** cheap, explainable, measurable (guard trigger counts in metrics and evals).
**Limits:** heuristic; small integers (0–10) aren't checked; it can't catch a wrong conclusion drawn from correct numbers, which is why the eval set exists.

## ADR-14: Business case and pilot
**Decision:** show value with explicit, editable assumptions, and propose a pilot to replace them with measurements.
**Pilot:**
- One department, 8 weeks, `assisted` mode.
- Baseline: HR ticket volume and handling time by category.
- KPIs:
  - Ticket deflection
  - Time to resolution
  - Routing defects found and fixed
  - % of proposals approved unchanged
  - Cost per resolved request vs a fully loaded HR minute
- Guardrails that must stay at zero: unauthorised exposures and unapproved high-risk actions.
