# Evidence: Phase J -- Step 04 per-layer test generation (3A/3B/3C)

**Date:** 2026-09-16
**Goal:** Agent 5's (Test Manager) real capability for Step 04 -- tests derived per layer from
each domain's own contracts, replacing the single hard-coded, insurance-only pytest suite that
`gather_validation_pack` (G3's prep tool) had been running regardless of which domain was
actually being validated.

## The real bug this started from

`gather_validation_pack` called `pytest tests/test_pipeline.py` unconditionally. That suite's
own `DOMAIN = "insurance"` constant (with an explicit comment: "a second domain..."). Confirmed
by reading it, not assumed: validating `asset_management` at G3 was checking whether
*insurance's* regression suite passed -- a question with no relationship to asset_management's
own correctness. Fixing this properly meant building a domain-scoped test capability, not
patching the symptom.

## Why this doesn't read `control.dq_results` for the report

That table has no entity/layer column (confirmed from its own DDL in
`emitters/control_plane.py`) -- a historical row can't be reliably attributed to "silver,
dim_policy" versus any other entity that happens to share a column name. Rather than force an
attribution the schema doesn't support, `emitters/test_pack.py` **re-runs** the exact
evaluation functions bronze/silver/gold already use --
`silver_transform.eval_entity_quality_rules` (schema-agnostic despite the parameter name --
it'll evaluate against a bronze schema exactly as well as a silver one) and
`gold_transform.eval_business_checks` -- against one fresh `run_id` generated per test-pack
run, then reads back only what that exact run_id just wrote. No duplicated rule logic, no
ambiguous history-mining.

## The genuinely new capability: real orphan-FK checking

Every fact contract already declares `foreign_keys` (dimension + join column) and a
`conformance.reject_orphan_fks` flag. Grepped across `emitters/silver_transform.py` and
`emitters/gold_transform.py`: **neither has ever enforced it.** This phase closes that gap
using exactly what each contract already declares -- the dimension's own `business_key` column
name, never assumed to match the fact's `on` column by convention.

## Two real bugs caught by actually running this against real data, not by reasoning about it

**1. A genuine, previously-invisible data problem, found and verified by hand.** Running the
new orphan-FK check against real insurance silver data surfaced:
```
fact_policy_coverage.policy_id -> dim_policy.policy_id: 100 orphan rows
fact_premium.policy_id        -> dim_policy.policy_id: 100 orphan rows
fact_payment.policy_id        -> dim_policy.policy_id: 100 orphan rows
fact_payment.customer_id      -> dim_customer.customer_id: 88 orphan rows
fact_claim.policy_id          -> dim_policy.policy_id: 100 orphan rows
fact_claim.customer_id        -> dim_customer.customer_id: 87 orphan rows
```
Verified by hand, not trusted on the check's own say-so: `dim_policy` has 4,900 distinct
`policy_id` values against an expected 5,000; the specific orphan IDs the check named (e.g.
`POL000032`) genuinely return zero rows anywhere in `dim_policy`, confirmed with a direct
count query. Root cause, traced to this project's own P1 evidence doc: `policies_2.csv` (100
rows, 30 sharing a duplicate `policy_number`) was deliberately quarantined at bronze as part of
the original seed design -- so 100 policies were never conformed into `dim_policy`, while the
claims/premiums/payments/coverage files that reference them were never quarantined. A real,
documented, deliberate scenario from this project's own history that **nothing had ever
actually checked for** until this phase. This is exactly what Step 04 is supposed to catch.

**2. A bug in this phase's own first draft, caught before it shipped.** The bronze layer's
initial re-evaluation ran every declared `quality_rules` entry -- including `unique` -- against
the full accumulated bronze table. That produced 6 "failures" (`unique(address_id)`,
`unique(customer_id)`, etc.), which looked like real problems until checked against
`bronze_loader.py`'s own implementation: its `_eval_quality_rules` runs `unique` against a
per-BATCH **staging table** (`{bronze}._staging_{source_id}`), never the accumulated table.
Bronze is append-only across multiple daily loads by design, so the same natural key
legitimately repeats across days -- re-testing `unique` at the whole-table scope was reporting
expected, by-design repetition as a failure. Fixed by excluding `unique` from the bronze
re-evaluation (kept for silver, where SCD1 dimensions really are rebuilt from scratch and
uniqueness is a real, meaningful property); `not_null`/`accepted_values`/`range`/`regex` stay
in at bronze since those are per-value checks, unaffected by accumulation. After the fix,
bronze went from 34/40 passing to a clean 30/30 -- the silver orphan-FK findings, which are
real, were unaffected.

## What was built

- **`emitters/test_pack.py`** -- `generate_test_pack(domain, target)`: 3A (row-landed +
  quality_rules per source, `unique` excluded for the reason above), 3B (row count +
  quality_rules per entity + the new orphan-FK check per fact), 3C (mart row count +
  business_rules). Returns cases grouped by layer with a pass/fail summary.
- **`run_test_pack`** -- a new ungated Agent 5 tool, callable on its own.
- **`gather_validation_pack` rewritten**: now keeps the pytest suite as `platform_regression`
  (explicitly labeled engine-sanity-only, not domain-specific) and adds `test_pack` as the real
  per-domain signal.
- **`accept_validation` (G3) rewritten** to block on `test_pack.total_failed`, not the old
  domain-blind `tests.passed` -- G3 for a domain now genuinely reflects that domain's own data.
- **Step 04 screen**: a "Run test pack" action showing live per-layer results -- pass/fail per
  case, with the real detail (row counts, orphan counts, which source resolved what).

## Verification

Real asset_management (no declared foreign_keys -- nothing to check there, correctly):
```
summary: bronze 7/7, silver 3/3, gold 2/2 -- total_failed: 0
```
Real insurance, post-fix:
```
summary: bronze 30/30, silver 10/16 (6 real orphan-FK failures), gold 3/3 -- total_failed: 6
```
Full G3 gate flow, both domains, through the real tools:
```
gather_validation_pack + accept_validation('asset_management') -> ok: True
gather_validation_pack + accept_validation('insurance')         -> ok: False, refused: True
```
G3 for insurance now correctly refuses on a real, previously-hidden problem it could never
have caught before this phase.

**Driven live through the actual browser**, not just the module level: opened Step 04 for
insurance, clicked Run test pack, watched all 30 bronze cases render as real passes and all 6
silver orphan-FK failures render with their exact row counts and column names; switched to
asset_management, ran again, watched all 12 cases pass clean.

## Regression

25/25 on duckdb and Databricks (`tests/test_pipeline.py` itself untouched -- it's still what
`platform_regression` runs; only what G3 *blocks on* changed). The test pack itself verified
against real Databricks data too (a real warehouse, real per-query network latency -- this ran
noticeably slower than against duckdb, which is itself informative: the per-entity/per-source
query pattern here is not free against a live warehouse, worth knowing before running it on a
much larger domain).

## Known gaps, stated plainly

- **The genuine orphan-FK finding in insurance is not fixed by this phase**, and shouldn't be
  -- Step 04's job is to catch it, not silently repair a data problem a human hasn't seen yet.
  It's now visible, correctly blocking G3, and it's the customer's/team's decision what to do
  about the quarantined `policies_2.csv` batch.
- **Agent 6 (Visualisation) still has zero capability.** Out of scope for this phase -- the
  user's request was specifically per-layer test generation; visualisation is its own,
  differently-shaped piece of work.
- **No SIT (system integration test) against the signed intent yet.** This phase proves each
  layer is internally consistent with its own contract; it doesn't yet check the built pipeline
  against the business outcome captured in Step 02's intent record (Phase H).
- **Only `not_null`/`unique`/`accepted_values`/`range`/`regex` are re-evaluated** -- the same
  set `_IMPLEMENTED_ATTR_CHECKS` in `intake_compiler.py` already limits itself to;
  `natural_language` rules remain Open Questions for a human, exactly as everywhere else in
  this platform.
