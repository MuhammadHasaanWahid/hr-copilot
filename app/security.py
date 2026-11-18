"""Authorisation model.

Design rule: the LLM is *never* the security boundary. Every tool call goes
through `ToolGateway`, which checks the caller's permissions and the data scope
(self / direct reports / organisation) *in code*, and redacts fields the caller
may not see before anything reaches the model. A prompt injection can at most
make the model *ask* for something - it cannot make the gateway grant it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_SELF = {"profile.read.self", "leave.read.self", "leave.submit.self", "policy.read", "project.read.self",
         "routing.diagnose.self", "routing.request_fix.self", "request.submit.self", "attendance.read.self"}
_TEAM = {"profile.read.team", "leave.read.team", "leave.analyze.team", "project.read.team", "attendance.read.team",
         "leave.review.team"}
_HR = {"profile.read.all", "leave.read.all", "leave.analyze.team", "project.read.all", "attendance.read.all",
       "analytics.org", "routing.diagnose.all", "routing.request_fix.all", "data_quality.scan",
       "hr.review",          # review employee requests (attendance, details, letters, other) + override leave
       "leave.review.all"}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "EMPLOYEE": set(_SELF),
    "MANAGER": _SELF | _TEAM,
    "HR_PARTNER": _SELF | _HR,
    "HR_ADMIN": _SELF | _HR | {"compensation.read", "routing.fix.approve", "audit.read.all", "metrics.read"},
}

ROLE_LABELS = {"EMPLOYEE": "Employee", "MANAGER": "Manager", "HR_PARTNER": "HR Business Partner",
               "HR_ADMIN": "HR Administrator"}

_INJECTION = re.compile(
    r"ignore (all |any )?(previous|prior|above)|disregard (all |the )?(previous|prior|above)|system prompt|"
    r"you are now|approve (every|all)|developer mode|admin mode|override (the )?(rules|policy)", re.I)


def looks_like_injection(text: str | None) -> bool:
    """Deterministic flag for instruction-like text inside employee-typed data."""
    return bool(text and _INJECTION.search(text))

# Fields considered sensitive and only visible with an explicit permission.
SENSITIVE_FIELDS = {"salary_annual": "compensation.read"}


class AuthorizationError(Exception):
    """Raised when a principal tries to access data or actions outside its scope."""


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    role: str
    department: str
    team: str | None = None
    direct_reports: frozenset[str] = field(default_factory=frozenset)

    @property
    def permissions(self) -> set[str]:
        return ROLE_PERMISSIONS.get(self.role, set())

    def can(self, permission: str) -> bool:
        return permission in self.permissions


def access_scope(principal: Principal, target_id: str, domain: str) -> str:
    """Return the scope under which `principal` may read `domain` data about
    `target_id` ('self' | 'team' | 'all'), or raise AuthorizationError."""
    if target_id == principal.id and principal.can(f"{domain}.read.self"):
        return "self"
    if principal.can(f"{domain}.read.all"):
        return "all"
    if target_id in principal.direct_reports and principal.can(f"{domain}.read.team"):
        return "team"
    raise AuthorizationError(
        f"{principal.role} '{principal.id}' is not authorised to read {domain} data of employee '{target_id}'.")


def require(principal: Principal, permission: str) -> None:
    if not principal.can(permission):
        raise AuthorizationError(f"Missing permission '{permission}' for role {principal.role}.")


# ASCII control chars + zero-width + bidi override/isolate chars (used to hide or reorder injected text).
_INVISIBLE = [*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F,
              *range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
_CONTROL = re.compile("[" + "".join(re.escape(chr(c)) for c in _INVISIBLE) + "]")
UNTRUSTED_OPEN, UNTRUSTED_CLOSE = "[EMPLOYEE_TEXT]", "[/EMPLOYEE_TEXT]"


def untrusted(text: str | None, limit: int = 500) -> str | None:
    """Spotlight free text typed by employees before it reaches the model:
    strip control / bidi characters, cap the length, remove look-alike markers
    and wrap it in explicit data markers the system prompt tells the model about."""
    if text is None:
        return None
    clean = _CONTROL.sub("", str(text)).replace(UNTRUSTED_OPEN, "").replace(UNTRUSTED_CLOSE, "")[:limit]
    return f"{UNTRUSTED_OPEN}{clean}{UNTRUSTED_CLOSE}"


def redact(principal: Principal, record: dict) -> dict:
    """Data minimisation: drop sensitive fields the principal may not see."""
    out = dict(record)
    for fld, perm in SENSITIVE_FIELDS.items():
        if fld in out and not principal.can(perm):
            out.pop(fld)
    return out
