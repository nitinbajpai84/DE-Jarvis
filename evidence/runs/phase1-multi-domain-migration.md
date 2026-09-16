# Evidence: Phase 1 -- multi-domain / multi-client plumbing

**Date:** 2026-09-16
**Goal:** make the platform generic across domains (and clients) so a second domain can be
onboarded by uploading a workbook, with zero manual console steps -- without changing any
behaviour for the existing insurance domain.

## Design decisions (agreed before execution)

- **Shared catalog, domain-prefixed schemas.** One `jarvis` catalog per platform; schemas
  named `{domain}_bronze` / `{domain}_silver` / `{domain}_gold`. Chosen over catalog-per-domain
  specifically because Jarvis already auto-creates *schemas* (`CREATE SCHEMA IF NOT EXISTS`)
  but has never created *catalogs* -- so a shared catalog means onboarding a new domain needs
  no manual Databricks console step, which catalog-per-domain would have required forever.
  (A `jarvis-awm` catalog had been hand-created during this discussion; it is deliberately
  left unused, not deleted.)
- **One shared `control` schema** across every domain, with `domain`/`client` columns on its
  tables, rather than a control plane per domain -- keeps the Control Room a single unified
  view with no cross-schema joins. This mirrors the "Cross-domain Data Governance" band in the
  AWM reference architecture: governance/lineage/DQ is one layer spanning the domain zones.
- **One Slack channel**, messages tagged `[domain]` in the header, rather than a channel per
  domain (no new Slack app setup needed to test a new domain).

## What changed

**Contracts**
- `contracts/sources/*.source.yaml` -> `contracts/sources/<domain>/*.source.yaml` (regenerated
  by the compiler, old flat files removed via `git rm` -- recoverable from history).
- All three contract types now carry `domain` and `client`.
- `contracts/platform/{duckdb,databricks}.yaml`: `storage` changed from fixed schema names to
  `schema_pattern: "{domain}_{layer}"` + a still-fixed shared `control`.

**Excel templates** (the input surface, v2)
- Source catalogue `Sources` tab: added `client` (`domain` already existed but was previously
  captured into the contract and then ignored downstream).
- Silver model `Standardisation` tab: added `client`.
- Gold model: **new** `Standardisation` tab (`domain`, `client`) -- the gold workbook is now
  self-describing like the other two, replacing `gold_model_compiler.py`'s `--domain` flag
  (and the hardcoded `["--domain", "insurance"]` the Control Room's upload flow was passing,
  which would have forced every uploaded gold workbook to compile as insurance).

**Code**
- New `emitters/sql_dialect.py::resolve_schema(platform, domain, layer)` -- the single place
  that maps (domain, layer) to a schema name.
- `control_plane.py`: `domain`/`client` columns added to `run_registry`, `file_audit`,
  `data_object_catalogue`, `dq_results`; the one-off `columns_processed` migration generalised
  into a `_MIGRATIONS` table-driven check-then-ALTER loop with backfill (still avoiding
  `ADD COLUMN IF NOT EXISTS`, which transpiles clean but is invalid on Databricks).
  `compile_source_registration` no longer hardcodes `bronze.<source>` as bronze_table_name.
- `bronze_loader.py`: contract lookup now searches `contracts/sources/*/`, erroring on an
  ambiguous source_id rather than guessing; domain/client read from the contract itself (not a
  new CLI arg) and written on every control-plane insert.
- `silver_transform.py` / `gold_transform.py`: schema resolution via `resolve_schema`, domain/
  client on every `log_run`.
- `ops_monitor.py`: alert headers now carry a `[domain]` tag.
- `webapp/backend/pipeline.py`: domain-aware (defaults to `insurance` until the Control Room
  gets a domain selector in Phase 4), and `run_registry` queries now filter by domain -- the
  control plane is shared, so an unfiltered query would mix domains once a second one exists.
- `harness/dashboard.py`, `harness/insights_report.py`: fixed to use `resolve_schema` (they
  read the now-removed fixed keys and would have crashed).

## Data migration

Bronze/silver/gold tables were **copied** (`CREATE TABLE <new> AS SELECT * FROM <old>`) into
the domain-scoped schemas, row counts verified per table. The old `bronze`/`silver`/`gold`
schemas were deliberately **left in place, not dropped** -- the migration is additive and
reversible; cleaning them up is a separate, explicit decision. `control` was never renamed
(it stays shared), so it only needed the ALTER + backfill.

| platform | tables copied | row-count mismatches |
|---|---|---|
| duckdb | 24 | 0 |
| databricks | 24 | 0 |

Backfill verified: `domain='insurance'`, `client='default'`, **0 nulls** across all four
control tables on both platforms (duckdb 64/39/30/102 rows; databricks 71/41/29/99).

## Verification

Full pipeline re-run through the new code path on **both** platforms (not just a read-side
check -- the write path is what the refactor touched):

- bronze: all 10 sources, idempotency correctly skipped already-loaded files, the deliberately
  bad fixtures still quarantined.
- silver: dim_party 5500 / dim_address 5000 / dim_product 50 / dim_customer 5306 (4900 current)
  / dim_agent 495 (460 current) / dim_policy 5238 (4900 current) + 4 facts.
- gold: 36 / 30 / 7 / 45.

Identical to the pre-refactor numbers on both platforms.

```
pytest tests/test_pipeline.py --target=duckdb      25 passed
pytest tests/test_pipeline.py --target=databricks  25 passed
```

Control Room API confirmed serving correctly on both targets after the change
(`{"target":"duckdb","domain":"insurance",...}` / `{"target":"databricks","domain":"insurance",...}`,
both reporting 42,160 bronze / 41,489 silver / 118 gold rows).

## Known follow-ups (not done in Phase 1)

- `harness/daily_digest.py` aggregates run_registry across all domains. Correct today (one
  domain) and arguably the right default for an ops digest, but worth a decision once a second
  domain lands.
- The old `bronze`/`silver`/`gold` schemas still hold a frozen copy of the pre-migration data.
- Control Room still has no domain selector (Phase 4).
- `.claude/agents/de-bronze.md` and some specs/evidence docs still describe the flat
  `contracts/sources/<source>.source.yaml` layout in prose.
