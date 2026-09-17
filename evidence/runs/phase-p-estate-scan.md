# Phase P — Stage 01 estate scan and estate report

Date: 2026-09-17

## What was built

- `emitters/estate.py` runs a tiered scan of everything a domain can see. It covers the domain's own schemas plus schemas no domain owns, and never touches `control` or other tenants.
  - **Tier 1**: census of every schema, table and column, with row counts.
  - **Tier 2**: null % and approximate distinct counts for a shortlist ranked by intent vocabulary, then size.
  - **Tier 3**: foreign keys with integrity (intact / orphans / weak), plus mirror detection. Mirror verdicts compare business columns with EXCEPT ALL, excluding build metadata.
  - **Tier 4**: Gemini describes each shortlisted table. It is given the measured facts and told not to restate them.
- `build_report` produces:
  - coverage and the entity map
  - relationships
  - the sensitivity register
  - quality hotspots, with expected nulls listed separately
  - mirrors
  - readiness per intent
  - open questions, ranked by severity
- API endpoints: `POST /api/estate/scan` runs on a background thread, one scan per domain and target. `GET /api/estate/scans` and `GET /api/estate/report` read the results. All three check the caller's domain.
- Control Room: the Discovery screen has Estate, Findings and Connect-a-source tabs, and polls while a scan runs.

## Databricks performance fix

The first version issued one INSERT per table and per column, and one UPDATE per profiled column. That is invisible on DuckDB, but every statement is a network round trip on Databricks. A real scan there had censused 7 tables after about 20 minutes when it was stopped.

Each tier now computes in memory. Results go out through `SqlConnection.executemany` as multi-row INSERTs, the batcher already built for bronze loading. The ordering changed so this works:
- Mirror detection runs before the census write, because it only reads source tables.
- Tier 4 enrichment is written as a delete plus one batched re-insert of the scan's table rows.

A scan row left `running` by a dead process is now closed as `failed`, with note "interrupted: …", the next time scans are listed. The Databricks scan orphaned by the stopped run was closed this way.

## Verified

| Check | DuckDB | Databricks |
|---|---|---|
| Full 4-tier scan | 70 s (Gemini dominates; tiers 1–3 take 1.3 s) | **251 s**, all 48 tables (was 7 tables in ~20 min) |
| Schemas / tables / columns / rows | 6 / 48 / 546 / 167,534 | 6 / 48 / 546 / 167,534 |
| Relationships intact / orphans / weak | 12 / 16 / 0 | 12 / 16 / 0 |
| Mirrors identical / diverged | 24 / 0 | 24 / 0 |
| Sensitive columns | 16 | 16 |
| Tables enriched by Tier 4 | 40 / 40 | 40 / 40 |
| Open questions | 14 | 14 |

Both engines produce the same report for the same estate. This DuckDB report also matches the one from before the refactor.

Other checks through the API on a local server:
- **Double-click guard:** the second POST was refused with the running scan's id.
- **Scan list:** the completed scan shows up as expected.

`tests/test_estate.py` adds 24 tests:
- **Heuristics:** key stems, entity naming, ownership that is not a substring match, build metadata (`created_at` and `updated_at` are deliberately kept), expected nulls, sensitivity, and tenancy scope.
- **End to end on a synthetic estate with planted answers:**
  - The other tenant's schema is excluded.
  - An intact FK is found.
  - An orphaned FK at 95% resolution is found and never reversed.
  - An identical mirror is found.
  - A diverged mirror is found, where one email differs. `silver_loaded_at` differs on every row and alone must not count.
  - The sensitive column is not double-counted from its legacy copy.
  - `deleted_at` is reported as an expected null, not a hotspot.
- **Batching guard:** adding 100 columns must not change the number of statements issued. This was checked by forcing `executemany` back to row-by-row: the test then fails, 271 vs 71 statements.
- **Abandoned scan:** a `running` row with no live process is closed as failed.

Regression: `pytest tests/ -q --target duckdb` → 49 passed; `--target databricks` → 49 passed.

## Honest limits

- Row counts use `count(*)` per table. That suits this deployment's scale. A 40,000-table estate should read the catalogue's own statistics instead.
- Tier 3 only matches columns with the same name within one schema, and only id-like columns.
- Tier 4's descriptions are suggestions attached to evidence. They are never written into a contract.
- The abandoned-scan cleanup assumes the web process is the only thing running scans for a (domain, target). A CLI scan running at the same moment against the same target would be closed wrongly.
