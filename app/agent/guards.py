"""Post-generation guards against hallucination (OWASP LLM: misinformation).

1. Grounding  - every number, ISO date and employee name in the answer must
                appear in the evidence of this turn (tool results, the user's
                own message, the session context). Unsupported claims trigger
                one automatic correction round, then a visible warning.
2. Action claims - the answer may only say something was submitted / approved /
                fixed if a tool actually returned status "executed" this turn.
3. Citations  - policy clause IDs must exist in the handbook (knowledge.py).

These are deterministic, cheap and explainable. They do not prove an answer is
right, but they catch the most damaging failure: confident invented facts.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

NUM_RE = re.compile(r"(?<![\w.-])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(\s?%)?(?![\w-])")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
CLAUSE_RE = re.compile(r"\b[A-Z]{2}-\d+\.\d+\b")
LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s", re.M)
TABLE_RULE_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$", re.M)
CLAIM_RE = re.compile(
    r"\b(?:has|have|had)\s+(?:now\s+)?been\s+(?:successfully\s+)?(submitted|approved|fixed|updated|reassigned|executed|re-?routed)\b"
    r"|\bI(?:'ve| have)\s+(?:now\s+)?(?:successfully\s+)?(submitted|approved|fixed|updated|reassigned|executed|re-?routed)\b"
    r"|\b(?:is|are)\s+now\s+(fixed|approved|submitted|reassigned|re-?routed)\b"
    r"|\bsuccessfully\s+(submitted|approved|fixed|updated|reassigned|re-?routed)\b",
    re.I)
NEGATION_RE = re.compile(r"\b(not|n't|never|cannot|can't|once|after|until|when|if|will)\b[^.]{0,40}$", re.I)
SMALL_NUMBERS = {str(i) for i in range(0, 11)}  # counts like "2 problems" are too common to verify usefully


@dataclass
class GuardReport:
    unsupported: list[str] = field(default_factory=list)
    false_action_claims: list[str] = field(default_factory=list)
    invalid_citations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.unsupported or self.false_action_claims or self.invalid_citations)

    def as_dict(self) -> dict:
        return {"verified": self.ok, "unsupported_claims": self.unsupported,
                "false_action_claims": self.false_action_claims, "invalid_citations": self.invalid_citations}


def _norm_num(s: str) -> str:
    s = s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return s
    return str(int(f)) if f == int(f) else f"{f:g}"


def _evidence_numbers(text: str) -> set[str]:
    nums = set()
    for m in re.finditer(r"-?\d+(?:\.\d+)?", text.replace(",", "")):
        n = _norm_num(m.group().lstrip("-"))
        nums.add(n)
        try:  # 0.57 in data == "57%" in prose
            f = float(n)
            if f <= 1:
                nums.add(_norm_num(str(round(f * 100, 1))))
                nums.add(_norm_num(str(round(f * 100))))
        except ValueError:
            pass
    return nums


def check(reply: str, evidence: list, employee_names: list[str], executed_actions: int, kb) -> GuardReport:
    rep = GuardReport()
    ev_text = json.dumps(evidence, default=str, ensure_ascii=False)
    ev_nums = _evidence_numbers(ev_text)
    ev_lower = ev_text.lower()

    body = CLAUSE_RE.sub(" ", reply)                 # clause IDs are checked separately
    body = re.sub(r"\(https?://[^)]+\)", " ", body)  # link targets
    body = LIST_MARKER_RE.sub(" ", body)
    body = TABLE_RULE_RE.sub(" ", body)

    for d in sorted(set(DATE_RE.findall(body))):
        if d not in ev_text:
            rep.unsupported.append(d)
    body_no_dates = DATE_RE.sub(" ", body)
    for m in NUM_RE.finditer(body_no_dates):
        raw = m.group(1)
        n = _norm_num(raw)
        if n in SMALL_NUMBERS or n in ev_nums:
            continue
        rep.unsupported.append(raw + (m.group(2) or "").strip())
    for name in employee_names:
        if name in reply and name.lower() not in ev_lower:
            rep.unsupported.append(name)
    rep.unsupported = sorted(set(rep.unsupported))

    if executed_actions == 0:
        for m in CLAIM_RE.finditer(reply):
            prefix = reply[max(0, m.start() - 60):m.start()]
            if NEGATION_RE.search(prefix):
                continue
            rep.false_action_claims.append(m.group(0))

    _, rep.invalid_citations = kb.validate_citations(reply)
    return rep


def correction_prompt(rep: GuardReport) -> str:
    parts = ["[Automatic verification by HR Copilot - not written by the user]"]
    if rep.unsupported:
        parts.append("These figures/dates/names in your answer do not appear in any tool result: "
                     + ", ".join(rep.unsupported) + ". Remove them or replace them with values from tool results "
                     "(call a tool if needed).")
    if rep.false_action_claims:
        parts.append("You described an action as completed (" + "; ".join(rep.false_action_claims) + ") but no tool "
                     "executed it. Say precisely what is pending and who must confirm or approve it.")
    if rep.invalid_citations:
        parts.append("These policy clause IDs do not exist: " + ", ".join(rep.invalid_citations) + ". Remove them.")
    parts.append("Rewrite your full answer for the user.")
    return "\n".join(parts)
