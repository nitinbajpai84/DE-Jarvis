# Evidence: Phase B -- silver DQ + gold business-rule checks

**Date:** 2026-09-16
**Goal:** close the gap Phase A's own docstring flagged -- Jarvis had no silver-layer DQ
enforcement and no gold-layer business-rule checks at all, even though the reference AWM
slides named three specific gold checks (Variance Check, Dimension Mapping Inconsistency
Check, New/Missing Dimension Check) as a headline governance feature.

## What was built

- `emitters/intake_compiler.py`: `_entity_quality_rules()` captures `dq_rule` rows with
  `level=attribute, layer=silver` into each silver entity's new `quality_rules` field (same
  rule vocabulary as bronze: not_null/unique/in_list/range/regex). `natural_language` rules at
  this layer become model-level `open_questions`, same discipline as bronze/attribute ones.
  `_gold_contract()` now carries full executable fields (`check_type`/`column`/`params`/
  `severity`) on every business_rules entry instead of only a descriptive `statement` -- the
  three implemented check types are enforced; the other four in the schema's enum
  (`calculated_column_accuracy`, `trend_anomaly`, `reconciliation`, `custom_sql`) are recorded
  with a printed warning, not silently claimed as enforced.
- `emitters/silver_transform.py`: `eval_entity_quality_rules()` -- runs after each dimension/
  fact build, logs to `control.dq_results` with the same run_id as that entity's `run_registry`
  row. No quarantine concept at this layer (a bad row can't be un-conformed after the fact);
  this evaluates and logs, it doesn't block.
- `emitters/gold_transform.py`: `eval_business_checks()` -- runs after each mart build, same
  `dq_results` logging pattern.
  - `new_or_missing_dimension`: every value of the named column in the mart must exist in the
    referenced silver dimension's current members.
  - `dimension_mapping_inconsistency`: every value of the named column must be in the
    `allowed` list.
  - `variance_vs_history`: auto-detects a date/timestamp-typed grain column, partitions by the
    rest of the grain, and flags period-over-period changes exceeding `max_pct_change`. This is
    a **deliberate simplification** of `lookback_days` (immediately-previous period, not a
    rolling N-day average) -- said so in the logged `observed_value` text, not hidden.

## Verification (real execution, not inspection)

Extended the Phase A throwaway `verify_intake` domain: added a silver-layer `in_list` rule on
`dim_vi_customer.risk_tier`, and two gold business checks on `mart_vi_customer_by_tier` --
`new_or_missing_dimension` (expected to pass, since the mart is derived straight from the
dimension) and `dimension_mapping_inconsistency` with `allowed=low,medium` (**deliberately
excluding `high`**, to prove the check actually fires, not just runs clean by accident).

Logged `control.dq_results` after a real bronze -> silver -> gold run:

```
accepted_values          risk_tier   warning  True   0   0   -- silver DQC
new_or_missing_dimension risk_tier   warning  True   0   0   -- gold BR: every tier exists in the dim
dimension_mapping_inconsistency risk_tier warning False 1  1  -- gold BR: caught the 1 'high'-tier row
```

`variance_vs_history` needs a date-grain mart the workbook test didn't have, so it was unit-
tested directly: a synthetic `gold.mart_test` with two portfolios, one with an 80% month-over-
month jump (over the 25% threshold) and one with a 4% jump (under it) --

```
variance_vs_history  False  "1 period-over-period changes (by 'month') exceed 25% (...)"  1
```

Exactly 1 violation, matching the deliberately-planted one, not the compliant one.

**Regression:** full 25-test suite on both duckdb and Databricks after these changes -- 25/25
on both. Insurance's existing hand-authored `business_rules` (all `statement`/`applies_to`
only, no `check_type`) are correctly skipped by the new evaluator rather than mis-evaluated --
verified by checking the short-circuit logic, not just by absence of errors.

## Known gaps (not done, not silently claimed)

- `calculated_column_accuracy`, `trend_anomaly`, `reconciliation`, `custom_sql` business checks
  -- schema models them, `gold_transform.py` does not execute them yet.
- `variance_vs_history` compares to the immediately preceding period only, not a genuine
  `lookback_days`-wide rolling average.
- Silver/gold `natural_language` rules still only ever become Open Questions -- no agent yet
  drafts a concrete check from them. That's Phase C.
