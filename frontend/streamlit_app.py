"""HR Copilot - Streamlit frontend.

Run locally:      streamlit run frontend/streamlit_app.py
Live demo:        deployed on Streamlit Community Cloud (secrets: ANTHROPIC_API_KEY, ...)
Separate backend: set API_URL=http://host:8000 to use the FastAPI service over HTTP.

The UI is a thin client: identity, permissions, policy, approvals and audit all
live in the backend service. Nothing here can bypass them.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="HR Copilot", page_icon="🧭", layout="wide")

# Streamlit Cloud secrets -> environment (read by app.config.Settings). Never displayed.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, (str, int, float, bool)):
            os.environ.setdefault(_k, str(_v))
# No secrets file locally -> .env / real environment variables are used instead.
except Exception:  # noqa: S110 # nosec B110
    pass

from frontend.client import ClientError, HttpClient, LocalClient  # noqa: E402

SERIES_COLOR = "#2a78d6"
STATUS_ICON = {"ok": "✅", "denied": "🔒", "error": "⚠️", "proposed": "📝", "executed": "⚡", "blocked": "⛔"}
REPO_URL = os.getenv("REPO_URL", "")


# ----------------------------------------------------------------------------- client
@st.cache_resource
def _http_client(url: str) -> HttpClient:
    return HttpClient(url)


def get_client():
    api = os.getenv("API_URL", "").strip()
    if api:
        return _http_client(api)
    if "client" not in st.session_state:  # one isolated sandbox per visitor session
        with st.spinner("Preparing your private demo environment (synthetic HR data)…"):
            st.session_state.client = LocalClient()
    return st.session_state.client


client = get_client()
ss = st.session_state
ss.setdefault("token", None)
ss.setdefault("user", None)
ss.setdefault("messages", [])
ss.setdefault("chat_id", uuid.uuid4().hex[:12])
ss.setdefault("pending", None)


def suggestions(user_id: str, scenario: dict) -> list[str]:
    dl = date.fromisoformat(scenario["atlas_deadline"])
    alt_s, alt_e = (dl + timedelta(days=3)).isoformat(), (dl + timedelta(days=14)).isoformat()
    return {
        "E1001": [
            "I'm planning to take two weeks off next month, but I'm not sure how much leave I have remaining. I also "
            "have an important project deadline during that period. Can you help me figure out whether I can take the "
            "leave, what approvals I need, and what I should do before submitting the request?",
            f"What about {alt_s} to {alt_e} instead?",
            "Submit it, handover: Lucas Meyer covers backend reviews and on-call",
            "What is Lucas Meyer's salary?",
        ],
        "E0102": [
            "Three members of my team have requested leave during the same week, and we have a critical project "
            "delivery coming up. Can you help me understand the potential impact, identify scheduling conflicts, and "
            "suggest a reasonable approach without violating company policies?",
            "Show me Ayesha Raza's leave balance",
            "Analyse absence patterns across the company",
        ],
        "E0501": [
            "We've noticed an increase in employee absences across several departments. I want to understand whether "
            "there are any meaningful patterns, identify potential operational concerns, and determine what actions "
            "we should consider.",
            "Run a data consistency scan for approval routing",
            "What is Lucas Meyer's salary?",
        ],
        "E1010": [
            "I recently changed departments, my reporting manager has changed, and I think my leave approval is still "
            "going to my previous manager. Can you check what might be wrong and help me resolve it?",
        ],
        "E0502": [
            "Run a data consistency scan for approval routing",
            "What does the policy say about changing approval routing?",
            "What is Lucas Meyer's salary?",
        ],
    }.get(user_id, ["How much annual leave do I have left?", "What does the policy say about notice periods?"])


def sign_in(employee_id: str, passcode: str | None) -> None:
    try:
        res = client.login(employee_id, passcode)
    except ClientError as exc:
        st.sidebar.error(str(exc))
        return
    ss.token, ss.user = res["token"], res["user"]
    ss.messages, ss.chat_id, ss.pending = [], uuid.uuid4().hex[:12], None
    st.rerun()


def call(fn, *args):
    """Run a client call; on an expired/invalid session, sign the user out cleanly."""
    try:
        return fn(ss.token, *args)
    except ClientError as exc:
        if exc.status == 401:
            ss.token = ss.user = None
            st.warning("Your session expired - please sign in again.")
            st.stop()
        raise


# ----------------------------------------------------------------------------- sidebar
health = client.health()
info = client.personas()
with st.sidebar:
    st.markdown("## 🧭 HR Copilot")
    st.caption("Governed, agentic HR assistant · proof of concept by **Hasaan Wahid**")
    ai = f"{health['provider'].title()} ({health['model']})" if health["mode"] != "offline" else "rule-based offline planner"
    st.caption(f"AI: **{ai}** · autonomy: **{health['autonomy']}** · business date: **{health['business_date']}**")

    st.markdown("#### Sign in as")
    personas = info["personas"]
    labels = {p["id"]: f"{p['name']} · {p['role'].replace('_', ' ').title()}" for p in personas}
    current = ss.user["id"] if ss.user else personas[0]["id"]
    choice = st.radio("Demo persona", [p["id"] for p in personas], format_func=labels.get,
                      index=[p["id"] for p in personas].index(current) if current in labels else 0,
                      label_visibility="collapsed")
    st.caption(next(p["story"] for p in personas if p["id"] == choice))
    passcode = st.text_input("Demo passcode", type="password") if info.get("passcode_required") else None
    if st.button("Sign in" if not ss.user or ss.user["id"] != choice else "Signed in ✓", use_container_width=True,
                 type="primary", disabled=bool(ss.user and ss.user["id"] == choice)):
        sign_in(choice, passcode)
    with st.expander("Sign in with another employee ID"):
        other = st.text_input("Employee ID", placeholder="e.g. E1002, E2001")
        if st.button("Sign in with ID") and other:
            sign_in(other, passcode)

    if ss.user:
        u = ss.user
        st.success(f"**{u['name']}** ({u['id']})  \n{u['role'].replace('_', ' ').title()} · {u['department']}"
                   + (f" · {u['direct_reports']} direct reports" if u["direct_reports"] else ""))
        c1, c2 = st.columns(2)
        if c1.button("New chat", use_container_width=True):
            call(client.new_chat, ss.chat_id)
            ss.messages, ss.chat_id = [], uuid.uuid4().hex[:12]
            st.rerun()
        if health["env"] != "prod" and c2.button("Reset data", use_container_width=True,
                                                 help="Re-seed all synthetic systems for today"):
            call(client.reset)
            ss.messages, ss.chat_id = [], uuid.uuid4().hex[:12]
            st.rerun()
    st.divider()
    st.caption("All people and data are synthetic. Each visitor gets a private sandbox."
               + (f"  \n[Source code & docs]({REPO_URL})" if REPO_URL else ""))

# ----------------------------------------------------------------------------- landing
if not ss.user:
    st.title("HR Copilot")
    st.markdown(
        "**An AI agent that investigates across HR systems, lets deterministic engines make the policy and "
        "analytical calls, explains the result with cited policy clauses, and prepares actions that a human "
        "confirms or approves.**\n\n"
        "Pick a persona in the sidebar and press **Sign in**. Each persona has suggested questions that match "
        "the four scenarios in the assessment brief:\n\n"
        "1. **Ayesha** (employee) - two weeks off next month with a project deadline in the way\n"
        "2. **Omar** (manager) - three overlapping leave requests before a critical release\n"
        "3. **Sana** (HR business partner) - rising absences across departments\n"
        "4. **Bilal** (employee) - changed department, approvals still go to the old manager\n"
        "5. **Daniel** (HR systems admin) - approves the high-risk fix Bilal's request creates\n")
    st.info("Everything you see is synthetic. The assistant can only read and do what the signed-in role allows - "
            "try asking for a colleague's salary.")
    st.stop()

# ----------------------------------------------------------------------------- tabs
actions = call(client.actions)
open_actions = sum(a["can_decide"] for a in actions)
tab_chat, tab_actions, tab_audit, tab_value, tab_how = st.tabs([
    "💬 Assistant", f"✅ Actions ({open_actions})" if open_actions else "✅ Actions", "🧾 Audit", "📈 Value",
    "ℹ️ How it works"])


def render_assistant(m: dict) -> None:
    st.markdown(m["content"])
    r = m.get("meta")
    if not r:
        return
    g = r["guard"]
    badge = ":green[✓ verified against system data]" if g["verified"] else ":orange[⚠ partly unverified]"
    extra = " · self-corrected once" if r.get("self_corrected") else ""
    tok = r["usage"]["input_tokens"] + r["usage"]["output_tokens"]
    st.caption(f"{badge}{extra} · {r['mode']} · {len(r['trace'])} tool call(s) · {r['latency_ms']} ms"
               + (f" · {tok:,} tokens" if tok else ""))
    if r.get("notice"):
        st.warning(r["notice"])
    if r["trace"]:
        with st.expander(f"Reasoning trace · {len(r['trace'])} tool call(s)"):
            for i, t in enumerate(r["trace"], 1):
                st.markdown(f"**{i}. `{t['tool']}`** {STATUS_ICON.get(t['status'], '')} {t['status']} · "
                            f"risk: {t['risk']} · {t['ms']} ms")
                st.code(json.dumps(t["args"]), language="json")
                try:
                    pretty = json.dumps(json.loads(t["result_preview"]), indent=1)[:3000]
                except ValueError:
                    pretty = t["result_preview"]
                st.code(pretty, language="json")


with tab_chat:
    for m in ss.messages:
        with st.chat_message(m["role"], avatar="🧑" if m["role"] == "user" else "🧭"):
            render_assistant(m) if m["role"] == "assistant" else st.markdown(m["content"])

    if not ss.messages:
        st.markdown("**Try asking:**")
        for i, s in enumerate(suggestions(ss.user["id"], info["scenario"])):
            if st.button(s if len(s) < 140 else s[:137] + "…", key=f"sug{i}", use_container_width=True):
                ss.pending = s
                st.rerun()
    else:
        with st.expander("Suggested questions"):
            for i, s in enumerate(suggestions(ss.user["id"], info["scenario"])):
                if st.button(s if len(s) < 140 else s[:137] + "…", key=f"sugx{i}", use_container_width=True):
                    ss.pending = s
                    st.rerun()

    prompt = st.chat_input("Ask about leave, your team, absences, approvals…", max_chars=4000)
    prompt = prompt or ss.pending
    if prompt:
        ss.pending = None
        ss.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user", avatar="🧑"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar="🧭"):
            with st.spinner("Investigating across HR systems…"):
                try:
                    res = call(client.chat, ss.chat_id, prompt)
                    ss.messages.append({"role": "assistant", "content": res["reply"], "meta": res})
                except ClientError as exc:
                    ss.messages.append({"role": "assistant", "content": f"⚠️ {exc}"})
        st.rerun()

with tab_actions:
    st.markdown("The assistant never changes a system on its own authority. Self-service changes wait for **your "
                "confirmation**; high-risk changes wait for an **authorised approver who is not the requester**.")
    if not actions:
        st.info("No actions yet. Ask the assistant to submit leave or fix approval routing.")
    for a in actions:
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"**#{a['id']} · {a['summary']}**")
            c2.markdown(f"`{a['status']}`")
            st.caption(f"Risk: **{a['risk']}** · requested by {a['requester']['name']} · awaiting {a['awaiting']}"
                       f" · created {a['created_at']}")
            pv = a.get("preview") or {}
            for w in pv.get("warnings", []) or []:
                st.markdown(f"- ⚠️ {w}")
            for f in pv.get("findings", []) or []:
                st.markdown(f"- {f}")
            if a.get("result"):
                st.caption("Result: " + json.dumps(a["result"]))
            if a["can_decide"]:
                self_service = a["awaiting"].startswith("requester")
                b1, b2, _ = st.columns([1, 1, 3])
                if b1.button("Confirm & submit" if self_service else "Approve", key=f"ap{a['id']}", type="primary"):
                    out = call(client.decide, a["id"], True)
                    st.toast(f"Proposal #{a['id']}: {out['status']}")
                    st.rerun()
                if b2.button("Discard" if self_service else "Reject", key=f"rj{a['id']}"):
                    call(client.decide, a["id"], False)
                    st.rerun()
            elif a["status"] == "pending" and a["requester"]["id"] == ss.user["id"]:
                st.caption("🔒 You can't approve your own high-risk request (segregation of duties). "
                           "Sign in as Daniel Brooks (HR systems admin) to approve it.")

with tab_audit:
    aud = call(client.audit)
    (st.success if aud["chain_valid"] else st.error)(
        f"Hash chain {'valid ✓' if aud['chain_valid'] else 'BROKEN'} · {aud['total_entries']} entries in total · "
        + ("full log (HR admin)" if "routing.fix.approve" in ss.user["permissions"] else "showing your own events"))
    rows = [{"#": e["seq"], "time (UTC)": e["ts"], "actor": e["actor"], "event": e["event"],
             "details": json.dumps(e["details"])[:240], "hash": e["hash"][:12] + "…"} for e in aud["entries"]]
    st.dataframe(rows, use_container_width=True, hide_index=True)

with tab_value:
    m = call(client.metrics)
    guards = m["invalid_policy_citations_caught"] + m["unsupported_claims_caught"] + m["false_action_claims_caught"]
    k = st.columns(4)
    k[0].metric("Requests handled", m["turns"])
    k[1].metric("Est. manual effort avoided", f"{m['estimated_manual_minutes_avoided'] / 60:.1f} h")
    k[2].metric("AI spend", f"${m['llm_cost_usd']:.2f}", help=f"${m['cost_per_turn_usd']:.3f} per request")
    k[3].metric("Avg. latency", f"{m['avg_latency_ms'] / 1000:.1f} s")
    k = st.columns(4)
    k[0].metric("Unauthorised requests blocked", m["authorization_denials"])
    k[1].metric("Guard catches", guards, help="Invented citations, unsupported figures, false 'done' claims")
    k[2].metric("Actions proposed", m["outcomes"].get("action_proposed", 0))
    k[3].metric("Tokens used", f"{m['tokens']['input'] + m['tokens']['output']:,}")
    if m["by_category"]:
        import altair as alt
        import pandas as pd
        df = pd.DataFrame([{"category": c.replace("_", " "), "requests": n} for c, n in m["by_category"].items()])
        chart = (alt.Chart(df, title="Requests by business category").mark_bar(
            color=SERIES_COLOR, cornerRadiusEnd=4, height=18)
            .encode(x=alt.X("requests:Q", title="Requests", axis=alt.Axis(tickMinStep=1, grid=True)),
                    y=alt.Y("category:N", sort="-x", title=None),
                    tooltip=[alt.Tooltip("category:N"), alt.Tooltip("requests:Q")])
            .properties(height=max(120, 34 * len(df))))
        st.altair_chart(chart, use_container_width=True)
    st.progress(min(1.0, m["daily_tokens_used"] / max(1, m["daily_token_budget"])),
                text=f"Daily AI token budget: {m['daily_tokens_used']:,} / {m['daily_token_budget']:,} "
                     "(the assistant switches to the offline planner when it is reached)")
    with st.expander("Assumptions behind 'manual effort avoided'"):
        st.table([{"category": c.replace("_", " "), "manual minutes per request": v}
                  for c, v in m["assumptions_minutes_per_request"].items()])
        st.caption(m["note"])

with tab_how:
    st.markdown("### The AI plans and explains - code decides and enforces")
    st.graphviz_chart("""
