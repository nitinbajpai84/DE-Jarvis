# Gate Record — P1 Architecture Gate

**Phase**: P1 | **Stage**: after 5 (Plan) | **Spec version**: P1-v1 | **Date**: 2026-09-15

## What this freezes

`specs/P1/design.md` + ADR-001 + `specs/P1/plan.md` (below). Once implementation starts,
deviation from this design requires a new logged decision, not a silent change.

## Key bindings being frozen

- Platform: DuckDB only (`contracts/platform/duckdb.yaml`).
- Ingestion approach: direct DuckDB loader, not `dlt` (ADR-001).
- Quarantine semantics: whole-batch quarantine on FQC failure or any error-severity DQC
  violation (requirements.md Q1/Q2 defaults).
- Control-plane scope: 5 tables (`run_registry`, `file_audit`, `data_object_catalogue`,
  `dq_results`, `load_lineage`); `batch`/`batch_log`/`activity_log` deferred.

## Approval

**Status: PENDING** — same basis as `P1-intent.md`: proceeding to implementation under explicit
human instruction to build ahead of synchronous review, not a self-approval. Rollback point will
be tagged in git once implementation and testing are complete, per the Definition of Done.

**Approver**: _(awaiting Nitin's review)_
