# P1 Test Run — Source → Bronze (`orders`)

**Stage**: 7 (Testing & Validation), produced by Test Manager
**Run ID**: `5742c426-46fc-461f-99cd-d00e9209abd0` (post-review-fixes run; superseded an earlier
run `d56010e4-...` made before Stage 8 fixes below — same result both times)
**Command**: `python harness/run_bronze.py --source orders`
**Result**: `files_seen=10 files_accepted=7 files_quarantined=3 rows_loaded=3359`

Raw output and query results captured below are from the actual run against
`harness/jarvis.duckdb`, not asserted.

## Stage 8 independent review — findings and fixes

A fresh subagent (no prior context on this repo) reviewed the implementation independently,
re-ran the loader itself rather than trusting this document, and reported (full text in the
session, condensed here):

- **Bug**: `size_deviation_pct` (contract value 40) was declared in `file_checks` but never
  evaluated — `design.md`'s own algorithm said it should be. **Fixed**: added the same
  trailing-median comparison used for row-count deviation.
- **Bug**: an exception mid-run would exit before the `run_registry` insert, silently violating
  CLAUDE.md rule 7 ("no silent runs") in the failure path specifically — success was always
  logged, failure wasn't. **Fixed**: `run()` now wraps the load in `try/finally`; a failed run
  writes `status='failed'` with the exception message, then re-raises. Verified with a deliberate
  fault injection (an unimplemented rule type mid-run) — confirmed a `run_registry` row with
  `status='failed'` and the correct `error_message` appears, not nothing.
- **Rule-1-adjacent**: `"bronze"`/`"control"` schema names were hardcoded string literals rather
  than read from `contracts/platform/<target>.yaml`. **Fixed**: both loaders now read
  `platform["storage"]["bronze"|"control"]`, and `run()` refuses any `--target` other than
  `duckdb` rather than silently assuming portability that doesn't exist yet.
- **More serious, NOT fixed, needs your call**: ADR-001 (bypassing `dlt`/`sqlglot` for direct
  DuckDB SQL) conflicts with CLAUDE.md rule 2, which — unlike rule 4 — has no documented
  human-override mechanism. See the "Independent review finding" section added to
  `evidence/decisions/ADR-001-p1-ingestion-approach.md`. This is the one item from P1 that
  genuinely needs a decision from you, not just a review.

Re-ran after fixes: identical result (`7 accepted / 3 quarantined / 3359 rows`), confirming the
fixes didn't change P1's actual behavior against this fixture set — `size_deviation_pct` simply
never was the binding constraint for any of the 10 files (row-count and schema-drift already
caught the two FQC failures).

## Success criteria (`P1-intent.md`, amended by `specs/P1/requirements.md`) — checked one by one

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | All 10 files processed, no crash | **PASS** | `files_seen=10`, run completed, single exit |
| 2 | Schema-drift file quarantined | **PASS** | `orders_20260909.csv` (7 cols vs expected 8): `file_audit.action='quarantined'`, `fqc_passed=false`, file present in `harness/quarantine/orders/` |
| 3 (amended) | Dip file quarantined at FQC | **PASS** | `orders_20260908.csv` (60 rows vs `min_rows: 100`): `fqc_passed=false`, quarantined, never staged (no `data_object_catalogue` row — it never became a tracked object) |
| 4 (amended) | Duplicate file fully quarantined at DQC, not partially loaded | **PASS** | `orders_20260910.csv`: `fqc_passed=true` (file-level checks fine), staged, `unique(order_id)` rule failed with `failed_row_count=25` at `severity=error` → whole batch rejected. `data_object_catalogue` row exists (`loader_status='quarantined'`) because it *was* staged, distinguishing it from the FQC rejections above. Zero rows from this file are in `bronze.orders`. |
| 5 | Lineage columns on every accepted row | **PASS** | Sampled rows carry `data_catalogue_id`, `source_record_id` (1-based, monotonic per file), `_run_id`, `_source_file`, `_record_hash` (distinct per row) |
| 6 | Exactly one `run_registry` row per invocation | **PASS** | One row, `run_id` matches CLI output |
| 7 | Independent reviewer | **PASS (with fixes required and applied)** | fresh subagent, no prior repo context, re-ran the loader itself rather than trusting this doc — see review section above and `evidence/decisions/P1-review.md` |
| 8 | Git rollback tag | pending — next, after this doc is finalized |

## Row-count reconciliation

Sum of the 7 accepted days' row counts from the original seed-generation output
(563+418+427+417+503+527+504) = **3359**, exactly matching `bronze.orders` row count and
`run_registry.rows_loaded`. No silent row loss or duplication among accepted files.

## R3 verified (bronze does not parse `order_date`)

Sample row: `order_date = '01/09/2026'` (string, matches source format exactly, not cast to a
DATE type). Standardisation to ISO-8601 remains silver's job per `sales.model.yaml`.

## R5 verified (freshness warnings expected, not a defect)

Every one of the 8 files that reached DQC evaluation (7 accepted + the day-10 duplicate file
before its rejection) has a `freshness` warning with `failed_row_count` equal to its full row
count — expected, since the seed data is dated 2026-09-01–10 and this run happened 2026-09-15,
`severity: warn` so it never blocked promotion by itself.

## What this run does *not* prove

- Databricks portability (P5, not attempted).
- Behavior on a second concurrent source (no orchestration/batch decomposition exists — see
  ADR-001).
- `regex` / `referential` quality rule types (not present in this contract, and the loader
  raises `NotImplementedError` rather than silently skipping them if they appear later).
