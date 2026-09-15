# Silver SCD Test — Bronze to Silver Conformance

**Stage**: 7 (Testing & Validation), produced by Test Manager
**Scope**: `contracts/models/insurance.model.yaml`, compiled from
`docs/templates/jarvis_silver_model_template.xlsx`
**Engine under test**: `emitters/silver_transform.py`

## What's actually being tested

Bronze is append-only — every landed batch adds rows, nothing is ever overwritten there. Silver
is where that gets conformed down to a usable shape: SCD1 tables collapse to one current row per
key, SCD2 tables keep history but expose exactly one current row per key. The precondition for
testing this at all is that bronze actually contains **multiple, genuinely different versions**
of the same business key — a single clean load proves nothing here, since conforming one version
of a record is trivial.

`harness/seed/generate_insurance_day2.py` creates that precondition: a second landing batch,
dated a day after the first, changing tracked attributes for a subset of existing keys. Bronze
now holds two versions of those records under the same business key, landed as separate batches.

## Test setup — verified before testing the transform

| Table | SCD type | Tracked attributes changed | Records changed | Bronze rows before → after day 2 |
|---|---|---|---|---|
| parties | scd1 | email, phone | 200 of 5,500 | 5,500 → 5,700 |
| addresses | scd1 | line1, city | 200 of 5,000 | 5,000 → 5,200 |
| products | scd1 | product_name, active_flag | 10 of 50 | 50 → 60 |
| customers | scd2 | risk_tier, lifecycle_stage | 300 of 4,900 | 4,900 → 5,200 |
| agents | scd2 | status | 50 of 460 | 460 → 510 |
| policies | scd2 | policy_status | 300 of 4,900 | 4,900 → 5,200 |

Confirmed via direct query before running the transform (not assumed from the generator's own
row counts): every "changed" business key has exactly 2 bronze rows, every unchanged key has 1 —
no duplication introduced by re-running the loader (see the idempotency fix below).

## SCD1 results — PASS

**Success criterion**: exactly one silver row per business key, values matching the *latest*
bronze version.

| Dimension | Silver rows | Distinct keys | Keys with >1 row (must be 0) |
|---|---|---|---|
| dim_party | 5,500 | 5,500 | 0 |
| dim_address | 5,000 | 5,000 | 0 |
| dim_product | 50 | 50 | 0 |

Spot-checked `PTY000011`: bronze has two rows (`davisryan@example.com` / day 1, then
`davisryan.updated@example.com` / day 2). Silver has exactly one row, with the **day-2** email —
confirming both "no duplication" and "latest wins," not just row count.

## SCD2 results — PASS, including the "no spurious version" edge case

**Success criterion**: possibly multiple silver rows per key, but exactly one with
`row_is_current = true`; a version is cut only when a *tracked* attribute's value actually
changes, not merely re-landed.

| Dimension | Silver rows | Current rows | Distinct keys | Keys with >1 current (must be 0) |
|---|---|---|---|---|
| dim_customer | 5,188 | 4,900 | 4,900 | 0 |
| dim_agent | 495 | 460 | 0 |
| dim_policy | 5,146 | 4,900 | 4,900 | 0 |

Note the silver row counts are **not** simply bronze-key-count + changed-count (e.g. dim_customer
is 5,188, not 4,900+300=5,200). This was investigated, not assumed correct: the day-2 generator
reassigns tracked attributes randomly without checking they differ from the original, so a small
fraction of "changes" coincidentally land on the same value combination. Per the SCD2 contract
in `contracts/control/control_model.yaml` ("merge_key match TRUE, merge_hash match TRUE -> NO
ACTION"), those should **not** produce a new version. Verified directly: of 300 dual-bronze-version
customers, exactly 12 collapsed to a single silver version (4,900 + 300 − 12 = 5,188, exact
match). This is the engine correctly implementing the spec's edge case, not a bug — confirmed by
checking the actual coincidental-match rate is consistent with the random generator's value
space (4 risk tiers × 5 lifecycle stages), not an arbitrary discrepancy.

Spot-checked `CUST000160`: two silver rows — `(former, high, row_is_current=false,
row_end_date=<day-2 timestamp>)` then `(active, very_high, row_is_current=true,
row_end_date=null)`. Textbook SCD2 shape.

## Facts — PASS (straight conform, no SCD)

| Fact | Rows | Matches bronze source |
|---|---|---|
| fact_policy_coverage | 5,000 | ✓ |
| fact_premium | 5,000 | ✓ |
| fact_payment | 5,000 | ✓ |
| fact_claim | 4,900 | ✓ |

## A real bug found and fixed while setting this test up

Re-running `bronze_loader.py` against a landing folder with both day-1 and day-2 files present
reprocessed **every** matching file every run, including already-loaded ones — `bronze.customers`
reached 14,700 rows (4,900 × 3) before this was caught. Root cause: no "already loaded" check.
Fixed in `emitters/bronze_loader.py`: before staging a batch, check `control.file_audit` for an
existing `action='loaded'` row with the same `file_name`; skip if found. Re-verified after the
fix with a full clean reload — exact expected row counts, confirmed by direct query (see table
above), not just absence of an error.

## Databricks parity — confirmed, not assumed

Loaded the same day-2 deltas into Databricks bronze (which only had day-1 data until this
point), then re-ran `emitters/silver_transform.py --target databricks`. Result matched the
DuckDB run **exactly**: `dim_customer` 5,188/4,900 current, `dim_agent` 495/460, `dim_policy`
5,146/4,900, all SCD1 dims and all facts identical row-for-row. Same `SqlConnection` abstraction,
same SQL, no dialect-specific branch needed for any of this — the sqlglot transpile approach
(ADR-001) holds for the silver layer too, not just bronze.

## What this run does not prove

- Multi-table SCD (a change split across two source tables landing in the same batch).
- Late-arriving fact rows against a not-yet-current dimension member (`late_arriving:
  inferred_member` in the model contract is declared but not yet exercised by a test).
