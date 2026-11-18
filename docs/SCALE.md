# Scale test

Measured on 2026-11-18 with `python scripts/scale_test.py`, single process, SQLite in memory, Windows / Python 3.12.10. Times are the best of several runs through the real ToolGateway (authorisation + audit included).

| Employees | Absence records | Absence analytics (org-wide) | Consistency scan | Team conflict optimiser | Eligibility check | Balance lookup |
|---|---|---|---|---|---|---|
| 5,057 | 13,147 | 56 ms | 1 ms | 2 ms | 0.2 ms | 0.1 ms |
| 50,057 | 130,268 | 795 ms | 16 ms | 2 ms | 0.2 ms | 0.1 ms |

**Reading it:** interactive per-person tools (eligibility, balance, team conflicts) stay in the low milliseconds regardless of headcount, because they touch one employee or one team through indexed queries. Org-wide absence analytics grows linearly with the number of absence records; at enterprise scale it should run as a scheduled job against a warehouse, with the assistant reading the precomputed result. LLM latency (seconds) dominates every interactive request, not the tools.
