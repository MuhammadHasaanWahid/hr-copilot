"""Deterministic fallback planner.

Used when no API key is configured, when the LLM is unavailable (degraded
mode), and in tests. It is intentionally simple - keyword intent routing plus
templated answers - but it calls exactly the same tools through exactly the
same gateway, so governance behaviour is identical in both modes. It is NOT
the intelligent part of the system; it is the safety net that keeps HR
operations working if the model provider has an outage.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from collections.abc import Callable

from ..calendar_utils import add_working_days, first_day_next_month, is_working_day
from ..security import Principal

# Status icons (bandit B105 false positive: the key "pass" is not a password).
ICON = {"pass": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}  # nosec B105
DEPARTMENTS = ["engineering", "customer support", "sales", "finance", "people & hr"]
NUM = {"one": 1, "a": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10}
MIN_POLICY_SCORE = 2.5  # below this BM25 score a clause is not a real match - say "not covered" instead of guessing


def _has(t: str, *words: str) -> bool:
    return any(w in t for w in words)


class OfflineAgent:
    name = "offline"

    def __init__(self, today: date) -> None:
        self.today = today

    # ---------------------------------------------------------------- parsing
    def parse_period(self, t: str) -> tuple[date, date, str] | None:
        iso = re.findall(r"\d{4}-\d{2}-\d{2}", t)
        if len(iso) >= 2:
            return date.fromisoformat(iso[0]), date.fromisoformat(iso[1]), "dates you gave"
        n = 5
        m = re.search(r"\b(\d+|one|a|two|three|four|five|ten)\s+(week|day)s?\b", t)
        if m:
            k = int(m.group(1)) if m.group(1).isdigit() else NUM[m.group(1)]
            n = k * 5 if m.group(2) == "week" else k
        if len(iso) == 1:
            s = date.fromisoformat(iso[0])
            return s, add_working_days(s, n), f"{n} working days from {s}"
        if "next month" in t:
            s = first_day_next_month(self.today)
            while not is_working_day(s):
                s += timedelta(days=1)
            return s, add_working_days(s, n), f"the first {n} working days of next month"
        if "next week" in t:
            s = self.today + timedelta(days=7 - self.today.weekday())
            return s, add_working_days(s, n), f"{n} working days from next Monday"
        return None

    # ------------------------------------------------------------------ main
    def run(self, principal: Principal, state: dict, text: str, call: Callable[[str, dict], dict]) -> str:
        t = text.lower()
        if _has(t, "submit", "go ahead", "book it", "apply for it") and state.get("planned"):
            return self._submit(state, text, call)
        personal = _has(t, "my ", " me ", "i ", "i'm")
        if personal and (_has(t, "routing", "previous manager", "old manager", "wrong manager") or (
                "manager" in t and "approv" in t and _has(t, "chang", "still", "wrong", "previous"))):
            return self._routing(t, call)
        if personal and _has(t, "my request", "status of my", "my leave request", "my application"):
            return self._my_requests(call)
        if personal and _has(t, "letter") and _has(t, "need", "request", "want", "get"):
            r = call("submit_hr_request", {"type": "employment_letter", "subject": "Employment letter",
                                           "details": text[:500]})
            if r["status"] == "proposed":
                return ("📝 I've prepared an **employment letter request**. Confirm it on the **My requests** page and "
                        "HR will review it.")
            return f"I couldn't prepare the request: {r.get('error') or r.get('reason')}"
        if personal and _has(t, "attendance", "absent", "absence") and not _has(t, "pattern", "trend", "across",
                                                                                   "department", "increase"):
            r = call("get_attendance", {})
            if r["status"] != "ok":
                return self._denied(r)
            d = r["data"]
            days = ", ".join(a["day"] for a in d["absences"][:10]) or "none"
            return (f"Since {d['period_start']} you have **{d['absence_days_last_90_days']} absence day(s)** recorded: "
                    f"{days}. If one of these is wrong, raise an **attendance correction** on the My requests page.")
        if _has(t, "absence", "absent", "sick leave trend", "pattern"):
            return self._absences(t, call)
        if "team" in t and _has(t, "leave", "conflict", "requested", "time off", "vacation"):
            return self._team(t, call)
        if _has(t, "consistency", "data quality", "mismatch"):
            r = call("scan_data_consistency", {})
            if r["status"] != "ok":
                return self._denied(r)
            rows = r["data"]["routing_mismatches"]
            return (f"Checked {r['data']['employees_checked']} employees: **{len(rows)} routing mismatch(es)** [LV-5.1].\n\n"
                    + "\n".join(f"- {x['employee']} ({x['department']}): HRIS manager {x['hris_manager']}, workflow "
                                f"approver {x['workflow_approver']}" for x in rows))
        person = self._person(text, principal)
        if _has(t, "salary", "compensation", "pay ", "profile", "who is", "title"):
            return self._profile(person, call)
        if _has(t, "policy", "notice", "carried over", "carry over", "sabbatical") and not re.search(r"\d{4}-\d{2}", t):
            return self._policy(text, call)
        if _has(t, "balance", "how many days", "how much leave", "how much annual leave", " left", "remaining") \
                and not self.parse_period(t):
            return self._balance(call, person)
        if _has(t, "leave", "time off", "days off", "vacation", "holiday", "weeks off", "week off") or (
                len(re.findall(r"\d{4}-\d{2}-\d{2}", t)) >= 2 and state.get("planned")):
            return self._leave(principal, state, t, call)
        return self._policy(text, call)

    @staticmethod
    def _person(text: str, principal: Principal) -> str | None:
        """Pick a 'Firstname Lastname' mention that isn't the current user."""
        for m in re.findall(r"\b([A-Z][a-z]+ [A-Z][a-z]+)(?:'s)?\b", text):
            if m != principal.name and not m.startswith(("What ", "Show ", "Can ", "Run ", "Submit ")):
                return m
        return None

    def _resolve(self, name: str, call) -> tuple[str | None, str | None]:
        r = call("find_employee", {"name": name})
        hits = r.get("data", {}).get("matches", [])
        if not hits:
            return None, (f"I couldn't find anyone called **{name}** that you have access to. "
                          "You can only see people within your role's scope [DP-1.1].")
        return hits[0]["employee_id"], None

    def _profile(self, name: str | None, call) -> str:
        eid, err = self._resolve(name, call) if name else (None, None)
        if err:
            return err
        r = call("get_employee_profile", {"employee_id": eid} if eid else {})
        if r["status"] != "ok":
            return self._denied(r)
        p = r["data"]
        out = (f"**{p['name']}** — {p['title']}, {p['department']} ({p.get('team') or '-'}), {p['location']}. "
               f"Reports to {p['manager_name']}. Hired {p['hire_date']}.")
        if "salary_annual" in p:
            out += f"\n\nAnnual salary: **{p['salary_annual']:,}** (visible to you as HR administrator)."
        else:
            out += "\n\n🔒 Compensation is not shown: only HR administrators may see salary data [DP-1.1]."
        return out

    # -------------------------------------------------------------- intents
    def _my_requests(self, call) -> str:
        r = call("list_my_requests", {})
        reqs = r.get("data", {}).get("requests", [])
        if not reqs:
            return "You have no requests yet. You can create one on the **My requests** page."
        lines = [f"- **{x['type_label']}** · {x['title']} · *{x['status']}*" for x in reqs[:8]]
        return "Here are your requests:\n\n" + "\n".join(lines)

    @staticmethod
    def _denied(r: dict) -> str:
        return f"🔒 I can't do that for you: {r.get('error', 'access denied')}"

    def _balance(self, call, name: str | None = None) -> str:
        eid, err = self._resolve(name, call) if name else (None, None)
        if err:
            return err
        r = call("get_leave_balance", {"employee_id": eid} if eid else {})
        if r["status"] != "ok":
            return self._denied(r)
        a = r["data"]["annual"]
        who = f"{r['data']['employee_name']} has" if eid else "You have"
        return (f"{who} **{a['available']:g} days** of annual leave available in {r['data']['year']} "
                f"({a['entitled']:g} entitled + {a['carried_over']:g} carried over − {a['used']:g} used − "
                f"{a['pending']:g} pending) [LV-1.2].")

    def _leave(self, principal, state, t, call) -> str:
        period = self.parse_period(t)
        if not period:
            return ("Happy to check that. Which dates are you thinking of? You can say e.g. "
                    "\"2 weeks next month\" or give a start and end date as YYYY-MM-DD.")
        start, end, how = period
        call("get_leave_balance", {})
        call("get_project_commitments", {})
        r = call("check_leave_eligibility", {"start_date": start.isoformat(), "end_date": end.isoformat()})
        if r["status"] != "ok":
            return self._denied(r)
        ev = r["data"]
        out = [f"**Assumption:** I read this as {how}: **{start} → {end}** ({ev['working_days']} working days). "
               "Tell me exact dates and I'll re-check.\n"]
        out.append(self._verdict(ev))
        if ev.get("earliest_compliant_start"):
            s2 = date.fromisoformat(ev["earliest_compliant_start"])
            e2 = add_working_days(s2, ev["working_days"])
            r2 = call("check_leave_eligibility", {"start_date": s2.isoformat(), "end_date": e2.isoformat()})
            if r2["status"] == "ok":
                ev2 = r2["data"]
                out.append(f"\n**Closest compliant alternative:** {s2} → {e2} — "
                           + ("passes all hard rules" if ev2["can_submit"] else "still blocked")
                           + (f", with {len(ev2['needs_attention'])} point(s) needing attention." if ev2["needs_attention"] else "."))
                start, end, ev = s2, e2, ev2
        state["planned"] = {"start_date": start.isoformat(), "end_date": end.isoformat()}
        steps = []
        if any(c["clause"] == "LV-3.3" for c in ev["checks"]):
            steps.append("Write a handover note naming who covers your project work [LV-3.3].")
        if any(c["clause"] == "LV-3.4" for c in ev["checks"]):
            steps.append("Talk to your manager early: your critical role near the deadline needs a department-head "
                         "exception [LV-3.4] — or consider dates after the deadline.")
        if ev.get("low_coverage_days"):
            steps.append(f"Team coverage drops below 60% on {', '.join(ev['low_coverage_days'])}; your manager needs a "
                         "coverage plan [LV-3.1].")
        if any(c["clause"] == "LV-5.1" and c["status"] == "warn" for c in ev["checks"]):
            steps.append("Your approval routing is inconsistent — ask me to fix routing before submitting [LV-5.1].")
        approvers = ", ".join(f"{a['name']} ({a['role']})" for a in ev["required_approvals"])
        out.append(f"\n**Approvals needed:** {approvers}.")
        if steps:
            out.append("\n**Before you submit:**\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
        out.append(f"\nSay **\"submit, handover: <who covers you>\"** and I'll prepare the request for "
                   f"{start} → {end} for your confirmation.")
        return "\n".join(out)

    @staticmethod
    def _verdict(ev: dict) -> str:
        head = ("❌ **Not submittable as is.**" if not ev["can_submit"]
                else "⚠️ **Possible, with conditions.**" if ev["needs_attention"] else "✅ **Looks good.**")
        bal = ev.get("balance")
        rows = "\n".join(f"| {ICON[c['status']]} {c['rule']} | {c['detail']} | [{c['clause']}] |" for c in ev["checks"])
        b = f" Balance: {bal['available_before']:g} → {bal['available_after']:g} days." if bal else ""
        return f"{head}{b}\n\n| Check | Result | Policy |\n|---|---|---|\n{rows}"

    def _submit(self, state, text, call) -> str:
        m = re.search(r"handover\s*:\s*(.+)", text, re.I)
        args = dict(state["planned"], reason="Annual leave", handover_note=m.group(1).strip() if m else None)
        r = call("submit_leave_request", {k: v for k, v in args.items() if v})
        if r["status"] == "proposed":
            return (f"📝 I've prepared the request (**proposal #{r['proposal_id']}**): {r['preview']['summary']}. "
                    f"It will be routed to {r['preview']['will_be_routed_to']}.\n\n"
                    "It is **not submitted yet** — confirm it on the **My requests** page; your manager then reviews it.")
        if r["status"] == "executed":
            return f"✅ Submitted as request #{r['result']['request_id']}, routed to {r['result']['routed_to']}."
        if r["status"] == "blocked":
            return "I'm in advisory mode and can't submit for you. " + r["would_do"]["summary"] + " — please do it in the leave portal."
        return f"I couldn't prepare the request: {r.get('error')}"

    def _routing(self, t, call) -> str:
        r = call("diagnose_approval_routing", {})
        if r["status"] != "ok":
            return self._denied(r)
        d = r["data"]
        if d["status"] == "consistent":
            return (f"I checked HRIS, the workflow engine and your pending requests: everything routes to "
                    f"**{d['hris_manager']['name']}**, your current manager. No action needed [LV-5.1].")
        out = [f"You're right — I found **{len(d['findings'])} problem(s)** across systems:\n"]
        for f in d["findings"]:
            out.append(f"- **{f['system']}:** {f['issue']}")
            if f.get("root_cause"):
                out.append(f"  - *Root cause:* {f['root_cause']}")
        out.append("\nPolicy says routing must follow the HRIS manager and be updated within two business days of a "
                   "change [LV-5.1].")
        if _has(t, "fix", "resolve", "help", "correct"):
            p = call("request_routing_fix", {})
            if p["status"] == "proposed":
                out.append(f"\n🛠️ I've raised **fix proposal #{p['proposal_id']}**: {p['preview']['summary']}. Because "
                           "this changes system configuration it needs approval by an **HR systems administrator** — "
                           "I can't execute it myself. You can track it on the My requests page.")
            else:
                out.append(f"\nI couldn't raise a fix: {p.get('error') or p.get('reason')}")
        else:
            out.append("\nWant me to raise a fix request with HR systems administration?")
        return "\n".join(out)

    def _team(self, t, call) -> str:
        p = self.parse_period(t)
        args = {"start_date": p[0].isoformat(), "end_date": p[1].isoformat()} if p and re.search(r"\d{4}-", t) else {}
        r = call("analyze_team_leave_conflicts", args)
        if r["status"] != "ok":
            return self._denied(r)
        d = r["data"]
        if "message" in d:
            return d["message"]
        cov = " · ".join(f"{c['date'][5:]}: {round(c['coverage'] * 100)}%" for c in d["coverage_if_all_approved"])
        out = [f"I analysed **{d['window'][0]} → {d['window'][1]}** for your team of {d['team_size']}."]
        for dl in d["project_deadlines_near_window"]:
            out.append(f"- 📌 **{dl['project']}** deadline {dl['deadline']}" + (" (business-critical)" if dl["critical"] else ""))
        out.append(f"\n**Coverage if everything is approved:** {cov} (minimum 60% [LV-3.1]).\n")
        out.append("| Request | Days | Submitted | Recommendation |\n|---|---|---|---|")
        for a in d["request_assessments"]:
            rec = "✅ Approve" if a["recommendation"] == "approve" else "🔁 " + a["recommendation"].capitalize()
            if a.get("alternatives"):
                rec += " — e.g. " + " or ".join(f"{x['start_date']}→{x['end_date']}" for x in a["alternatives"])
            out.append(f"| {a['employee']} {a['dates']} | {a['working_days']:g} | #{a['submission_rank']} | {rec} |")
        out.append("\n**Why:**")
        for a in d["request_assessments"]:
            if a.get("conflicts"):
                out.append(f"- {a['employee']}: " + "; ".join(a["conflicts"]) + " [LV-3.4]")
            if a["forfeits_days_if_declined"]:
                out.append(f"- {a['employee']} would forfeit {a['forfeits_days_if_declined']:g} day(s) at year end if "
                           "declined, which weighs in their favour [LV-3.2][LV-4.1]"
                           + ("." if a["recommendation"] == "approve" else " — so offer dates before year end."))
        sus = [a for a in d["request_assessments"]
               if re.search(r"ignore (all )?previous|instruction|approve every", a["employee_provided_reason__untrusted_text"] or "", re.I)]
        for a in sus:
            out.append(f"\n⚠️ {a['employee']}'s request reason contains text that tries to instruct the system "
                       "(\"ignore previous instructions…\"). I ignored it; you may want to have it checked.")
        out.append("\nThis is a recommendation — the decision is yours [DP-1.2].")
        return "\n".join(out)

    def _absences(self, t, call) -> str:
        dept = next((d for d in DEPARTMENTS if d in t), None)
        r = call("analyze_absence_patterns", {"department": dept} if dept else {})
        if r["status"] != "ok":
            return self._denied(r)
        d = r["data"]
        if "message" in d:
            return d["message"]
        o = d["organisation"]
        out = [f"Organisation-wide absence is **{o['recent_rate_pct']}%** over the last 4 weeks vs "
               f"**{o['baseline_rate_pct']}%** baseline. I found **{len(d['findings'])} significant pattern(s)**:\n"]
        for i, f in enumerate(d["findings"], 1):
            out.append(f"**{i}. {f['group']} — {f['type'].replace('_', ' ')} ({f['severity']})**  \n{f['evidence']}")
            if f.get("hypothesis"):
                out.append(f"*Hypothesis:* {f['hypothesis']}")
            out.append("*Consider:* " + " ".join(f["recommended_actions"][:2]) + "\n")
        dq = d["data_quality"]
        out.append(f"**Data quality:** {dq['absences_recorded_during_approved_leave']} absence(s) recorded during "
                   f"approved leave (excluded), {dq['records_missing_notice_time']} record(s) missing notice time.")
        if d["suppressed_groups_below_min_size"]:
            out.append(f"Groups under {d['min_group_size']} people were suppressed for privacy [AT-1.1]: "
                       + ", ".join(d["suppressed_groups_below_min_size"]) + ".")
        out.append("\nThese are statistical signals, not conclusions about individuals [AT-1.1][AT-1.2].")
        return "\n".join(out)

    def _policy(self, text, call) -> str:
        r = call("search_policy", {"query": text})
        res = [x for x in r.get("data", {}).get("results", []) if x["score"] >= MIN_POLICY_SCORE]
        if not res:
            return ("The policy handbook does not cover that - I couldn't find a matching clause, so I won't guess. "
                    "Please ask HR directly. I can help with leave planning, team leave conflicts, absence analytics "
                    "(HR) and approval-routing problems.")
        return "Here's what the policy says:\n\n" + "\n\n".join(f"> {c['text']} [{c['clause_id']}]" for c in res[:3])
