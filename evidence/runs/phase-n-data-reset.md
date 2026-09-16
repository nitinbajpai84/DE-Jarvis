# Evidence: Phase N -- full data reset (DuckDB + real Databricks) and a real bronze-loader bug found along the way

**Date:** 2026-09-17
**Goal:** per the user's request, clear all data currently in DuckDB and the real Databricks
workspace for both domains, then reload a fresh pair of datasets from the real landing files,
logging any issues found -- as a rehearsal for the two company logins built in Phase M+1
(multi-tenant auth) actually onboarding for real.

## What was wiped and reloaded

For each of (duckdb, databricks) x (insurance, asset_management):
- Dropped every table in `{domain}_bronze`, `{domain}_silver`, `{domain}_gold`.
- Cleared this domain's rows from `control.run_registry`, `file_audit`,
  `data_object_catalogue`, `dq_results`, `sdlc_run`, `incident_ticket`. `load_lineage` and
  `sdlc_stage_run` were deliberately left alone -- neither has a domain column, and both are
  only ever queried by `run_id` obtained from the tables already cleared (confirmed by grep
  across `webapp/` and `emitters/`, not assumed), so their old rows become permanently
  unreachable rather than needing a join-based delete.
- Reloaded from the real landing-zone files already on disk (`harness/landing/<source>/`) via
  the actual production loaders -- `bronze_loader.run`, `silver_transform.run`,
  `gold_transform.run` -- the same pipeline a real file drop goes through. No synthetic or
  invented data of any kind.

## A real bug, found by the real workspace -- not by local testing

The insurance reload on Databricks crashed partway through with a `RequestError: Retry request
would exceed Retry policy max retry duration of 900.0 seconds` -- a real transient warehouse
availability issue. Resuming the reload afterward should have been a non-event (the loader is
supposed to be idempotent, skipping already-loaded files) -- instead it surfaced a genuine data
integrity bug: `control.file_audit` showed `customers_1.csv` marked `action='loaded'` with
`row_count=4900`, but `insurance_bronze.customers` only held 420 rows total. The idempotency
check (`emitters/bronze_loader.py`, "skip if file_audit already says loaded") then made the
retry skip that file forever instead of reprocessing it -- silent, permanent data loss with no
built-in recovery path.

**Root cause:** `bronze_loader.run()` wrote the `file_audit` "loaded" row *before* the actual
`insert into {bronze}.{source_id} ... select ... from _staging` that moves the real data. Every
`con.execute()` against Databricks is its own network round trip with no surrounding
transaction, so a connection drop between those two statements leaves file_audit claiming
success for data that was never actually written. Against local DuckDB (in-process, no network)
this window is effectively zero and never bit anyone in this project before; against a real
warehouse over a real network, it did, on the very first large real workload this reload phase
produced.

**Fix** (`emitters/bronze_loader.py`): reordered so the real bronze insert happens first, and
`file_audit`/`data_object_catalogue` are written only after it succeeds. A crash before the
insert now leaves the file looking not-yet-loaded, so a retry correctly reprocesses it instead
of silently skipping it forever.

**Repair of the already-corrupted row:** deleted the bad `file_audit` and
`data_object_catalogue` rows for `customers_1.csv`, then re-ran `bronze_loader.run('customers',
'databricks', domain='insurance')` with the fixed code. Verified every one of insurance's 10
bronze tables against the known-good DuckDB reference counts afterward -- `customers` was the
only one affected (all 9 others matched exactly, including the 3 that showed as "skipped" in
the resumed run, which really had completed cleanly before the crash). Rebuilt silver/gold;
`dim_customer` now shows `rows: 5306, current_rows: 4900` on both platforms, identical.

## Final state, verified

| | DuckDB | Databricks |
|---|---|---|
| insurance bronze/silver/gold | 42,160 / 41,489 / 118 rows | identical, verified table-by-table |
| asset_management bronze/silver/gold | 48 / 48 / 6 rows | identical (first real load onto Databricks for this domain) |
| insurance test pack | 6 real failing cases (orphan FKs, same known issue from Phase J) -- 6 tickets raised | same 6 cases, same 6 tickets raised |
| asset_management test pack | 0 failing cases | 0 failing cases |

25/25 regression suite passed on both duckdb and Databricks, before and after the fix.

## What this proves, stated plainly

Not "the reload worked" in the trivial sense -- **a real crash against a real warehouse
surfaced a real ordering bug that no amount of local DuckDB testing across this entire project
would ever have caught**, because the bug's precondition (a connection drop between two
supposedly-adjacent writes) doesn't meaningfully exist in-process. This is the value of actually
running the destructive step against the real workspace rather than only simulating it.

## Known gaps, stated plainly

- The fix closes the specific window between the data insert and the file_audit write, but the
  three statements involved (bronze insert, file_audit insert, data_object_catalogue insert)
  still aren't wrapped in one atomic transaction -- a crash between the bronze insert and the
  file_audit insert now just means "reprocess this file," which is safe and correct, but a crash
  *between* file_audit and data_object_catalogue would leave those two slightly out of sync.
  Lower-severity (data_object_catalogue is a secondary catalogue, not consulted by the
  idempotency check), not fixed here.
- `load_lineage` and `sdlc_stage_run` rows from before the wipe still physically exist for both
  domains (orphaned, unreachable) rather than being deleted -- inert, not a correctness issue,
  documented rather than silently left unexplained.
