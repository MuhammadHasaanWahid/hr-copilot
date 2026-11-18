# HR Copilot

**An HR portal with a built-in AI assistant: employees ask and request, managers and HR approve, and the AI checks the rules.**

**Author:** Hasaan Wahid · Buggcy AI Engineer assessment
**Live demo:** https://hr-copilot-ztg86uxlfxdxmfyv4w2rj8.streamlit.app

![CI](https://github.com/MuhammadHasaanWahid/hr-copilot/actions/workflows/ci.yml/badge.svg)

---

## What it does

In a big company, HR information lives in many separate systems: employee records, leave, approvals, projects and attendance. Answering a simple question like *"Can I take two weeks off?"* means checking all of them plus the policy. HR Copilot does that work.

| Who | What they can do |
|---|---|
| **Employee** | See leave balance and attendance · ask the assistant · submit requests (leave, attendance correction, personal details, employment letter, other) · track them |
| **Manager** | Approve or reject the team's leave, with a policy check and a team-coverage plan next to each request |
| **HR** | Review all other requests · see workforce insights (absence patterns, data errors) |
| **HR Admin** | Everything HR can do · approve system fixes · audit log · usage and value report |

**How a request moves:**

```
Employee submits  →  right reviewer gets it  →  approve / reject with a comment  →  change applied + logged
                     (leave → manager, other → HR, system fix → HR admin)
```

## Key ideas

- **The AI explains; code decides; people approve.** Rules, numbers and calculations come from tested code, not from the AI's guesses. The AI can prepare a request, but **only a person can approve it**, and **nobody can approve their own request**.
- **Every answer is checked.** Numbers, dates and names in an answer must match the HR records, and the assistant can't say "done" unless something really happened. Answers that pass show **"Verified against HR records"**.
- **Each role sees only what it's allowed to.** An employee can't see a colleague's data, and only HR admins see salaries. This is enforced in code, so a tricky message to the AI can't get around it.
- **It spots tricks.** One demo leave request hides a message aimed at the AI ("ignore all previous instructions…"). The system flags it to the manager and ignores it.
- **It keeps working.** If the AI service is down or out of free quota, a built-in rule-based assistant answers instead.
- **Everything is recorded** in a tamper-proof audit log.

## Try the demo (5 minutes)

Open the live demo, pick an account and click **Sign in**. Use the **user menu (top right)** to switch accounts in the same tab.

1. **Ayesha (employee):** ask *"Can I take two weeks off next month?"* She sees her balance, the notice rule she misses by one day, her project deadline problem, and who must approve.
2. **Omar (manager):** open **Approvals**. Three people want the same week off. The page suggests approving two and moving Kenji's dates, and warns about the hidden instruction.
3. **Sana (HR):** open **Insights**. It shows a rise in absences in one team during a busy release, a Monday/Friday pattern in Support, and data errors.
4. **Bilal (employee):** his approvals go to his old manager. Click **"Ask HR to fix this"**.
5. **Daniel (HR admin):** **Approvals → Approve** the fix. Bilal's requests now appear in Omar's inbox.

All people and data are fictional. Each visitor gets a private copy, so nothing you do affects anyone else.

## Run it yourself

```bash
python -m pip install -r requirements.txt
copy .env.example .env                              # add a free GEMINI_API_KEY (optional)
python -m streamlit run frontend/streamlit_app.py   # opens http://localhost:8501
```

Without an API key it still works, using the built-in rule-based assistant.
Optional REST API: `python -m uvicorn app.main:app --port 8000` (docs at `/docs`).

## How it's built

```
Streamlit UI  →  Service / API (login, limits)  →  AI assistant (Gemini; Claude optional)
                                                      ↓ every action goes through
                                     Tool gateway: permission check · hide private data · risk level · audit
                                                      ↓
                        Policy engine · Analytics · Request workflow  →  5 simulated HR systems (SQLite)
```

| Part | Technology |
|---|---|
| Interface | Streamlit (thin client: no business logic) |
| Backend | Python 3.12, FastAPI, Pydantic |
| AI | Google Gemini free tier with automatic fallback between models; Claude and Amazon Bedrock supported |
| Data | SQLite (simulated HR systems + requests, audit, sessions) |
| Login | Signed tokens (JWT), the same kind real company sign-in uses |
| Quality | pytest, evaluation set, ruff, bandit, pip-audit, GitHub Actions CI |

## Proof it works

| Check | Result |
|---|---|
| Automated tests | **57 passing**: rules, permissions, request workflow, login, AI loops, full UI journeys |
| Test questions with real Gemini (29 cases, incl. hacking attempts) | **29/29 passed**, every answer fact-checked · **0 data leaks · 0 unapproved actions** ([details](docs/EVALS.md)) |
| Same questions, built-in assistant | **26/26 passed** |
| Scale test (50,000 employees) | Personal checks ≤ 2 ms · company-wide absence analysis ~0.8 s ([details](docs/SCALE.md)) |
| Security scans | Clean |

## Main decisions

- **Rules in code, not in the AI**: always correct, testable and explainable.
- **Gemini free tier for the demo**: costs nothing. Google may use free-tier messages to improve its products, which is fine here only because all data is fake. Real HR data needs a paid plan (Gemini on Vertex AI, or Claude) under a data agreement.
- **Not OpenRouter**: it adds another company handling employee data, plus extra fees.
- **Streamlit**: quick to build and free to host. It holds no logic, so it can be replaced by a React front end later.
- **SQLite**: no setup needed; switching to Postgres is a configuration change.

Full reasoning: [docs/DECISIONS.md](docs/DECISIONS.md)

## Limitations (honest list)

- HR systems and company login are **simulated**; real ones would plug into the same interfaces.
- The demo uses one country's simplified leave policy.
- The live demo keeps data in memory, so it resets when the session ends.
- Answer checks catch invented facts, but not every wrong conclusion. The test set covers the rest.

## Path to production

Real company login (SSO) · connectors to real HR systems (e.g. Workday, SAP) · Postgres · a paid AI plan with a data agreement and an EU region · notifications in Teams or Slack · regular security testing. AI used in hiring and employment is "high-risk" under the EU AI Act, with obligations from **2 December 2027**. The design already supports human approval, logging and transparency.

## More docs

[Security](docs/SECURITY.md) · [Privacy](docs/PRIVACY.md) · [Deployment](docs/DEPLOYMENT.md) · [Runbook](docs/RUNBOOK.md) · [Evaluations](docs/EVALS.md) · [Scale test](docs/SCALE.md) · [Decisions](docs/DECISIONS.md)
