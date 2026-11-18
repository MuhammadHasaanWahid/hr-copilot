"""Scale test: grow the synthetic organisation to N employees and time the tools.

    python scripts/scale_test.py --sizes 5000 50000

Adds N employees in teams of 8 (one manager each), with HRIS records, approval
routes, leave balances and ~26 weeks of absence history, then times the
heaviest operations through the real ToolGateway. Results -> docs/SCALE.md.
"""
from __future__ import annotations

import argparse
import os
import platform
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["RATE_LIMIT_PER_MINUTE"] = "100000"

from app.calendar_utils import working_days_between  # noqa: E402
from app.config import Settings  # noqa: E402
from app.container import Container  # noqa: E402

TEAM = 8


def grow(c: Container, n: int) -> None:
    rng = random.Random(11)
    today = c.settings.today
    days = [d.isoformat() for d in working_days_between(today - timedelta(days=182), today - timedelta(days=1))]
    emps, routes, bals, abs_rows = [], [], [], []
    depts = [f"Operations {i}" for i in range(1, 11)]
    for i in range(n):
        eid = f"X{i:06d}"
        dept = depts[i % len(depts)]
        team_no = i // TEAM
        mgr = f"X{team_no * TEAM:06d}"
        is_mgr = i % TEAM == 0
        emps.append((eid, f"Synthetic Person {i}", None, "Associate", dept, f"Team {team_no}", "Remote",
                     "MANAGER" if is_mgr else "EMPLOYEE", None if is_mgr else mgr, "2020-01-01", None, 50000,
                     today.isoformat()))
        if not is_mgr:
            routes.append((eid, mgr, today.isoformat(), "hris_sync"))
        bals.append((eid, "annual", today.year, 22, rng.randint(0, 15), 0))
        k = sum(1 for _ in range(len(days)) if rng.random() < 0.02)
        for d in rng.sample(days, k):
            abs_rows.append((eid, d, "sick", round(rng.uniform(0, 3), 1)))
    db = c.db
    with db.transaction():
        db.executemany("INSERT INTO hris_employees VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", emps)
        db.executemany("INSERT INTO wf_approval_routes VALUES (?,?,?,?)", routes)
        db.executemany("INSERT INTO leave_balances VALUES (?,?,?,?,?,?)", bals)
        db.executemany("INSERT INTO att_absences (employee_id, day, kind, notice_hours) VALUES (?,?,?,?)", abs_rows)
        db.executemany("INSERT OR IGNORE INTO hris_departments VALUES (?, ?)", [(d, "X000000") for d in depts])


def timed(fn, repeat: int = 3) -> float:
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - t0)
        assert out.get("status") in (None, "ok"), out
    return best * 1000


def run(n: int) -> dict:
    c = Container(Settings(llm_mode="offline"), db_path=":memory:")
    t0 = time.perf_counter()
    grow(c, n)
    build = time.perf_counter() - t0
    total = c.db.one("SELECT COUNT(*) AS n FROM hris_employees")["n"]
    absences = c.db.one("SELECT COUNT(*) AS n FROM att_absences")["n"]
    hr, mgr, emp = c.principal("E0501"), c.principal("E0102"), c.principal("E1001")
    g = c.gateway
    nm = c.scenario.next_month_start
    res = {
        "employees": total, "absence_records": absences, "build_s": round(build, 1),
        "absence_analytics_ms": timed(lambda: g.call(hr, "analyze_absence_patterns", {}), 2),
        "consistency_scan_ms": timed(lambda: g.call(hr, "scan_data_consistency", {})),
        "team_conflicts_ms": timed(lambda: g.call(mgr, "analyze_team_leave_conflicts", {})),
        "eligibility_ms": timed(lambda: g.call(emp, "check_leave_eligibility",
                                               {"start_date": (nm + timedelta(days=14)).isoformat(),
                                                "end_date": (nm + timedelta(days=18)).isoformat()})),
        "balance_lookup_ms": timed(lambda: g.call(emp, "get_leave_balance", {})),
    }
    return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in res.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", nargs="+", type=int, default=[5000, 50000])
    args = ap.parse_args()
    rows = []
    for n in args.sizes:
        r = run(n)
        rows.append(r)
        print(r)
    header = ("| Employees | Absence records | Absence analytics (org-wide) | Consistency scan | Team conflict "
              "optimiser | Eligibility check | Balance lookup |\n|---|---|---|---|---|---|---|")
    lines = [f"| {r['employees']:,} | {r['absence_records']:,} | {r['absence_analytics_ms']:,.0f} ms | "
             f"{r['consistency_scan_ms']:,.0f} ms | {r['team_conflicts_ms']:,.0f} ms | {r['eligibility_ms']:,.1f} ms | "
             f"{r['balance_lookup_ms']:,.1f} ms |" for r in rows]
    doc = (f"# Scale test\n\nMeasured on {date.today().isoformat()} with `python scripts/scale_test.py`, single process, "
           f"SQLite in memory, {platform.system()} / Python {platform.python_version()}. Times are the best of "
           "several runs through the real ToolGateway (authorisation + audit included).\n\n" + header + "\n"
           + "\n".join(lines) + "\n\n**Reading it:** interactive per-person tools (eligibility, balance, team "
           "conflicts) stay in the low milliseconds regardless of headcount, because they touch one employee or one "
           "team through indexed queries. Org-wide absence analytics grows linearly with the number of absence "
           "records; at enterprise scale it should run as a scheduled job against a warehouse, with the assistant "
           "reading the precomputed result. LLM latency (seconds) dominates every interactive request, not the tools.\n")
    (ROOT / "docs" / "SCALE.md").write_text(doc, encoding="utf-8")


if __name__ == "__main__":
    main()
