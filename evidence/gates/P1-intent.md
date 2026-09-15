# Gate Record — P1 Intent Gate

**Phase**: P1 — Source → Bronze (catalogue-driven)
**Stage**: 1 (Intent), produced by Program Manager
**Date**: 2026-09-15
**Spec version**: P1-v1

## Outcome

Prove that a real source contract can be landed into bronze exactly as specified — every file
checked before load, every bad row caught and recorded (not silently dropped or silently
accepted), and full lineage from a bronze row back to the exact source file and row within it —
with zero hand-written business logic outside `contracts/`.

## In scope

- Source: `orders` only (`contracts/sources/orders.source.yaml`). See "Scope decision" below.
- Landing-zone file checks (`file_checks` block): min rows, expected columns, row-count
  deviation, schema-drift rejection, quarantine on failure.
- Bronze load: append-only, lineage columns per `contracts/control/control_model.yaml`
  (`data_catalogue_id`, `source_record_id`, plus `_run_id`, `_source_file`, `_ingested_at`,
  `_record_hash` per CLAUDE.md layer rules).
- Data-quality checks (`quality_rules` block) evaluated on bronze, results written to
  `control.dq_results`, severity respected (`error` vs `warn`).
- Control plane: `run_registry`, `file_audit`, `dq_results`, `load_lineage`,
  `data_object_catalogue` populated for every run — no silent runs (CLAUDE.md rule 7).
- Target: DuckDB only (`harness/jarvis.duckdb`). Databricks port is P5, not P1.

## Out of scope

- `policy_docs` (unstructured source) — different pattern (LLM entity extraction, confidential
  classification, data-locality rule). Deferred to its own pass once `orders` is proven.
- Silver (conformance, SCD2, dedup) — bronze lands duplicates and bad dates as-is; deduplication
  and standardisation are explicitly silver's job per CLAUDE.md's layer table, not bronze's.
- Orchestration/scheduling — P1 proves the pipeline runs correctly on demand. Recurring
  scheduling is not part of this phase's success criteria.
- Full CDM `batch` / `batch_log` / `activity_log` decomposition from `control_model.yaml` — P1
  collapses these into a single `run_registry` row per run (one source, one run = one batch).
  Revisit when a second concurrent source makes batch-of-batches coordination necessary. Logged
  as an ADR in Stage 4, not silently dropped.

## Success criteria (measurable, checked against real seed data, not asserted)

1. All 10 files in `harness/landing/orders/` are processed; the run does not crash on the
   deliberately broken ones.
2. The schema-drift file (`orders_20260909.csv`, 7 columns) is quarantined, not loaded into
   bronze — `reject_on_schema_drift: true` is enforced, not just documented.
3. The row-count-dip file (`orders_20260908.csv`, 60 rows vs `min_rows: 100`) is flagged in
   `dq_results` / rejected per `file_checks`, not silently accepted.
4. The duplicate + bad-date file (`orders_20260910.csv`) loads into bronze as-is (bronze does not
   dedupe or fix dates) but the `unique(order_id)` and any date-format issue are visible in
   `dq_results` at the appropriate severity.
5. Every accepted row carries `data_catalogue_id`, `source_record_id`, `_run_id`,
   `_source_file`, `_ingested_at`, `_record_hash` — traceable to one exact row in one exact file.
6. `control.run_registry` has exactly one row per pipeline invocation. No run happens without a
   registry row.
7. A different agent identity reviews the implementation than the one that wrote it
   (CLAUDE.md rule 5) — recorded in `evidence/decisions/P1-review.md`.
8. A git tag exists marking the P1 rollback point.

## Open Questions

See `specs/P1/requirements.md` — Stage 2 (BA) output. Anything left unresolved there blocks this
gate under normal process.

## Approval

**Status: PENDING.** Per explicit instruction from the user (2026-09-15, in-session): proceed
through Design → Plan → Implementation ahead of synchronous gate sign-off; review is deferred to
after the work exists rather than blocking before it starts. This is *not* a self-approval — the
Program Manager agent has not approved this gate, and does not have authority to. It is a
documented, human-directed exception to the normal blocking behavior, made by the accountable
human, not inferred by an agent. Rollback remains available via the tagged commit regardless of
when review happens.

**Approver**: _(awaiting Nitin's review)_
**Decision**: _(pending)_
