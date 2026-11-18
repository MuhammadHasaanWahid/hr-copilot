"""Tool catalogue + ToolGateway.

Tools are the *only* way the agent touches enterprise systems. Each tool
declares: a typed input model (validated before execution), a coarse
permission, a risk tier, and a business category (for value metrics).

    READ  - executes immediately, after authorisation + field redaction
    LOW   - self-service write (e.g. submit my own leave request); behaviour set
            by the autonomy dial: blocked / requester confirms / auto-execute
    HIGH  - changes other people's data or system configuration; ALWAYS needs an
            authorised human approver who is not the requester
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .actions import ActionService
from .analytics import AbsenceAnalyzer, ConsistencyChecker, TeamConflictAnalyzer
from .audit import AuditLog
from .config import Settings
from .policy.engine import LeavePolicyEngine
from .policy.knowledge import PolicyKnowledgeBase
from .security import AuthorizationError, Principal, access_scope, redact, require, untrusted
from .systems import Systems

READ, LOW, HIGH = "read", "low", "high"


@dataclass
class Ctx:
    principal: Principal
    s: Systems
    engine: LeavePolicyEngine
    conflicts: TeamConflictAnalyzer
    absences: AbsenceAnalyzer
    consistency: ConsistencyChecker
    kb: PolicyKnowledgeBase
    settings: Settings


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class ToolSpec:
    name: str
    description: str
    args: type[_Args]
    permission: str
    risk: str
    category: str
    run: Callable[[Ctx, Any], dict]                       # READ: result. WRITE: prepare -> preview dict
    commit: Callable[[Ctx, Any, Principal], dict] | None = None
    approval_permission: str | None = None

    def schema(self) -> dict:
        s = self.args.model_json_schema()
        s.pop("title", None)
        for p in s.get("properties", {}).values():
            p.pop("title", None)
            if "anyOf" in p:  # Optional[X] -> X (nullable is implied by not being required)
                non_null = [x for x in p.pop("anyOf") if x.get("type") != "null"]
                p.update(non_null[0] if non_null else {})
            p.pop("default", None) if p.get("default") is None else None
        return {"name": self.name, "description": self.description, "input_schema": s}


# --------------------------------------------------------------------------- helpers
def _target(ctx: Ctx, employee_id: str | None) -> str:
    return employee_id or ctx.principal.id


def _name(ctx: Ctx, eid: str | None) -> str | None:
    e = ctx.s.hris.get(eid) if eid else None
    return e["name"] if e else eid


# ============================================================================ READ tools
class ProfileArgs(_Args):
    employee_id: str | None = Field(None, description="Employee ID, e.g. E1001. Omit for the current user.")


def t_profile(ctx: Ctx, a: ProfileArgs) -> dict:
    eid = _target(ctx, a.employee_id)
    scope = access_scope(ctx.principal, eid, "profile")
    emp = ctx.s.hris.get(eid)
    if not emp:
        raise ValueError(f"No employee {eid}")
    out = redact(ctx.principal, emp)
    out["manager_name"] = _name(ctx, emp["manager_id"])
    out["access_scope"] = scope
    return out


class FindArgs(_Args):
    name: str = Field(description="Full or partial employee name.")


def t_find(ctx: Ctx, a: FindArgs) -> dict:
    hits = []
    for e in ctx.s.hris.find_by_name(a.name):
        try:
            access_scope(ctx.principal, e["id"], "profile")
        except AuthorizationError:
            continue  # silently exclude people outside the caller's scope (no existence leak)
        hits.append({"employee_id": e["id"], "name": e["name"], "title": e["title"], "department": e["department"]})
    return {"matches": hits[:10]}


class BalanceArgs(_Args):
    employee_id: str | None = Field(None, description="Omit for the current user.")
    year: int | None = Field(None, description="Calendar year; defaults to the current year.")


def t_balance(ctx: Ctx, a: BalanceArgs) -> dict:
    eid = _target(ctx, a.employee_id)
    access_scope(ctx.principal, eid, "leave")
    year = a.year or ctx.settings.today.year
    out = {"employee_id": eid, "employee_name": _name(ctx, eid), "year": year, "clauses": ["LV-1.1", "LV-1.2", "LV-4.1"]}
    for lt in ("annual", "sick"):
        out[lt] = ctx.s.leave.balance(eid, lt, year)
    return out


class RequestsArgs(_Args):
    employee_id: str | None = Field(None, description="Omit for the current user (ignored if team=true).")
    team: bool = Field(False, description="Managers: list requests of all direct reports.")
    status: Literal["pending", "approved", "all"] = "all"


def t_requests(ctx: Ctx, a: RequestsArgs) -> dict:
    statuses = ("pending", "approved") if a.status == "all" else (a.status,)
    if a.team:
        require(ctx.principal, "leave.read.team") if not ctx.principal.can("leave.read.all") else None
        ids = list(ctx.principal.direct_reports)
    else:
        eid = _target(ctx, a.employee_id)
        access_scope(ctx.principal, eid, "leave")
        ids = [eid]
    rows = ctx.s.leave.requests(ids, statuses)
    for r in rows:
        r["employee_name"] = _name(ctx, r["employee_id"])
        r["approver_name"] = _name(ctx, r["approver_id"])
        r["employee_provided_reason__untrusted_text"] = untrusted(r.pop("reason"))
        r["handover_note__untrusted_text"] = untrusted(r.pop("handover_note"))
    return {"requests": rows}


class EligibilityArgs(_Args):
    start_date: date = Field(description="First day of leave (YYYY-MM-DD).")
    end_date: date = Field(description="Last day of leave (YYYY-MM-DD), inclusive.")
    leave_type: Literal["annual", "sick"] = "annual"
    employee_id: str | None = Field(None, description="Omit for the current user. Managers may check direct reports.")


def t_eligibility(ctx: Ctx, a: EligibilityArgs) -> dict:
    eid = _target(ctx, a.employee_id)
    access_scope(ctx.principal, eid, "leave")
    return ctx.engine.evaluate_leave(eid, a.leave_type, a.start_date, a.end_date)


class ProjectArgs(_Args):
    employee_id: str | None = Field(None, description="Omit for the current user.")
    start_date: date | None = Field(None, description="Optional window start; only deadlines in window returned.")
    end_date: date | None = None


def t_projects(ctx: Ctx, a: ProjectArgs) -> dict:
    eid = _target(ctx, a.employee_id)
    access_scope(ctx.principal, eid, "project")
    rows = ctx.s.projects.assignments_for([eid])
    if a.start_date and a.end_date:
        rows = [r for r in rows if a.start_date.isoformat() <= r["deadline"] <= a.end_date.isoformat()]
    for r in rows:
        r["critical"] = bool(r["critical"])
        r["critical_role"] = bool(r["critical_role"])
    return {"employee_id": eid, "assignments": rows}


class ConflictArgs(_Args):
    start_date: date | None = Field(None, description="Window start. Omit to auto-detect the busiest upcoming week.")
    end_date: date | None = None
    manager_id: str | None = Field(None, description="HR only: analyse another manager's team. Omit for own team.")


def t_conflicts(ctx: Ctx, a: ConflictArgs) -> dict:
    mid = a.manager_id or ctx.principal.id
    if mid != ctx.principal.id:
        require(ctx.principal, "leave.read.all")
    return ctx.conflicts.analyze(mid, a.start_date, a.end_date)


class AbsenceArgs(_Args):
    department: str | None = Field(None, description="Limit to one department, e.g. 'Customer Support'.")
    recent_weeks: int = Field(4, ge=1, le=12, description="Length of the recent window compared to the baseline.")


def t_absences(ctx: Ctx, a: AbsenceArgs) -> dict:
    return ctx.absences.analyze(a.department, a.recent_weeks)


class DiagnoseArgs(_Args):
    employee_id: str | None = Field(None, description="Omit for the current user. HR may diagnose anyone.")


def t_diagnose(ctx: Ctx, a: DiagnoseArgs) -> dict:
    eid = _target(ctx, a.employee_id)
    if eid != ctx.principal.id:
        require(ctx.principal, "routing.diagnose.all")
    return ctx.consistency.diagnose(eid)


class NoArgs(_Args):
    pass


def t_scan(ctx: Ctx, a: NoArgs) -> dict:
    return ctx.consistency.scan()


class PolicyArgs(_Args):
    query: str = Field(description="Natural-language policy question or keywords.")


def t_policy(ctx: Ctx, a: PolicyArgs) -> dict:
    return {"results": ctx.kb.search(a.query)}


# =========================================================================== WRITE tools
class SubmitArgs(_Args):
    start_date: date
    end_date: date
    leave_type: Literal["annual", "sick"] = "annual"
    reason: str = Field("", max_length=300)
    handover_note: str | None = Field(None, max_length=1000,
                                         description="Required if a project deadline falls inside the leave (LV-3.3).")


def p_submit(ctx: Ctx, a: SubmitArgs) -> dict:
    """Prepare/validate (also re-run at execution time)."""
    ev = ctx.engine.evaluate_leave(ctx.principal.id, a.leave_type, a.start_date, a.end_date)
    if not ev["can_submit"]:
        raise ValueError("Request violates policy and cannot be submitted: " + " ".join(ev["blocking_issues"]))
    if any(c["clause"] == "LV-3.3" for c in ev["checks"]) and not (a.handover_note or "").strip():
        raise ValueError("A handover note is required because a project deadline falls inside the leave (LV-3.3).")
    route = ctx.s.workflow.route(ctx.principal.id)
    return {"summary": f"Submit {ev['working_days']} day(s) {a.leave_type} leave {a.start_date} to {a.end_date}",
            "will_be_routed_to": _name(ctx, route["approver_id"]) if route else None,
            "warnings": ev["needs_attention"], "required_approvals": ev["required_approvals"]}


def c_submit(ctx: Ctx, a: SubmitArgs, approver: Principal) -> dict:
    p_submit(ctx, a)
    route = ctx.s.workflow.route(ctx.principal.id)
    n = ctx.engine.evaluate_leave(ctx.principal.id, a.leave_type, a.start_date, a.end_date)["working_days"]
    rid = ctx.s.leave.create_request(ctx.principal.id, a.leave_type, a.start_date, a.end_date, n,
                                     route["approver_id"], ctx.settings.today, a.reason, a.handover_note)
    return {"request_id": rid, "status": "pending", "routed_to": _name(ctx, route["approver_id"])}


class RoutingFixArgs(_Args):
    employee_id: str | None = Field(None, description="Omit for the current user.")


def p_fix(ctx: Ctx, a: RoutingFixArgs) -> dict:
    eid = _target(ctx, a.employee_id)
    if eid != ctx.principal.id:
        require(ctx.principal, "routing.request_fix.all")
    d = ctx.consistency.diagnose(eid)
    if not d["proposed_fix"]:
        raise ValueError("Routing is already consistent with HRIS; nothing to fix.")
    f = d["proposed_fix"]
    return {"summary": f"Re-route approvals for {d['employee']} to {f['set_route_to']['name']} and reassign "
                       f"{len(f['reassign_pending_requests'])} pending request(s)",
            "findings": [x["issue"] for x in d["findings"]], "fix": f}


def c_fix(ctx: Ctx, a: RoutingFixArgs, approver: Principal) -> dict:
    eid = _target(ctx, a.employee_id)
    f = p_fix(ctx, a)["fix"]  # re-validate against current state
    new = f["set_route_to"]["id"]
    with ctx.s.db.transaction():
        ctx.s.workflow.set_route(eid, new, ctx.settings.today, f"hr_fix_by_{approver.id}")
        moved = ctx.s.leave.reassign_pending(eid, new)
    return {"route_now": f["set_route_to"]["name"], "pending_requests_reassigned": moved}


# =============================================================================== catalogue
TOOLS: list[ToolSpec] = [
    ToolSpec("get_employee_profile", "Get an employee's HRIS profile (title, department, manager, hire date). "
             "Sensitive fields are removed unless the caller is entitled to them.",
             ProfileArgs, "profile.read.self", READ, "profile_lookup", t_profile),
    ToolSpec("find_employee", "Resolve a person's name to an employee ID. Only returns people the caller may see.",
             FindArgs, "profile.read.self", READ, "profile_lookup", t_find),
    ToolSpec("get_leave_balance", "Get annual and sick leave balance (entitled, carried over, used, pending, available).",
             BalanceArgs, "leave.read.self", READ, "leave_planning", t_balance),
    ToolSpec("list_leave_requests", "List pending/approved leave requests for an employee or the caller's team.",
             RequestsArgs, "leave.read.self", READ, "leave_planning", t_requests),
    ToolSpec("check_leave_eligibility",
             "Run the deterministic policy engine on a proposed leave period: balance, notice period, approval chain, "
             "project deadlines, critical-role rules, team coverage and approval-routing consistency. Returns a "
             "verdict per rule with policy clause IDs. ALWAYS use this before advising whether leave is possible.",
             EligibilityArgs, "leave.read.self", READ, "leave_planning", t_eligibility),
    ToolSpec("get_project_commitments", "List an employee's project assignments, roles, deadlines and criticality.",
             ProjectArgs, "project.read.self", READ, "leave_planning", t_projects),
    ToolSpec("analyze_team_leave_conflicts",
             "Managers: analyse overlapping leave requests for their team. Computes daily coverage, critical-role "
             "conflicts near project deadlines, and an optimal approve/re-schedule plan with alternative dates, "
             "following policy LV-3.1/3.2/3.4. Omit dates to analyse the busiest upcoming week.",
             ConflictArgs, "leave.analyze.team", READ, "team_conflict_analysis", t_conflicts),
    ToolSpec("analyze_absence_patterns",
             "HR only: statistical absence analysis by department and team (recent vs baseline rates, z-scores, "
             "Monday/Friday skew, short-notice share, link to critical project load), with data-quality report. "
             "Aggregated, k-anonymised output; never identifies individuals.",
             AbsenceArgs, "analytics.org", READ, "absence_analytics", t_absences),
    ToolSpec("diagnose_approval_routing",
             "Cross-check HRIS reporting line, workflow-engine approval route and pending leave approvals for one "
             "employee; explains any inconsistency and its root cause and proposes a fix.",
             DiagnoseArgs, "routing.diagnose.self", READ, "routing_diagnosis", t_diagnose),
    ToolSpec("scan_data_consistency", "HR only: scan all employees for HRIS vs workflow routing mismatches.",
             NoArgs, "data_quality.scan", READ, "routing_diagnosis", t_scan),
    ToolSpec("search_policy", "Search the HR policy handbook. Returns clauses with IDs to cite.",
             PolicyArgs, "policy.read", READ, "policy_question", t_policy),
    ToolSpec("submit_leave_request",
             "Submit a leave request for the CURRENT USER. Only call after the user explicitly asked to submit and "
             "confirmed the dates. Depending on the autonomy policy this creates a proposal the user confirms with one "
             "click, or submits directly. Fails if the request breaks a hard policy rule.",
             SubmitArgs, "leave.submit.self", LOW, "leave_planning", p_submit, c_submit),
    ToolSpec("request_routing_fix",
             "Request a fix for broken approval routing (re-point the workflow route to the HRIS manager and reassign "
             "pending approvals). HIGH RISK: always creates a proposal that an HR systems administrator must approve.",
             RoutingFixArgs, "routing.request_fix.self", HIGH, "routing_diagnosis", p_fix, c_fix,
             approval_permission="routing.fix.approve"),
]


class ToolGateway:
    def __init__(self, ctx_factory: Callable[[Principal], Ctx], actions: ActionService, audit: AuditLog,
                 settings: Settings) -> None:
        self.specs = {t.name: t for t in TOOLS}
        self.ctx_factory, self.actions, self.audit, self.settings = ctx_factory, actions, audit, settings
        for t in TOOLS:
            if t.commit:
                actions.register_executor(t.name, self._executor(t))

    def _executor(self, spec: ToolSpec):
        def run(requester: Principal, args: dict, approver: Principal) -> dict:
            return spec.commit(self.ctx_factory(requester), spec.args(**args), approver)
        return run

    def tool_definitions(self) -> list[dict]:
        return [t.schema() for t in TOOLS]

    def category(self, name: str) -> str:
        return self.specs[name].category if name in self.specs else "other"

    def call(self, principal: Principal, name: str, raw_args: dict) -> dict:
        spec = self.specs.get(name)
        if not spec:
            return {"status": "error", "error": f"Unknown tool '{name}'."}
        try:
            args = spec.args(**(raw_args or {}))
        except ValidationError as exc:
            return {"status": "error", "error": "Invalid arguments: " + "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())}
        jargs = json.loads(args.model_dump_json(exclude_none=True))
        ctx = self.ctx_factory(principal)
        try:
            require(principal, spec.permission)
            if spec.risk == READ:
                data = spec.run(ctx, args)
                self.audit.record(principal.id, "tool.read", tool=name, args=jargs)
                return {"status": "ok", "data": data}

            preview = spec.run(ctx, args)  # validates + authorises the write
            autonomy = self.settings.autonomy
            if autonomy == "advisory":
                self.audit.record(principal.id, "tool.write_blocked", tool=name, args=jargs, autonomy=autonomy)
                return {"status": "blocked", "reason": "The assistant runs in ADVISORY mode: it may not change "
                        "any system. Tell the user how to do this themselves.", "would_do": preview}
            if spec.risk == LOW and autonomy == "autonomous":
                result = spec.commit(ctx, args, principal)
                self.audit.record(principal.id, "tool.write_executed", tool=name, args=jargs, result=result)
                return {"status": "executed", "result": result}
            if spec.risk == LOW:
                p = self.actions.create(name, jargs, principal, LOW, preview["summary"], preview, "requester", None)
                return {"status": "proposed", "proposal_id": p["id"], "awaiting": "the user's one-click confirmation "
                        "in the Actions tab", "preview": preview}
            p = self.actions.create(name, jargs, principal, HIGH, preview["summary"], preview, "permission",
                                    spec.approval_permission)
            return {"status": "proposed", "proposal_id": p["id"],
                    "awaiting": "approval by an HR systems administrator (segregation of duties)", "preview": preview}
        except AuthorizationError as exc:
            self.audit.record(principal.id, "tool.denied", tool=name, args=jargs, reason=str(exc))
            return {"status": "denied", "error": str(exc)}
        except ValueError as exc:
            self.audit.record(principal.id, "tool.rejected", tool=name, args=jargs, reason=str(exc))
            return {"status": "error", "error": str(exc)}
