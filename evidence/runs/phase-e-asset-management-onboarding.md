# Evidence: Phase E -- onboarding `asset_management` as domain #2

**Date:** 2026-09-16
**Goal:** prove the platform is actually generic (not "generic for one domain we designed it
around") and prove the human-in-the-loop UX (Phase D) end to end on a real onboarding, in one
real run -- not a scripted demo.

## What was built

- `docs/templates/_awm_intake_workbook.xlsx` -- a real intake workbook for a second domain,
  `asset_management`: 3 sources (`awm_portfolios`, `awm_instruments`, `awm_positions`), 2 SCD1
  dims (`dim_awm_portfolio`, `dim_awm_instrument`), 1 fact (`fact_awm_position`, deliberately
  **not** SCD -- grain `portfolio_code + as_of_date + instrument_code`), 1 gold mart
  (`mart_awm_portfolio_value`, built directly off the fact rather than via a join, to sidestep
  a known translator gap rather than paper over it), 8 DQ rules, and 1 business check
  (`new_or_missing_dimension`).
- Synthetic landing data at `harness/landing/awm_portfolios/`, `awm_instruments/`,
  `awm_positions/` -- 6 portfolios, 15 instruments, 27 positions, seed=42.
- Nothing hand-written past the workbook: contracts, dims, fact, and gold mart were all
  produced by the same compiler/emitter chain every other domain uses.

## Verification: the real gated agent run, through the real UI

Uploaded `_awm_intake_workbook.xlsx` in the Control Room and clicked **Run via Agent** -- the
same PM-driven `deepagents` team from Phase C, same Freeze gate from Phase C/D, pointed at a
domain the platform had never seen. Confirmed via the live "Freeze gate" panel and the
database, not just the UI's own claim:

```
run 776cfbdd -- asset_management/default -- started 7:33:35 PM
  specify: completed  -- system: 3 sources, 0 open questions, 0 warnings
  freeze:  completed  -- human+system: wrote 8 contract files for domain='asset_management'
```

`contracts/sources/asset_management/{awm_portfolios,awm_instruments,awm_positions}.source.yaml`,
`contracts/models/asset_management.model.yaml`, and `contracts/semantics/asset_management.gold.yaml`
did not exist before this run; all five exist now, written only after the human Approve click,
exactly matching the Phase C/D gate design. No hand-authored contract for this domain at any
point.

## Verification: real bronze -> silver -> gold execution

Ran the real pipeline (`harness/bronze_loader.py` equivalent path, `emitters/silver_transform.py`,
`emitters/gold_transform.py`) against `asset_management` on duckdb. Row counts from
`GET /api/pipeline/flow?target=duckdb&domain=asset_management`, cross-checked live in the
Control Room UI after wiring the domain selector (see below):

| layer  | tables | columns | rows |
|--------|-------:|--------:|-----:|
| bronze |      3 |      12 |   48 |
| silver |      3 |      16 |   48 |
| gold   |      1 |       3 |    6 |

Bronze/silver row counts match (48 = 27 positions + 15 instruments + 6 portfolios landed and
loaded 1:1, no rejects). Gold's 6 rows are `mart_awm_portfolio_value` -- one row per portfolio.

## Verification: DQ rules and the business check actually ran and passed

Queried `control.dq_results` directly (not the UI) for `domain='asset_management'`:

```
new_or_missing_dimension  portfolio_code    error    passed=True  fail=0
range                     market_value      warning  passed=True  fail=0
regex                     instrument_code   warning  passed=True  fail=0
not_null                  portfolio_code    error    passed=True  fail=0
unique                    portfolio_code    error    passed=True  fail=0
accepted_values           portfolio_type    error    passed=True  fail=0
```

6 rows logged, all passed, including `new_or_missing_dimension` -- the Phase B gold
business-rule check ([phase-b-dq-business-rules.md](phase-b-dq-business-rules.md)) exercised
for the first time on a domain other than the one it was built against.

## Bug found and fixed while proving this: the domain selector never worked

Verifying gold-layer row counts for `asset_management` in the Control Room UI (not just via
`curl`) surfaced a real, previously-undetected gap: `GET /api/pipeline/flow` and
`GET /api/alerts` had accepted a `domain` parameter in `pipeline.py` since Phase 1, but their
FastAPI route signatures in `webapp/backend/main.py` never declared it -- FastAPI silently
drops a query parameter a route doesn't declare, no error, so `?domain=asset_management` was
being ignored at the API layer the entire time. Domain filtering had never actually been
reachable from outside a raw Python call.

Fixed by:
- Adding `domain: str = pipeline.DEFAULT_DOMAIN` to both route signatures.
- Adding `GET /api/domains`, which lists every domain with compiled contracts on disk (so a
  domain onboarded through the Freeze gate appears with zero code change).
- Adding a `<select id="domain">` to the frontend header, next to the existing target selector,
  wired to both `refreshFlow()` and `refreshAlerts()`.

Verified live in the browser (`http://127.0.0.1:8010/`, current uvicorn process, no `--reload`
but restarted after these edits): switching the domain dropdown from `insurance` to
`asset_management` correctly repaints bronze/silver/gold from 10/10/4 tables (insurance) to
3/3/1 tables (asset_management) with the exact row counts above, no console errors, no stale
data. This is the same class of bug as the gate-bypass investigation in Phase D -- something
that looked done because the backend function existed, but wasn't actually reachable end to
end until someone drove it through the real UI.

## Regression

Full 25-test suite, both platforms, after all Phase E changes:

```
pytest tests/ --target duckdb      -> 25 passed
pytest tests/ --target databricks  -> 25 passed
```

## What this proves, plainly

- **Genericity (Phase 1's claim, now exercised for real):** the same emitters, the same
  compiler, the same control-plane schema pattern (`{domain}_bronze/silver/gold` +
  shared `control`) handled a domain with a materially different shape -- non-SCD fact,
  business-rule check, gold mart built without a join -- without any code change.
- **The human-in-the-loop UX (Phase D's claim, now exercised on a real onboarding instead of a
  scripted demo):** a real unfamiliar domain went through discover -> specify -> the Freeze gate
  -> a human Approve click in the browser -> real contract files on disk -> real bronze/silver/
  gold execution -> real DQ results, with nothing hand-written and nothing silently skipped.
- **Verification discipline paid off again:** the domain-selector gap would not have been found
  by re-reading code or by trusting the backend function's existence -- only by actually trying
  to see the result in the UI, the same lesson from Phase D's gate-bypass investigation.

## Known gaps

- Multi-agent delegation (PM -> BA/architect -> DE-fork -> Test Manager) still hasn't been
  proven live through this UI -- this run, like every real run before it, was the PM calling
  the intake tools directly. Still open from Phase D.
- `docs/templates/_awm_intake_workbook.xlsx` keeps the leading-underscore naming used for the
  original insurance template; unlike that one this is a real, kept Phase E deliverable, not a
  scratch file -- worth a plain rename if this becomes a recurring onboarding pattern, not done
  here since it wasn't asked for.
