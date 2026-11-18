# Deployment

Two steps: push the code to a **public GitHub repository**, then deploy the Streamlit app on **Streamlit Community Cloud** so reviewers can use it live. About 15 minutes.

## 1. Push to GitHub

1. Install Git for Windows (https://git-scm.com/download/win) or GitHub Desktop, and sign in to GitHub.
2. On github.com create an **empty public repository**, e.g. `hr-copilot`. Don't add a README, .gitignore or license; the project already has them.
3. In a terminal in the project folder:

```powershell
git init
git config user.name "Hasaan Wahid"
git config user.email "<your GitHub no-reply address, see GitHub > Settings > Emails>"
git add .
git status                      # CHECK: .env, data/ and the assessment PDF must NOT be listed
git commit -m "HR Copilot: governed agentic HR assistant (PoC)"
git branch -M main
git remote add origin https://github.com/MuhammadHasaanWahid/hr-copilot.git
git push -u origin main
```

4. On GitHub, open **Actions**. The CI pipeline (lint, tests, offline evals, bandit, pip-audit) should turn green.
5. In `README.md`, replace `OWNER/REPO` in the CI badge with your repository path.

> The public repo must never contain secrets. `.gitignore` excludes `.env`, `.streamlit/secrets.toml`, `data/` and `*.pdf` (the confidential brief). Check `git status` before every commit.

## 2. Deploy the live demo on Streamlit Community Cloud

1. Go to https://share.streamlit.io and sign in with GitHub.
2. **Create app** → **Deploy a public app from GitHub**:
   - Repository: `<your-user>/hr-copilot`
   - Branch: `main`
   - Main file path: `frontend/streamlit_app.py`
   - App URL: choose one, e.g. `hr-copilot-hasaan`
3. Open **Advanced settings**:
   - Python version: **3.12**
   - **Secrets:** paste the contents of `.streamlit/secrets.toml.example` with real values. At minimum set `GEMINI_API_KEY` (free from https://aistudio.google.com/apikey). Optional: `DEMO_PASSCODE`, so strangers can't use up your free quota.
4. **Deploy.** The first build takes a few minutes; dependencies come from the root `requirements.txt`.
5. Put the app URL into the README's **Live demo** line and push again.

> **After every push that changes backend code (`app/`), reboot the app** (Manage app → ⋮ → Reboot app). Streamlit Cloud hot-reloads the UI script on push but keeps already-imported backend modules in memory; a reboot loads the new backend.

**How the live demo is isolated and protected**
- **Isolation:** each visitor gets a private, in-memory sandbox of the synthetic organisation. One visitor's approvals never affect another's.
- **Free quota:** the free Gemini tier costs nothing, but each model has a small daily quota. When a model is exhausted the next model in the chain is used; when all are exhausted the app keeps working through the offline planner and tells the user.
- **Limits:** per-user rate limit, per-conversation and daily token budgets.
- **Optional passcode:** `DEMO_PASSCODE` gates sign-in if the link is shared widely.

## 3. Optional: API and UI as separate services

For a production-like layout, run the FastAPI backend separately and point Streamlit at it:

```powershell
$env:APP_ENV="prod"; $env:JWT_SECRET="<long random>"; python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
$env:API_URL="http://localhost:8000"; python -m streamlit run frontend/streamlit_app.py
```

With `APP_ENV=prod`, demo login and data reset are disabled; tokens must come from the company identity provider (see SECURITY.md).
