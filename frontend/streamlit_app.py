"""HR Copilot - Streamlit frontend (role-based HR self-service and approvals portal).

Run locally:      python -m streamlit run frontend/streamlit_app.py
Separate backend: set API_URL=http://host:8000 to use the FastAPI service over HTTP.

The UI is a thin client: identity, permissions, policy, approvals and audit all
live in the backend service. Nothing here can bypass them.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="HR Copilot", page_icon="🧭", layout="wide", initial_sidebar_state="collapsed")

# Streamlit Cloud secrets -> environment (read by app.config.Settings). Never displayed.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, (str, int, float, bool)):
            os.environ.setdefault(_k, str(_v))
# No secrets file locally -> .env / real environment variables are used instead.
except Exception:  # noqa: S110 # nosec B110
    pass

from frontend.client import ClientError, HttpClient, LocalClient  # noqa: E402

BRAND = "#2a78d6"
ROLE_LABEL = {"EMPLOYEE": "Employee", "MANAGER": "Manager", "HR_PARTNER": "HR Business Partner", "HR_ADMIN": "HR Administrator"}
STATUS = {"pending": ("Pending review", "orange"), "approved": ("Approved", "green"), "executed": ("Completed", "green"),
          "rejected": ("Rejected", "red"), "failed": ("Could not complete", "red"), "executing": ("Processing", "blue")}
TOOL_LABEL = {
    "get_leave_balance": "Leave balance", "check_leave_eligibility": "Leave policy check",
    "get_project_commitments": "Project commitments", "analyze_team_leave_conflicts": "Team coverage analysis",
    "analyze_absence_patterns": "Absence analytics", "diagnose_approval_routing": "Approval routing check",
    "request_routing_fix": "Routing fix request", "submit_leave_request": "Leave request draft",
    "submit_hr_request": "HR request draft", "search_policy": "HR policy handbook", "get_employee_profile": "Employee record",
    "find_employee": "Employee directory", "list_leave_requests": "Leave requests", "scan_data_consistency": "Data consistency scan",
    "list_my_requests": "My requests", "get_attendance": "Attendance records"}
# Policy-check status icons (bandit B105 false positive: the key "pass" is not a password).
CHECK_ICON = {"pass": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}  # nosec B105
TOOL_STATUS = {"ok": "✓", "denied": "access denied", "proposed": "draft created", "executed": "done", "error": "not possible",
               "blocked": "blocked"}

st.markdown("""
<style>
  #MainMenu, footer {visibility: hidden;}
  .block-container {padding-top: 4.2rem; max-width: 1180px;}
  [data-testid="stMetric"] {background: #ffffff; border: 1px solid #e6e8ec; border-radius: 12px; padding: 14px 18px;}
  [data-testid="stMetricLabel"] p {color: #52514e; font-weight: 500;}
  div[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 12px;}
  .hc-muted {color: #6b6f76; font-size: 0.92rem;}
  .hc-brand {font-weight: 700; font-size: 1.15rem; letter-spacing: -0.01em;}
  .hc-hero {font-size: 2.2rem; font-weight: 700; letter-spacing: -0.02em; margin-bottom: 0.2rem;}
  [data-testid="stChatMessage"] h1, [data-testid="stChatMessage"] h2, [data-testid="stChatMessage"] h3,
  [data-testid="stChatMessage"] h4 {font-size: 1.02rem !important; font-weight: 650; margin: 0.6rem 0 0.2rem; padding: 0;}
  [data-testid="stChatMessage"] p, [data-testid="stChatMessage"] li {font-size: 0.98rem; line-height: 1.55;}
</style>
""", unsafe_allow_html=True)


def esc(text) -> str:
    """Escape user-provided text before rendering it as markdown."""
    return re.sub(r"([\\`*_{}\[\]()<>#+\-.!|~$])", r"\\\1", str(text or ""))


# ----------------------------------------------------------------------------- client
@st.cache_resource
def _http_client(url: str) -> HttpClient:
    return HttpClient(url)


def get_client():
    api = os.getenv("API_URL", "").strip()
    if api:
        return _http_client(api)
    if "client" not in st.session_state:  # one isolated sandbox per visitor session
        with st.spinner("Preparing your private demo environment…"):
            st.session_state.client = LocalClient()
    return st.session_state.client


client = get_client()
ss = st.session_state
for k, v in {"token": None, "user": None, "messages": [], "chat_id": uuid.uuid4().hex[:12], "pending": None}.items():  # nosec B105
    ss.setdefault(k, v)


def call(fn, *args):
    """Run a client call; on an expired/invalid session, sign the user out cleanly."""
    try:
        return fn(ss.token, *args)
    except ClientError as exc:
        if exc.status == 401:
            ss.token = ss.user = None
            st.rerun()
        raise


def status_badge(status: str) -> None:
    label, color = STATUS.get(status, (status.title(), "gray"))
    st.badge(label, color=color)


def role() -> str:
    return ss.user["role"]


# ============================================================================== login
def login_screen() -> None:
    info = client.personas()
    _, mid, _ = st.columns([1, 1.25, 1])
    with mid:
        st.markdown("<div style='height:6vh'></div>", unsafe_allow_html=True)
        st.markdown("<div class='hc-hero'>🧭 HR Copilot</div>", unsafe_allow_html=True)
        st.markdown("<div class='hc-muted'>HR self-service, requests and approvals - with an AI assistant that "
                    "checks policy for you.</div>", unsafe_allow_html=True)
        st.write("")
        with st.container(border=True):
            people = info["personas"]
            labels = {p["id"]: f"{p['name']}  ·  {ROLE_LABEL.get(p['role'], p['role'])}" for p in people}
            choice = st.selectbox("Sign in as", list(labels), format_func=labels.get)
            st.caption(next(p["story"] for p in people if p["id"] == choice))
            passcode = st.text_input("Passcode", type="password") if info.get("passcode_required") else None
            if st.button("Sign in", type="primary", use_container_width=True):
                try:
                    res = client.login(choice, passcode)
                except ClientError as exc:
                    st.error(str(exc))
                else:
                    ss.token, ss.user = res["token"], res["user"]
                    ss.messages, ss.chat_id, ss.pending = [], uuid.uuid4().hex[:12], None
                    st.rerun()
        st.caption("Demo environment · all people and data are fictional · each visitor gets a private sandbox")
        st.write("")
        c1, c2, c3 = st.columns(3)
        c1.markdown("**Ask**  \n<span class='hc-muted'>Leave, attendance and policy answers, checked against HR records.</span>",
                    unsafe_allow_html=True)
        c2.markdown("**Request**  \n<span class='hc-muted'>Leave, corrections, letters - submit and track in one place.</span>",
                    unsafe_allow_html=True)
        c3.markdown("**Approve**  \n<span class='hc-muted'>Managers and HR decide with policy checks side by side.</span>",
                    unsafe_allow_html=True)


# ============================================================================== header
def header() -> None:
    u = ss.user
    left, right = st.columns([3, 1.3], vertical_alignment="center")
    left.markdown("<span class='hc-brand'>🧭 HR Copilot</span>", unsafe_allow_html=True)
    with right.popover(f"👤 {u['name']}", use_container_width=True):
        st.markdown(f"**{u['name']}**  \n{ROLE_LABEL.get(u['role'], u['role'])} · {u['department']}")
        if u["direct_reports"]:
            st.caption(f"{u['direct_reports']} direct reports")
        if st.button("Sign out", use_container_width=True):
            ss.token = ss.user = None
            ss.messages = []
            st.rerun()
        health = client.health()
        if health["env"] != "prod":
            st.divider()
            st.caption("Demo controls")
            if st.button("Reset demo data", use_container_width=True, help="Restore all fictional data for today"):
                call(client.reset)
                ss.messages, ss.chat_id = [], uuid.uuid4().hex[:12]
                st.rerun()


# ============================================================================== pages
def page_home() -> None:
    u = ss.user
    st.markdown(f"## Welcome back, {u['name'].split()[0]}")
    st.caption(f"{ROLE_LABEL.get(u['role'], u['role'])} · {u['department']}")
    ov = call(client.overview)
    mine = call(client.my_requests)
    annual = ov["balances"]["annual"]
    open_mine = sum(r["status"] == "pending" for r in mine["requests"])
    cols = st.columns(4)
    cols[0].metric("Annual leave available", f"{annual['available']:g} days" if annual else "-")
    cols[1].metric("My open requests", open_mine)
    cols[2].metric("Absences (90 days)", len(ov["absences_last_90_days"]))
    nxt = ov["upcoming_leave"][0]["title"].split(" (")[0] if ov["upcoming_leave"] else "None planned"
    cols[3].metric("Next leave", nxt)
    if ss.get("flash"):
        st.success(ss.pop("flash"))
    routing_banner(mine)
    if role() != "EMPLOYEE":
        inbox = call(client.inbox)
        waiting = len(inbox["leave"]) + len(inbox["cases"])
        st.write("")
        with st.container(border=True):
            a, b = st.columns([4, 1], vertical_alignment="center")
            a.markdown(f"**{waiting} request(s) waiting for your decision**  \n"
                       "<span class='hc-muted'>Each one comes with an automatic policy check.</span>",
                       unsafe_allow_html=True)
            if b.button("Open approvals", type="primary", use_container_width=True):
                st.switch_page(PAGES["approvals"])
    st.write("")
    st.markdown("#### Quick actions")
    q = st.columns(3)
    if q[0].button("💬  Ask the assistant", use_container_width=True):
        st.switch_page(PAGES["assistant"])
    if q[1].button("🗓️  Request leave", use_container_width=True):
        ss.new_request = "leave"
        st.switch_page(PAGES["requests"])
    if q[2].button("📝  New HR request", use_container_width=True):
        ss.new_request = "case"
        st.switch_page(PAGES["requests"])
    if mine["requests"]:
        st.write("")
        st.markdown("#### Recent requests")
        for r in mine["requests"][:4]:
            request_row(r)


def routing_banner(mine: dict) -> None:
    """Explain an approval-routing error to the employee and let them ask HR to fix it."""
    issue = mine.get("routing_issue")
    if not issue:
        return
    if issue["fix_requested"]:
        st.info(f"Your requests still go to **{esc(issue['routed_to'])}** instead of your manager "
                f"**{esc(issue['current_manager'])}**. A fix has been requested and is waiting for the HR systems "
                "administrator.", icon=":material/hourglass_top:")
        return
    with st.container(border=True):
        a, b = st.columns([4, 1.3], vertical_alignment="center")
        a.markdown(f":orange[**Your approvals are going to the wrong person.**]  \nYour leave requests are sent to "
                   f"**{esc(issue['routed_to'])}** (your previous manager) instead of **{esc(issue['current_manager'])}**. "
                   "They won't be reviewed by your current manager until this is fixed.")
        if b.button("Ask HR to fix this", type="primary", use_container_width=True, key="fixroute"):
            try:
                call(client.raise_routing_fix, ss.user["id"])
                st.session_state.flash = "Fix requested. Once HR approves it, your pending requests move to your manager."
            except ClientError as exc:
                st.session_state.flash = f"Could not request the fix: {exc}"
            st.rerun()


def request_row(r: dict) -> None:
    with st.container(border=True):
        a, b = st.columns([4, 1.2], vertical_alignment="center")
        a.markdown(f"**{r['type_label']}** · {esc(r['title'])}  \n<span class='hc-muted'>Submitted {r['submitted_at']}"
                   f" · reviewer: {esc(r['reviewer'])}</span>", unsafe_allow_html=True)
        with b:
            status_badge(r["status"])
        if r.get("decided_by"):
            st.caption(f"Decided by {r['decided_by']}" + (f": “{r['decision_comment']}”" if r.get("decision_comment") else ""))
        if r.get("result") and isinstance(r["result"], dict) and r["result"].get("outcome"):
            st.caption(r["result"]["outcome"])


SUGGESTIONS = {
    "EMPLOYEE": [
        ("Can I take two weeks off next month?",
         "I'm planning to take two weeks off next month, but I'm not sure how much leave I have remaining. I also have an "
         "important project deadline during that period. Can you help me figure out whether I can take the leave, what "
         "approvals I need, and what I should do before submitting the request?"),
        ("My approvals go to my old manager",
         "I recently changed departments, my reporting manager has changed, and I think my leave approval is still going "
         "to my previous manager. Can you check what might be wrong and help me resolve it?"),
        ("How much leave do I have left?", "How much annual leave do I have left?"),
        ("What does the policy say about notice?", "How much notice do I need for a 7-day leave request?"),
    ],
    "MANAGER": [
        ("Three people want the same week off",
         "Three members of my team have requested leave during the same week, and we have a critical project delivery "
         "coming up. Can you help me understand the potential impact, identify scheduling conflicts, and suggest a "
         "reasonable approach without violating company policies?"),
        ("Show Ayesha Raza's leave balance", "Show me Ayesha Raza's leave balance."),
        ("How much leave do I have left?", "How much annual leave do I have left?"),
    ],
    "HR_PARTNER": [
        ("Why are absences increasing?",
         "We've noticed an increase in employee absences across several departments. I want to understand whether there "
         "are any meaningful patterns, identify potential operational concerns, and determine what actions we should consider."),
        ("Find approval-routing problems", "Run a data consistency scan for approval routing."),
        ("What is Lucas Meyer's salary?", "What is Lucas Meyer's salary?"),
    ],
    "HR_ADMIN": [
        ("Find approval-routing problems", "Run a data consistency scan for approval routing."),
        ("Check routing for Bilal Ahmed", "Check the approval routing for Bilal Ahmed."),
        ("What does the policy say about routing changes?", "What does the policy say about changing approval routing?"),
    ],
}


def render_answer(m: dict) -> None:
    st.markdown(m["content"])
    r = m.get("meta")
    if not r:
        return
    with st.container(horizontal=True, gap="small"):
        if r["guard"]["verified"]:
            st.badge("Verified against HR records", icon=":material/verified:", color="green")
        else:
            st.badge("Partly unverified", icon=":material/warning:", color="orange")
        if r["trace"]:
            n = len(r["trace"])
            st.badge(f"{n} source{'s' if n != 1 else ''} checked", color="gray")
    if r.get("notice"):
        st.caption(f"ℹ️ {r['notice']}")
    if r["trace"]:
        with st.expander("Sources checked"):
            for t in r["trace"]:
                st.markdown(f"- {TOOL_LABEL.get(t['tool'], t['tool'])} — {TOOL_STATUS.get(t['status'], t['status'])}")


def page_assistant() -> None:
    st.markdown("## Assistant")
    st.caption("Ask about leave, attendance, approvals or HR policy. Answers are checked against HR records; "
               "requests it prepares wait for your confirmation.")
    for m in ss.messages:
        with st.chat_message(m["role"], avatar="🧑" if m["role"] == "user" else "🧭"):
            if m["role"] == "assistant":
                render_answer(m)
            else:
                st.markdown(esc(m["content"]))
    if not ss.messages:
        st.write("")
        st.markdown("**Suggested questions**")
        sugg = SUGGESTIONS.get(role(), [])
        cols = st.columns(2)
        for i, (label, prompt) in enumerate(sugg):
            if cols[i % 2].button(label, key=f"sug{i}", use_container_width=True):
                ss.pending = prompt
                st.rerun()
    else:
        _, right = st.columns([5, 1])
        if right.button("New conversation", use_container_width=True):
            call(client.new_chat, ss.chat_id)
            ss.messages, ss.chat_id = [], uuid.uuid4().hex[:12]
            st.rerun()
    prompt = st.chat_input("Ask anything about HR…", max_chars=4000) or ss.pending
    if prompt:
        ss.pending = None
        ss.messages.append({"role": "user", "content": prompt})
        with st.spinner("Checking HR records…"):
            try:
                res = call(client.chat, ss.chat_id, prompt)
                ss.messages.append({"role": "assistant", "content": res["reply"], "meta": res})
            except ClientError as exc:
                ss.messages.append({"role": "assistant", "content": f"⚠️ {exc}"})
        st.rerun()


@st.dialog("Request leave", width="large")
def leave_dialog() -> None:
    today = date.today()
    c1, c2 = st.columns(2)
    start = c1.date_input("First day", value=today + timedelta(days=21), min_value=today)
    end = c2.date_input("Last day", value=today + timedelta(days=23), min_value=today)
    leave_type = st.segmented_control("Type", ["annual", "sick"], default="annual", format_func=str.title)
    reason = st.text_input("Reason (optional)", max_chars=300)
    handover = st.text_area("Handover note", max_chars=1000, help="Required if a project deadline falls inside your leave.")
    payload = {"start_date": start.isoformat(), "end_date": end.isoformat(), "leave_type": leave_type or "annual",
               "reason": reason, "handover_note": handover or None}
    try:
        ev = call(client.check_leave, payload)
    except ClientError as exc:
        st.error(str(exc))
        return
    icon = CHECK_ICON
    with st.container(border=True):
        st.markdown(f"**Policy check** · {ev['working_days']} working days")
        for chk in ev["checks"]:
            st.markdown(f"{icon[chk['status']]} **{chk['rule']}** — {esc(chk['detail'])}")
        st.caption("Approvers: " + ", ".join(a["name"] + f" ({a['role']})" for a in ev["required_approvals"]))
    if st.button("Submit request", type="primary", disabled=not ev["can_submit"], use_container_width=True):
        try:
            res = call(client.submit_leave, payload)
        except ClientError as exc:
            st.error(str(exc))
        else:
            st.session_state.flash = f"Leave request submitted - {res['reviewer']} will review it."
            st.rerun()


@st.dialog("New HR request", width="large")
def case_dialog() -> None:
    types = client.request_types()
    t = st.selectbox("Request type", list(types), format_func=lambda k: types[k]["label"])
    st.caption(types[t]["help"])
    subject = st.text_input("Subject", max_chars=120)
    payload = {"type": t, "subject": subject}
    if t == "attendance_correction":
        payload["day"] = st.date_input("Date to correct", value=date.today() - timedelta(days=7),
                                       max_value=date.today()).isoformat()
    if t == "personal_details":
        loc = st.text_input("New work location", max_chars=60)
        if loc:
            payload["new_location"] = loc
    payload["details"] = st.text_area("Details", max_chars=1500)
    if st.button("Submit to HR", type="primary", use_container_width=True, disabled=len(subject.strip()) < 3):
        try:
            call(client.submit_case, payload)
        except ClientError as exc:
            st.error(str(exc))
        else:
            st.session_state.flash = "Request submitted - HR will review it."
            st.rerun()


def page_requests() -> None:
    top, b1, b2 = st.columns([3, 1, 1], vertical_alignment="bottom")
    top.markdown("## My requests")
    requested = ss.pop("new_request", None)  # set by the quick actions on Home
    if b1.button("🗓️ Request leave", use_container_width=True) or requested == "leave":
        leave_dialog()
    elif b2.button("📝 New HR request", use_container_width=True) or requested == "case":
        case_dialog()
    if ss.get("flash"):
        st.success(ss.pop("flash"))
    mine = call(client.my_requests)
    routing_banner(mine)
    if mine["drafts"]:
        st.markdown("#### Waiting for your confirmation")
        st.caption("Prepared by the assistant. Nothing is sent until you confirm.")
        for d in mine["drafts"]:
            with st.container(border=True):
                a, b, c = st.columns([4, 1, 1], vertical_alignment="center")
                a.markdown(f"**{esc(d['summary'])}**")
                for w in (d.get("preview") or {}).get("warnings", []) or []:
                    a.caption(f"⚠️ {w}")
                if b.button("Submit", key=f"dc{d['id']}", type="primary", use_container_width=True):
                    out = call(client.decide, d["id"], True)
                    st.session_state.flash = ("Submitted for review." if out["status"] == "executed"
                                              else f"Could not submit: {(out.get('result') or {}).get('error')}")
                    st.rerun()
                if c.button("Discard", key=f"dd{d['id']}", use_container_width=True):
                    call(client.decide, d["id"], False)
                    st.rerun()
    st.markdown("#### All requests")
    if not mine["requests"]:
        st.info("No requests yet. Use the buttons above, or ask the assistant to prepare one.")
    for r in mine["requests"]:
        request_row(r)


def page_leave() -> None:
    st.markdown("## My leave & attendance")
    ov = call(client.overview)
    e = ov["employee"]
    st.caption(f"{e['title']} · {e['department']} · {e['location']} · manager: {e['manager'] or '-'}")
    a, s = ov["balances"]["annual"], ov["balances"]["sick"]
    c = st.columns(4)
    c[0].metric("Annual leave available", f"{a['available']:g} days")
    c[1].metric("Annual leave used", f"{a['used']:g} of {a['entitled'] + a['carried_over']:g}")
    c[2].metric("Pending approval", f"{a['pending']:g} days")
    c[3].metric("Sick leave used", f"{s['used']:g} of {s['entitled']:g}")
    st.write("")
    left, right = st.columns(2)
    with left:
        st.markdown("#### Upcoming leave")
        if not ov["upcoming_leave"]:
            st.caption("No upcoming leave.")
        for r in ov["upcoming_leave"]:
            request_row(r)
    with right:
        st.markdown("#### Attendance · last 90 days")
        ab = ov["absences_last_90_days"]
        st.caption(f"{len(ab)} absence day(s) recorded out of {ov['working_days_in_period']} working days.")
        if ab:
            st.dataframe([{"Date": x["day"], "Type": x["kind"].title()} for x in ab], hide_index=True,
                         use_container_width=True)
            st.caption("Something wrong? Raise an attendance correction on My requests.")


def page_approvals() -> None:
    st.markdown("## Approvals")
    st.caption("Leave requests are decided by the employee's manager; HR decides all other requests and can step in "
               "on leave. You can't decide your own requests.")
    if ss.get("flash"):
        st.success(ss.pop("flash"))
    inbox = call(client.inbox)
    if role() == "MANAGER":
        team_panel()
    leave, cases = inbox["leave"], inbox["cases"]
    st.markdown(f"#### Leave requests · {len(leave)}")
    if not leave:
        st.caption("Nothing waiting.")
    rec = {"approve": ("Policy check: OK", "green"), "review": ("Policy check: needs attention", "orange"),
           "reject": ("Policy check: breach", "red")}
    icon = CHECK_ICON
    for r in leave:
        with st.container(border=True):
            a, b = st.columns([4, 1.6], vertical_alignment="center")
            a.markdown(f"**{esc(r['requester']['name'])}** · {r['type_label']} · {r['title']}  \n"
                       f"<span class='hc-muted'>Submitted {r['submitted_at']}"
                       + (" · you are deciding as HR" if r.get("as_override") else "") + "</span>", unsafe_allow_html=True)
            label, color = rec[r["assessment"]["recommendation"]]
            with b:
                st.badge(label, color=color)
            if r.get("reason"):
                st.markdown(f"Reason: *{esc(r['reason'])}*")
            if r.get("flag"):
                st.warning(r["flag"], icon=":material/shield:")
            with st.expander("Policy check details"):
                for chk in r["assessment"]["checks"]:
                    st.markdown(f"{icon[chk['status']]} **{chk['rule']}** — {esc(chk['detail'])}")
            decision_controls("leave", r["id"])
    st.markdown(f"#### HR requests · {len(cases)}")
    if not cases:
        st.caption("Nothing waiting." if role() in ("HR_PARTNER", "HR_ADMIN") else "HR requests are reviewed by HR.")
    for r in cases:
        with st.container(border=True):
            a, b = st.columns([4, 1.6], vertical_alignment="center")
            a.markdown(f"**{esc(r['requester']['name'])}** · {r['type_label']}  \n{esc(r['title'])}")
            with b:
                st.badge("High risk · system change" if r["type"] == "request_routing_fix" else "HR review",
                         color="violet" if r["type"] == "request_routing_fix" else "blue")
            pv = r.get("preview") or {}
            if pv.get("details"):
                st.markdown(esc(pv["details"]))
            for f in pv.get("findings", []) or []:
                st.markdown(f"- {esc(f)}")
            if pv.get("system_check"):
                st.info(pv["system_check"], icon=":material/fact_check:")
            if r.get("flag"):
                st.warning(r["flag"], icon=":material/shield:")
            decision_controls("case", r["id"])
    if inbox["recently_decided"]:
        with st.expander("Recently decided"):
            for r in inbox["recently_decided"]:
                st.markdown(f"- **{esc(r['requester']['name'])}** · {r['type_label']} · {STATUS.get(r['status'], (r['status'],))[0]}"
                            + (f" · {esc(r['decision_comment'])}" if r.get("decision_comment") else ""))


def decision_controls(kind: str, rid: int) -> None:
    c1, c2, c3 = st.columns([3, 1, 1], vertical_alignment="bottom")
    comment = c1.text_input("Comment (optional)", key=f"cm{kind}{rid}", label_visibility="collapsed",
                            placeholder="Comment to the employee (optional)")
    for col, approve, label in ((c2, True, "Approve"), (c3, False, "Reject")):
        if col.button(label, key=f"{kind}{rid}{approve}", type="primary" if approve else "secondary",
                      use_container_width=True):
            try:
                if kind == "leave":
                    out = call(client.decide_leave, rid, approve, comment or None)
                else:
                    out = call(client.decide, rid, approve, comment or None)
            except ClientError as exc:
                st.error(str(exc))
                return
            st.session_state.flash = f"Request {STATUS.get(out['status'], (out['status'],))[0].lower()}."
            st.rerun()


def team_panel() -> None:
    t = call(client.team)
    plan = t.get("coverage_plan")
    for m in t.get("misrouted", []):
        st.warning(f"**{esc(m['employee'])}**'s leave request ({m['requests']}) is waiting with **{esc(m['routed_to'])}** "
                   "because of an approval-routing error, so it isn't in your list below. It will appear here as soon "
                   "as HR fixes the routing.", icon=":material/alt_route:")
    with st.container(border=True):
        st.markdown("**Team overview**")
        st.dataframe([{"Name": m["name"], "Role": m["title"], "Leave left": m["annual_available"],
                       "Next leave": m["next_leave"]} for m in t["members"]], hide_index=True, use_container_width=True)
        if plan and plan.get("request_assessments"):
            w = plan["window"]
            st.markdown(f"**Busiest upcoming week ({w[0]} to {w[1]}): suggested plan**")
            lowest_all = min(c["coverage"] for c in plan["coverage_if_all_approved"])
            lowest_plan = min(c["coverage"] for c in plan["coverage_with_recommended_plan"])
            st.caption(f"Approving everything drops team availability to {round(lowest_all * 100)}% "
                       f"(minimum 60%). With the suggested plan the lowest day is {round(lowest_plan * 100)}%.")
            for a in plan["request_assessments"]:
                alt = (" — suggest " + " or ".join(f"{x['start_date']} to {x['end_date']}" for x in a["alternatives"])
                       if a.get("alternatives") else "")
                st.markdown(f"- **{esc(a['employee'])}** ({a['dates']}): "
                            + ("approve" if a["recommendation"] == "approve" else a["recommendation"]) + alt)


def page_insights() -> None:
    st.markdown("## Workforce insights")
    data = call(client.insights)
    ab, cons = data["absences"], data["consistency"]
    o = ab["organisation"]
    c = st.columns(4)
    c[0].metric("Absence rate · last 4 weeks", f"{o['recent_rate_pct']}%",
                delta=f"{o['recent_rate_pct'] - o['baseline_rate_pct']:+.2f} pts vs baseline", delta_color="inverse")
    c[1].metric("Significant patterns", len(ab["findings"]))
    c[2].metric("Records excluded (data quality)", sum(ab["data_quality"].values()))
    c[3].metric("Approval-routing errors", cons["routing_mismatch_count"] if cons else "-")
    st.write("")
    left, right = st.columns([1.15, 1])
    with left:
        st.markdown("#### What needs attention")
        sev = {"high": "red", "medium": "orange"}
        for f in ab["findings"]:
            with st.container(border=True):
                st.badge(f"{f['severity'].title()} · {f['type'].replace('_', ' ')}", color=sev.get(f["severity"], "gray"))
                st.markdown(f"**{esc(f['group'])}**  \n{esc(f['evidence'])}")
                if f.get("hypothesis"):
                    st.caption(f["hypothesis"])
                for act in f["recommended_actions"][:2]:
                    st.markdown(f"- {esc(act)}")
    with right:
        st.markdown("#### Absence rate by department")
        import altair as alt
        import pandas as pd
        rows = [g for g in ab["groups"] if " / " not in g["group"]]
        df = pd.DataFrame([{"Department": g["group"], "Period": "Last 4 weeks", "Rate (%)": g["recent_absence_rate_pct"]}
                           for g in rows] + [{"Department": g["group"], "Period": "Baseline", "Rate (%)": g["baseline_absence_rate_pct"]}
                                             for g in rows])
        chart = (alt.Chart(df).mark_bar(cornerRadiusEnd=3, height=10)
                 .encode(y=alt.Y("Department:N", title=None, sort="-x"),
                         x=alt.X("Rate (%):Q", title="Absence rate (%)"),
                         yOffset=alt.YOffset("Period:N", sort=["Last 4 weeks", "Baseline"]),
                         color=alt.Color("Period:N", scale=alt.Scale(domain=["Last 4 weeks", "Baseline"],
                                                                     range=[BRAND, "#b8bcc4"]),
                                         legend=alt.Legend(orient="bottom", title=None)),
                         tooltip=["Department", "Period", "Rate (%)"])
                 .properties(height=60 * max(1, len(rows))))
        st.altair_chart(chart, use_container_width=True)
        st.caption("Groups under 5 people are hidden for privacy. Patterns are signals to discuss with managers, "
                   "not conclusions about individuals.")
        if cons and cons["routing_mismatches"]:
            st.markdown("#### Approval-routing errors")
            for m in cons["routing_mismatches"]:
                with st.container(border=True):
                    a, b = st.columns([3, 1.4], vertical_alignment="center")
                    a.markdown(f"**{esc(m['employee'])}** · {esc(m['department'])}  \n<span class='hc-muted'>Approvals go to "
                               f"{m['workflow_approver']} instead of manager {m['hris_manager']}</span>", unsafe_allow_html=True)
                    if b.button("Raise fix", key=f"fix{m['employee_id']}", use_container_width=True):
                        try:
                            call(client.raise_routing_fix, m["employee_id"])
                            st.toast("Fix request sent to the HR systems administrator.")
                        except ClientError as exc:
                            st.toast(str(exc))


def page_audit() -> None:
    st.markdown("## Audit log")
    aud = call(client.audit)
    msg = f"Tamper check: {'passed' if aud['chain_valid'] else 'FAILED'} · {aud['total_entries']} entries"
    if aud["chain_valid"]:
        st.success(msg)
    else:
        st.error(msg)
    st.dataframe([{"#": e["seq"], "Time (UTC)": e["ts"], "User": e["actor"], "Event": e["event"],
                   "Details": json.dumps(e["details"])[:200]} for e in aud["entries"]],
                 hide_index=True, use_container_width=True, height=520)


def page_usage() -> None:
    st.markdown("## Usage & value")
    m = call(client.metrics)
    guards = m["invalid_policy_citations_caught"] + m["unsupported_claims_caught"] + m["false_action_claims_caught"]
    c = st.columns(4)
    c[0].metric("Assistant conversations", m["turns"])
    c[1].metric("Est. HR time saved", f"{m['estimated_manual_minutes_avoided'] / 60:.1f} h")
    c[2].metric("AI cost", f"${m['llm_cost_usd']:.2f}")
    c[3].metric("Unauthorised requests blocked", m["authorization_denials"])
    c = st.columns(4)
    c[0].metric("Answers corrected by checks", guards)
    c[1].metric("Avg. response time", f"{m['avg_latency_ms'] / 1000:.1f} s")
    c[2].metric("Requests prepared", m["outcomes"].get("action_proposed", 0))
    c[3].metric("Tokens used", f"{m['tokens']['input'] + m['tokens']['output']:,}")
    if m["by_category"]:
        import altair as alt
        import pandas as pd
        df = pd.DataFrame([{"Topic": k.replace("_", " "), "Conversations": v} for k, v in m["by_category"].items()])
        st.altair_chart(alt.Chart(df, title="Conversations by topic").mark_bar(color=BRAND, cornerRadiusEnd=4, height=18)
                        .encode(x=alt.X("Conversations:Q", axis=alt.Axis(tickMinStep=1)), y=alt.Y("Topic:N", sort="-x", title=None),
                                tooltip=["Topic", "Conversations"]).properties(height=max(120, 34 * len(df))),
                        use_container_width=True)
    with st.expander("How 'time saved' is estimated"):
        st.table([{"Topic": k.replace("_", " "), "Manual minutes per request": v}
                  for k, v in m["assumptions_minutes_per_request"].items()])
        st.caption(m["note"])


# ============================================================================== router
if not ss.user:
    login_screen()
    st.stop()

header()
ROLE_PAGES = {
    "EMPLOYEE": ["home", "assistant", "requests", "leave"],
    "MANAGER": ["home", "assistant", "approvals", "requests", "leave"],
    "HR_PARTNER": ["home", "assistant", "approvals", "insights", "requests", "leave"],
    "HR_ADMIN": ["home", "assistant", "approvals", "insights", "audit", "usage", "requests", "leave"],
}
allowed = ROLE_PAGES.get(role(), ROLE_PAGES["EMPLOYEE"])
_SPEC = {
    "home": (page_home, "Home", ":material/home:"),
    "assistant": (page_assistant, "Assistant", ":material/forum:"),
    "requests": (page_requests, "My requests", ":material/assignment:"),
    "leave": (page_leave, "Leave & attendance", ":material/event:"),
    "approvals": (page_approvals, "Approvals", ":material/task_alt:"),
    "insights": (page_insights, "Insights", ":material/insights:"),
    "audit": (page_audit, "Audit log", ":material/shield:"),
    "usage": (page_usage, "Usage & value", ":material/monitoring:"),
}
PAGES = {k: st.Page(fn, title=t, icon=i, url_path=k, default=(k == "home")) for k, (fn, t, i) in _SPEC.items()}
nav = st.navigation([PAGES[k] for k in allowed], position="top")
goto = ss.pop("goto", None)  # one-off programmatic navigation (deep links, tests)
if goto in allowed and nav.url_path != goto:
    st.switch_page(PAGES[goto])
nav.run()
