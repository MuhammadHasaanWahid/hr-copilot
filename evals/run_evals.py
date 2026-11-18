"""Run the evaluation set.

    python evals/run_evals.py --mode offline            # deterministic planner, free, runs in CI
    python evals/run_evals.py --mode live               # real Claude (needs ANTHROPIC_API_KEY), costs money

Writes evals/results_<mode>.json and refreshes the matching section of docs/EVALS.md.
Exit code 1 if the pass rate is below --min-pass.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["RATE_LIMIT_PER_MINUTE"] = "100000"
# The eval set is a fixed benchmark world: pin its business date so results are comparable across days.
os.environ.setdefault("HR_DEMO_TODAY", "2026-11-18")

from app.config import Settings  # noqa: E402
from app.service import HRCopilotService  # noqa: E402
from evals.cases import CASES, after_deadline  # noqa: E402


def _expand(item, c):
    return item(c) if callable(item) else item


def _contains(reply: str, needle) -> bool:
    return needle.lower() in reply.lower()


def run_case(case: dict, mode: str) -> dict:
    settings = Settings(llm_mode="llm" if mode == "live" else "offline", app_env="demo")
    svc = HRCopilotService(settings, db_path=":memory:")
    c = svc.c
    alt_s, alt_e = after_deadline(c, 3, 14)
    d = date.fromisoformat(alt_s)
    fmt = {"alt_start": alt_s, "alt_end": alt_e, "alt_start_dm": f"{d.day} {d:%B}", "alt_start_md": f"{d:%B} {d.day}"}
    p = svc.principal(svc.login(case["persona"])["token"])
    tools, results = set(), []
    t0 = time.perf_counter()
    for turn in case["turns"]:
        msg = turn.format(**fmt)
        r = svc.chat(p, "eval", msg)
        results.append(r)
        tools |= {t["tool"] for t in r["trace"]}
    last = results[-1]
    reply = last["reply"]
    failures = []
    for t in case.get("tools", set()):
        if t not in tools:
            failures.append(f"tool not called: {t}")
    for item in case.get("include", []):
        item = _expand(item, c)
        options = [o.format(**fmt) for o in (item if isinstance(item, list) else [item])]
        if not any(_contains(reply, o) for o in options):
            failures.append(f"missing any of {options[:4]}")
    for item in case.get("exclude", []):
        for o in (_expand(item, c) if callable(item) else [item]):
            if o and _contains(reply, o):
                failures.append(f"leaked/forbidden: {o!r}")
    exp = case.get("outcome")
    if exp and last["outcome"] not in (exp if isinstance(exp, tuple) else (exp,)):
        failures.append(f"outcome {last['outcome']} != {exp}")
    executed = c.db.one("SELECT COUNT(*) AS n FROM app_proposals WHERE status='executed'")["n"]
    if executed:
        failures.append(f"{executed} proposal(s) executed without a human decision")
    if last["guard"]["false_action_claims"]:
        failures.append(f"false action claim: {last['guard']['false_action_claims']}")
    if last["guard"]["invalid_citations"]:
        failures.append(f"invalid citation: {last['guard']['invalid_citations']}")
    usage = {k: sum(r["usage"][k] for r in results) for k in ("input_tokens", "output_tokens")}
    cost = (usage["input_tokens"] * settings.price_input_per_mtok + usage["output_tokens"] * settings.price_output_per_mtok) / 1e6
    return {"id": case["id"], "persona": case["persona"], "passed": not failures, "failures": failures,
            "verified": last["guard"]["verified"], "self_corrected": any(r["self_corrected"] for r in results),
            "unsupported_claims": last["guard"]["unsupported_claims"], "mode": last["mode"],
            "tools": sorted(tools), "outcome": last["outcome"], "seconds": round(time.perf_counter() - t0, 1),
            "usage": usage, "cost_usd": round(cost, 4), "reply": reply}


def write_markdown(mode: str, rows: list[dict], summary: dict) -> None:
    doc = ROOT / "docs" / "EVALS.md"
    text = doc.read_text(encoding="utf-8") if doc.exists() else ""
    start, end = f"<!-- {mode}:start -->", f"<!-- {mode}:end -->"
    table = ["| Case | Persona | Result | Verified | Planner | Tools | s | Cost |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        table.append(f"| {r['id']} | {r['persona']} | {'✅ pass' if r['passed'] else '❌ ' + '; '.join(r['failures'])[:120]} "
                     f"| {'yes' if r['verified'] else 'no'}{' (self-corrected)' if r['self_corrected'] else ''} | {r['mode']} "
                     f"| {', '.join(r['tools'])[:80]} | {r['seconds']} | ${r['cost_usd']:.3f} |")
    block = (f"{start}\n**{mode.title()} run - {summary['date']}:** {summary['passed']}/{summary['total']} passed "
             f"({summary['pass_rate']:.0%}) · fully verified answers {summary['verified']}/{summary['total']} · "
             f"data leaks {summary['leaks']} · unapproved actions {summary['unapproved_actions']} · "
             f"total cost ${summary['cost_usd']:.2f} · avg {summary['avg_seconds']} s per case\n\n" + "\n".join(table) + f"\n{end}")
    if start in text:
        text = text[:text.index(start)] + block + text[text.index(end) + len(end):]
    else:
        text += ("\n\n" if text else "") + f"## {mode.title()} results\n\n" + block + "\n"
    doc.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["offline", "live"], default="offline")
    ap.add_argument("--min-pass", type=float, default=1.0)
    ap.add_argument("--only", default="", help="comma-separated case ids")
    ap.add_argument("--pause", type=float, default=0.0,
                    help="seconds to wait between cases (free-tier per-minute limits; e.g. 20)")
    args = ap.parse_args()
    if args.mode == "live" and not Settings().api_key_present:
        print("No API key for the configured provider (set GEMINI_API_KEY in .env).")
        return 2
    cases = [c for c in CASES if ("live" if args.mode == "live" else "offline") in c["modes"]]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]
    rows = []
    for i, case in enumerate(cases):
        if i and args.pause:
            time.sleep(args.pause)
        r = run_case(case, args.mode)
        rows.append(r)
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']:<36} {r['mode']:<20} {r['seconds']:>5}s  ${r['cost_usd']:.3f}"
              + ("" if r["passed"] else "  -> " + "; ".join(r["failures"])))
    summary = {"date": date.today().isoformat(), "total": len(rows), "passed": sum(r["passed"] for r in rows),
               "verified": sum(r["verified"] for r in rows),
               "leaks": sum(any(f.startswith("leaked") for f in r["failures"]) for r in rows),
               "unapproved_actions": sum(any("executed without" in f for f in r["failures"]) for r in rows),
               "cost_usd": sum(r["cost_usd"] for r in rows),
               "avg_seconds": round(sum(r["seconds"] for r in rows) / max(1, len(rows)), 1)}
    summary["pass_rate"] = summary["passed"] / max(1, summary["total"])
    (ROOT / "evals" / f"results_{args.mode}.json").write_text(json.dumps({"summary": summary, "cases": rows}, indent=1),
                                                              encoding="utf-8")
    if not args.only:
        write_markdown(args.mode, rows, summary)
    print(f"\n{summary['passed']}/{summary['total']} passed ({summary['pass_rate']:.0%}), verified "
          f"{summary['verified']}/{summary['total']}, leaks {summary['leaks']}, cost ${summary['cost_usd']:.2f}")
    return 0 if summary["pass_rate"] >= args.min_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
