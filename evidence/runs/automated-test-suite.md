# Evidence: Automated regression suite (tests/test_pipeline.py)

**Date:** 2026-09-16
**Trigger:** User flagged that "the test cases and all are not been yet done" -- all
verification up to this point had been ad-hoc manual querying, written up in
`evidence/runs/*.md` by hand. This replaces that with a re-runnable, assertion-based
suite anyone can execute with one command.

## What was built

- `tests/conftest.py` -- `--target` CLI option (`duckdb` default, or `databricks`),
  session-scoped `target` fixture.
- `tests/test_pipeline.py` -- 25 tests across 8 classes, run against the **real**
  database state (integration-style, not mocked), covering:
  - `TestBronzeFQC` -- schema-drift file and row-count-dip file are quarantined at
    the file-check stage (using the exact fixtures `harness/seed/generate_insurance.py`
    builds for this purpose).
  - `TestBronzeDQC` -- an error-severity DQC violation (`unique`, `accepted_values`)
    quarantines the **whole batch**, not just the offending rows (specs/P1/requirements.md
    Q1/Q2).
  - `TestBronzeIdempotency` -- no file is ever marked `loaded` more than once.
  - `TestBronzeLineage` -- all 6 lineage columns present on every bronze table.
  - `TestSCD1` -- exactly one row per business key (dim_party, dim_address, dim_product).
  - `TestSCD2` -- exactly one `row_is_current=true` row per key, and every key has one
    (dim_customer, dim_agent, dim_policy).
  - `TestGoldReconciliation` -- mart sums reconcile against fact sums (no join fan-out
    or row loss); loss_ratio's own arithmetic is independently recomputed and checked;
    loss_ratio is asserted to sit in a plausible P&C range (0-150%) -- this codifies the
    1,700%+ data-realism bug found and fixed manually earlier in the project, so a future
    regression fails loudly instead of shipping.
  - `TestControlPlaneCoverage` -- every phase (bronze/silver/gold) has written to
    `control.run_registry` (CLAUDE.md rule 7).

## Run 1 -- duckdb

```
pytest tests/test_pipeline.py -v --target=duckdb
25 passed in 1.44s
```

All green on the first run.

## Run 2 -- databricks (first attempt) -- caught a real gap

```
pytest tests/test_pipeline.py -v --target=databricks
```

9 tests failed, all `TABLE_OR_VIEW_NOT_FOUND` for `silver.*` tables, cascading into
gold reconciliation failures and a missing `run_registry` entries for `phase='silver'`
and `phase='gold'`.

**Root cause (verified, not assumed):** queried `jarvis.information_schema.tables` directly
and confirmed silver and gold had never actually been built against the live Databricks
catalog -- only against duckdb. Bronze was fully loaded there (10/10 tables); silver and
gold were not. This was a genuine gap the ad-hoc verification process had missed, exactly
the kind of thing this suite exists to catch.

**Fix:** ran the transforms for real against the live workspace:
```
python -m emitters.silver_transform --target=databricks
python -m emitters.gold_transform --target=databricks
```
Silver: dim_party(5500), dim_address(5000), dim_product(50), dim_customer(5188/4900 current),
dim_agent(495/460 current), dim_policy(5146/4900 current), plus 4 facts (~20k rows total).
Gold: mart_premium_by_product(36), mart_claims_by_status(30), mart_loss_ratio_by_line(7),
mart_monthly_performance(45).

## Run 3 -- databricks (after fix)

```
pytest tests/test_pipeline.py -v --target=databricks
25 passed in 23.49s
```

Both platforms now pass the identical assertion suite against live data -- this is the
cross-platform parity check the project's portability discipline (`emitters/sql_dialect.py`)
exists to make possible: one SQL string, one set of tests, two real backends.
