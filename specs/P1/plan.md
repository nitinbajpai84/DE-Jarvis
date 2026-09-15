# P1 Plan

**Stage**: 5, produced by Program Manager

## Tasks (in order)

1. `emitters/control_plane.py` — DDL for 5 control tables + `compile_source_registration()`.
2. `emitters/bronze_loader.py` — FQC → stage → DQC → promote/quarantine, per design.md.
3. `harness/run_bronze.py` — CLI entry.
4. Run against all 10 seed files in `harness/landing/orders/`.
5. `specs/P1/test-pack.md` + `evidence/runs/P1-bronze-run.md` — observed results vs the success
   criteria in `P1-intent.md` (as amended by requirements.md).
6. Independent review — spawned as a fresh agent with only the contract + diff, not this
   session's accumulated context (CLAUDE.md rule 5, author ≠ reviewer) →
   `evidence/decisions/P1-review.md`.
7. Commit, tag a rollback point.

## Acceptance

Matches `P1-intent.md` success criteria 1, 2, 5, 6, 7, 8 as written; criteria 3-4 as amended by
`requirements.md` (both the dip file and the duplicate file are now expected to be fully
quarantined, not partially loaded).
