# P1 Review — Source → Bronze (`orders`)

**Stage**: 8 (Review)
**Reviewer**: independent Claude subagent, spawned with no prior context on this repository —
given only CLAUDE.md, the relevant contracts, `specs/P1/requirements.md` and `design.md`, and
the diff. Did not read `evidence/runs/P1-bronze-run.md` (the implementer's own report) until
after forming its own view, and re-ran the loader itself rather than trusting any claim made by
the implementer. This is the author≠reviewer separation CLAUDE.md rule 5 requires, applied
literally rather than performed by the same session under a different hat.

## Verdict

**Approve with fixes needed.** Independently reproduced the exact same run result
(`files_seen=10, accepted=7, quarantined=3, rows_loaded=3359`) before reading any of the
implementer's documentation.

## Findings

1. **Design vs. contract, mostly holds.** Q1/Q2's readings in `requirements.md` are correctly
   grounded in `source.schema.yaml`'s own severity comment.
2. **ADR-001 conflicts with CLAUDE.md rule 2** (non-negotiable, no override mechanism unlike
   rule 4's Open Questions) by bypassing `dlt`/`sqlglot` for hand-written DuckDB SQL. Disclosed
   in the ADR, but disclosure isn't the same as authority to waive a hard rule. **Not resolved —
   escalated to the human for explicit decision**, see `evidence/decisions/ADR-001-...md`.
3. **Bug**: `size_deviation_pct` declared in the contract, never evaluated in code. Fixed
   same-day, re-verified.
4. **Bug**: a mid-run exception would exit before the `run_registry` insert — the one failure
   mode CLAUDE.md rule 7 exists to prevent was itself unlogged. Fixed same-day with
   `try/finally`, verified with a deliberate fault-injection test.
5. **Rule-1-adjacent**: `"bronze"`/`"control"` schema names were literals, not read from
   `contracts/platform/<target>.yaml`. Fixed same-day.
6. **Documentation gap**: the `unique` rule's `failed_row_count` semantic (rows in excess of
   first occurrence, not total rows sharing a duplicated key) was correct but undocumented.
   Fixed — now has an inline comment explaining the choice.

Full reviewer output is in the session transcript that produced this phase; this file is the
durable record per CLAUDE.md's evidence requirement.

## Outstanding for human decision

Item 2 above. Everything else was fixed and independently re-verifiable by re-running
`python harness/run_bronze.py --source orders` against the current code.
