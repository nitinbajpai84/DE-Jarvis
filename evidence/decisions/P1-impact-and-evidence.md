# P1 Impact Analysis & Evidence Summary

**Stages**: 9 (Impact Analysis, Solution Architect) + 10 (Evidence, Evidence Agent)

## Impact Analysis

No downstream consumers exist yet — silver/gold are not built. The impact surface P1 creates
for P2 (Bronze → Silver) to depend on:

- `bronze.orders` schema (8 source columns + 6 lineage columns) — P2 must consume `order_date`
  as a raw string and do its own parsing/standardisation (R3); this is now a hard dependency, not
  just a design note.
- Control-plane table shapes (`run_registry`, `file_audit`, `data_object_catalogue`,
  `dq_results`, `load_lineage`) are now real, not just contract text — P2 should extend the same
  5 tables rather than inventing parallel ones for silver.
- **Breaking-change risk for P2**: if ADR-001's rule-2 conflict gets resolved by switching to
  `dlt`, the control-plane writing pattern (direct SQL inserts against `control.*`) may need to
  change shape too. Flagging now so P2 planning accounts for it rather than discovering it mid-build.

## Evidence trail (traceability)

| Artifact | Path |
|---|---|
| Intent | `evidence/gates/P1-intent.md` |
| Requirements + Open Questions | `specs/P1/requirements.md` |
| Discovery | `specs/P1/discovery.md` |
| Design | `specs/P1/design.md` |
| ADR | `evidence/decisions/ADR-001-p1-ingestion-approach.md` |
| Plan | `specs/P1/plan.md` |
| Implementation | `emitters/control_plane.py`, `emitters/bronze_loader.py`, `harness/run_bronze.py` |
| Test run | `evidence/runs/P1-bronze-run.md` |
| Review | `evidence/decisions/P1-review.md` |
| This file | `evidence/decisions/P1-impact-and-evidence.md` |

## Rollback

Tagged in git as `p1-bronze-v1` at the commit that includes this file.
