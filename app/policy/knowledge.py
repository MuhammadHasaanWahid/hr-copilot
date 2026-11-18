"""Policy knowledge base with clause-level retrieval.

Policies are split into addressable clauses (e.g. LV-2.1). Retrieval is BM25
(no external services, deterministic, explainable). Every answer the assistant
gives about policy must cite clause IDs, and the orchestrator verifies those
IDs exist - a cheap but effective guard against invented policy.

Production path: swap BM25 for hybrid (BM25 + embeddings) retrieval over a
versioned policy store with per-country variants; the Clause interface stays.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

CLAUSE_RE = re.compile(r"^\[([A-Z]{2}-\d+\.\d+)\]\s*(.+)$")
TOKEN_RE = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "is", "be", "may", "must", "are",
        "by", "with", "any", "this", "that", "their", "than", "at", "as", "it", "i", "my", "can", "do",
        "how", "what", "much", "have", "will", "should"}


@dataclass(frozen=True)
class Clause:
    id: str
    title: str
    text: str


def _tokens(text: str) -> list[str]:
    toks = [t for t in TOKEN_RE.findall(text.lower()) if t not in STOP]
    # crude stemming keeps the PoC dependency-free
    return [t[:-1] if t.endswith("s") and len(t) > 3 else t for t in toks]


class PolicyKnowledgeBase:
    def __init__(self, path: Path | None = None) -> None:
        path = path or Path(__file__).with_name("handbook.md")
        self.clauses: dict[str, Clause] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            m = CLAUSE_RE.match(line.strip())
            if m:
                cid, body = m.groups()
                title, _, rest = body.partition(". ")
                self.clauses[cid] = Clause(cid, title, body)
        self._docs = {cid: _tokens(c.text) for cid, c in self.clauses.items()}
        self._avgdl = sum(map(len, self._docs.values())) / max(1, len(self._docs))
        n = len(self._docs)
        df: dict[str, int] = {}
        for toks in self._docs.values():
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def get(self, clause_id: str) -> Clause | None:
        return self.clauses.get(clause_id)

    def search(self, query: str, k: int = 4, k1: float = 1.5, b: float = 0.75) -> list[dict]:
        q = _tokens(query)
        scored = []
        for cid, toks in self._docs.items():
            score = 0.0
            for t in q:
                tf = toks.count(t)
                if tf:
                    score += self._idf.get(t, 0) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(toks) / self._avgdl))
            if score > 0:
                scored.append((score, cid))
        scored.sort(reverse=True)
        return [{"clause_id": cid, "title": self.clauses[cid].title, "text": self.clauses[cid].text,
                 "score": round(s, 3)} for s, cid in scored[:k]]

    def validate_citations(self, text: str) -> tuple[list[str], list[str]]:
        """Return (valid, invalid) clause IDs referenced in free text."""
        cited = sorted(set(re.findall(r"\b([A-Z]{2}-\d+\.\d+)\b", text)))
        return [c for c in cited if c in self.clauses], [c for c in cited if c not in self.clauses]
