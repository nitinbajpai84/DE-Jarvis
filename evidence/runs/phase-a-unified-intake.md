# Evidence: Phase A -- unified intake workbook, real engine underneath

**Date:** 2026-09-16
**Goal:** adopt the "Data Platform Factory" reference kit's unified intake workbook + JSON-
Schema-validated spec as Jarvis's new front end, while keeping Jarvis's own bronze/silver/gold
engine (the one actually proven against real Databricks) as the execution backend.

## What was built

- `contracts/schema/intake_spec.schema.json` -- the canonical spec schema, ported unchanged.
- `emitters/intake_template_builder.py` -- generates `docs/templates/jarvis_intake_template.xlsx`,
  one workbook (12 sheets + Project) replacing the three it retires.
- `emitters/intake_compiler.py` -- two-stage: `compile_workbook()` (workbook -> schema-validated
  canonical spec, ported from the reference kit's `excel_to_spec.py`) then `spec_to_contracts()`
  (canonical spec -> Jarvis's own `source.yaml` / `model.yaml` / `gold.yaml` shapes, **new** --
  not in the reference kit, which only ever emitted static SQL files and never executed against
  a live warehouse). Also emits one ODCS v3 contract per source as a bonus export.
- Retired (git rm, recoverable from history): `emitters/catalogue_compiler.py`,
  `silver_model_compiler.py`, `gold_model_compiler.py`, and the three old `.xlsx` templates.
  Nothing else imported them (checked before removing).
- `webapp/backend/uploads.py` / frontend: the Control Room's upload panel now has one dropzone
  for the one workbook, no kind picker.

## Known, deliberately-flagged translation gaps (not silently papered over)

- **Silver: one bronze source per entity.** `silver_transform.py` does a verbatim column
  SELECT from a single bronze table -- no per-column SQL expression, no multi-source UNION/
  conform. A `08_Silver_Mappings` entity with rows from more than one source, or a non-identity
  expression, compiles fine (so the human reviewer sees it) but only the first source's identity
  columns are actually loaded -- printed as a `WARNING`, not dropped silently. This is real
  engine work for a later phase, not a translator bug.
- **Gold: single-fact marts only.** `gold_transform.py` builds SQL from structured metadata
  (source_fact + join_dimensions + grain + metrics), not by executing a raw SELECT. The
  compiler regex-extracts the FROM-clause table as `source_fact`; a `JOIN` anywhere in the
  workbook's `sql` column is flagged with a warning naming the exact file to hand-edit, rather
  than guessed at.
- **`natural_language` DQ rules** never become SQL here -- they come back as `open_questions`
  on the source contract, for the Freeze gate (Phase C) to actually surface.
- File-level DQ checks the schema doesn't model 1:1 with Jarvis's FQC (`row_count_deviation_pct`,
  `reject_on_schema_drift`, `quarantine_path`) get safe operational defaults (not business
  decisions -- CLAUDE.md rule 4 is about business logic, not a quarantine folder path).

## Real bugs found and fixed while building this (not in scope, found by testing for real)

1. **Cross-domain idempotency leak.** `bronze_loader.py`'s "already loaded" check and its
   trailing-median FQC deviation query didn't filter by `domain` -- two domains whose sources
   happened to share a landing filename pattern would incorrectly skip/pollute each other. Fixed
   in both queries.
2. **Type vocabulary mismatch.** The canonical schema's `data_type` enum (`integer`, `boolean`,
   `double`, ...) doesn't match Jarvis's own (`int`, `bool`, `decimal`, ...) --
   `_DUCK_TYPE_MAP.get(col["type"], "varchar")` was silently falling through to `varchar` for
   e.g. `integer`, and a `range` DQ rule on that column then failed at execute time (`Cannot
   compare VARCHAR and DECIMAL`). Fixed with an explicit translation table in the compiler,
   since two type systems meeting is exactly the translator's job, not `bronze_loader.py`'s.
3. **`gold_transform.py` always tried to build `mart_monthly_performance`** -- a hand-coded mart
   hardcoded against insurance's `fact_premium`/`fact_claim`/`dim_policy`, unconditionally, for
   every domain. Broke immediately when a second domain ran through it for the first time. Now
   gated to `domain == "insurance"`.
4. **Accidental-overwrite risk in the shipped template.** The first draft's worked example used
   `project.domain: insurance` -- approving it unedited would have silently replaced the live
   insurance domain's 10 real source contracts with the toy 1-source example. Fixed two ways:
   the template's default domain is now `example_domain` with an explicit warning comment, and
   `intake_compiler.py` now prints a loud `** WARNING **` (with the existing source list) if
   the target domain directory already has different contracts on disk -- verified to fire
   correctly against a `domain: insurance` test case and stay silent for the safe default.

## Verification

**Full round-trip against real duckdb**, via a throwaway `verify_intake` domain exercising
everything the compiler claims to support -- not a dry run:

- 2 sources (one SCD1, one SCD2 with a real day-2 delta), all 5 implemented attribute DQ rule
  types (`not_null`, `unique`, `in_list`, `range`, and the newly-added `regex`), 1 gold mart.
- Bronze: both sources loaded correctly (5 + 7 rows); the deliberately-invalid regex test value
  was correctly caught as a warning-severity, non-blocking flag (loaded rows, DQ result recorded
  `passed=False`) -- proving severity/action semantics carried through correctly.
- Silver: `dim_vi_product` (SCD1) = 5 rows. `dim_vi_customer` (SCD2) = 7 rows total, 5 current --
  exactly right for 5 day-1 customers with 2 of them versioned by the day-2 delta.
- Gold: `mart_vi_customer_by_tier` = 3 rows (3 distinct risk tiers), built through the real
  declarative mart engine.
- Regex DQC: added to `bronze_loader.py` (`_IMPLEMENTED_RULE_TYPES`), using `regexp_matches`
  (duckdb) -- verified via `sqlglot.transpile` that it clean-transpiles to Databricks'
  `REGEXP_LIKE`, not assumed.
- All throwaway schemas, control-plane rows, contract files and landing files cleaned up after.

**Regression:** full 25-test suite re-run on **both** duckdb and Databricks after every
engine-level change (the domain-filter fix, the gold domain-gate, the regex rule addition, and
after retiring the three old compilers) -- 25/25 on both, every time.

## Not done in this phase

- Multi-source silver conform (needed for a real asset_management demo with Aladdin+BNP-style
  multiple providers into one entity) -- flagged above, real engine work for Phase C/E.
- Joined gold marts via the workbook -- same flag; author `join_dimensions` by hand in the
  compiled `gold.yaml` for now.
- Phases B (DQ/business-rule-check upgrades beyond what's already needed above), C (real agent
  activation), D (frontend Run view), E (onboard asset_management).
