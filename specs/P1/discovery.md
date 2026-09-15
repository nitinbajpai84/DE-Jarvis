# P1 Codebase Discovery

**Stage**: 3, produced by Program Manager

## Relevant existing state

- `contracts/sources/orders.source.yaml` — complete, already drove requirements.md.
- `contracts/control/control_model.yaml` — defines control-plane table shapes. P1 implements a
  subset: `run_registry`, `file_audit`, `dq_results`, `load_lineage`, `data_object_catalogue`.
  `batch`/`batch_log`/`activity_log` are collapsed into `run_registry` for P1 (single source, no
  orchestration yet) — see Design ADR.
- `dbt/dbt_project.yml`, `dbt/profiles.yml` — dbt is wired for the `dev` (DuckDB) target, verified
  working (`dbt debug` passed in P0 smoke test). No models exist yet.
- `harness/jarvis.duckdb` — exists from the P0 smoke test (created by `dbt debug`), currently has
  no tables. Safe to build on; nothing to migrate away from.
- `harness/landing/orders/*.csv` — 10 seed files already generated and unchanged since P0.
- `emitters/` — empty. No prior ingestion code to reconcile with or duplicate.
- README's tool table assigns ingestion to `dlt`. No existing dlt pipeline in the repo to extend.

## Decision inputs for Stage 4

- Nothing to refactor or reconcile — this is a greenfield implementation within an established
  scaffold. Discovery's main output is confirming there's no hidden prior art to conflict with,
  and pinning down exactly which control-plane tables are in scope (done above).