digraph G { rankdir=LR; node [shape=box style="rounded" fontname="Helvetica" fontsize=11];
  ui [label="Streamlit UI\\n(thin client)"]; api [label="Service / FastAPI\\nJWT, rate limits"];
  orch [label="Orchestrator\\nagent loop + guards"]; llm [label="Gemini (free tier, model fallback)\\nor Claude · offline planner"];
  gw [label="Tool gateway\\nvalidate · authorise · redact\\nrisk level · audit" style="rounded,filled" fillcolor="#e8f0fb"];
  eng [label="Policy engine · Analytics\\nAction service"]; sys [label="Simulated HR systems\\nHRIS · Leave · Workflow\\nProjects · Attendance"];
  db [label="SQLite\\naudit · proposals · sessions"];
  ui -> api -> orch -> llm; llm -> gw [label="tool calls"]; gw -> eng -> sys; eng -> db; }
""")
    st.markdown("""
- **Deterministic engines** make every policy verdict, statistic and plan; the model never guesses eligibility.
- **Tool gateway** checks identity, permissions and data scope in code, redacts sensitive fields, and audits every call.
- **Risk tiers + autonomy dial**: reads run directly; self-service writes need your confirmation; high-risk changes
  need an authorised approver who is not the requester - never auto-executed.
- **Guards** check every answer: figures, dates and names must come from tool results; "done" claims must match
  executed actions; policy clause IDs must exist. Failures trigger one self-correction, then a visible warning.
- **Untrusted text** (e.g. leave reasons) is wrapped and treated as data - a prompt injection is seeded in the demo.
- **Resilience**: if the AI provider fails or the budget is reached, the rule-based planner answers instead.
""")
