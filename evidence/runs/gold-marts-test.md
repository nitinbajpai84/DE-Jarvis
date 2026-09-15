# Gold Marts Test — Silver to Gold Semantic Layer

**Stage**: 7 (Testing & Validation), produced by Test Manager
**Scope**: `contracts/semantics/insurance.gold.yaml`, compiled from
`docs/templates/jarvis_gold_model_template.xlsx`
**Engine under test**: `emitters/gold_transform.py`

## What's being tested

Three marts, each a group-by aggregation from silver, joined to current-state dimensions.
`mart_loss_ratio_by_line` is the interesting case: `loss_ratio` depends on `claims_paid` (from
`fact_claim`) **and** `earned_premium` (from `fact_premium`) — two different fact tables at the
same grain, not a single-table rollup. The engine resolves this generically (aggregate each
fact to the mart's grain separately, `FULL OUTER JOIN` on the grain, then compute the ratio from
the joined additive metrics), not via a hand-hacked special case for this one metric.

## Row counts and reconciliation — PASS

| Mart | Rows | Real grain combos | NULL-line_of_business rows |
|---|---|---|---|
| mart_premium_by_product | 36 | 30 (5 lines × 6 transaction types) | 6 |
| mart_claims_by_status | 30 | 25 (5 lines × 5 claim statuses) | 5 |
| mart_loss_ratio_by_line | 7 | 5 (one per line of business) | 2 |

Totals reconciled directly against the source fact tables, not assumed correct from row counts
alone: `sum(written_premium)` across `mart_premium_by_product` = `sum(written_premium_amount)`
across `fact_premium` exactly (7,550,889.45 both sides); `sum(claims_paid)` across
`mart_claims_by_status` = `sum(paid_amount)` across `fact_claim` exactly (97,670,715.50 both
sides). No double-counting or silent row loss in the joins.

## The NULL-line_of_business rows — investigated, not ignored

Every mart has some rows with `line_of_business = null`. Traced to source rather than assumed
benign: exactly 100 rows in `fact_premium` and 100 in `fact_claim` reference `policy_id`s with
no matching `row_is_current=true` row in `dim_policy`. Those are precisely the 100 policies from
`policies_2.csv` — the duplicate-policy-number batch quarantined back in the original P1 bronze
test (`evidence/runs/P1-bronze-run.md`) and never promoted past bronze. The synthetic data
generator (`harness/seed/generate_insurance.py`) built premiums/payments/claims against the full
policy ID pool including that quarantined batch, so this orphaned-FK case was latent in the test
data from the start, now surfaced at the gold layer.

This is the exact scenario `contracts/models/insurance.model.yaml`'s `reject_orphan_fks: false`
anticipates ("false -> inferred member"). The current engine doesn't build an inferred-member
placeholder — an unresolved FK surfaces as a `NULL` grain value via the `LEFT JOIN`, which is a
defensible default but not what the contract technically promises. **Known gap, not silently
papered over**: `late_arriving: inferred_member` is declared in the silver model but not yet
implemented by either `silver_transform.py` or `gold_transform.py`.

## A genuine data-realism finding, not a bug

`loss_ratio` values came out at 1,675%–1,814% (e.g. auto: `claims_paid=18,157,886.81` /
`earned_premium=1,072,102.22` = 16.9). The arithmetic is correct — verified by hand against the
reconciled totals above — but real P&C loss ratios run 50–90%, not 1,600%+. Root cause: the
original synthetic data (`harness/seed/generate_insurance.py`) sized `claims.paid_amount`
(`random.uniform(200, 40000)` per claim) and `premiums.earned_premium_amount` (derived from
`random.uniform(50, 3000)` per premium transaction) independently, with no cross-reference to a
realistic premium-to-claim ratio. This is a real fixture-quality gap worth knowing about before
using these marts for anything resembling a realistic KPI demo — the gold *engine* is proven
correct; the *input data's* business realism is a separate, worthwhile follow-up.

## Databricks parity — confirmed

Re-ran `emitters/gold_transform.py --target databricks`. Row counts matched exactly (36/30/7).
Values matched to floating-point precision — e.g. `loss_ratio` for `auto`: duckdb
`16.936712256784624`, Databricks `16.93671224819598`, a ~1e-9 relative difference consistent
with `DOUBLE` representation noise between the two engines, not a computation discrepancy. Same
NULL-line_of_business rows present on both platforms (the 100 orphaned-FK premiums/claims are a
property of the data, not the platform). The cross-fact `FULL OUTER JOIN` pattern that
`mart_loss_ratio_by_line` needs works unmodified on Databricks — no dialect-specific SQL required
anywhere in the gold layer either.

## What this run does not prove

- `late_arriving: inferred_member` FK handling (currently NULLs, not placeholder members).
- Non-additive metrics with a chain longer than one hop (`average_claim_severity` was not
  exercised by this run's marts — only `loss_ratio`'s single-hop cross-fact case was).
- Dashboard tile rendering — the `Dashboards` tab compiled into the contract, but no dashboard
  page reads it yet.
