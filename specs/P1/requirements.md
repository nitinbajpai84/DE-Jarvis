# P1 Requirements — Source → Bronze (`orders`)

**Stage**: 2 (Requirement Analysis), produced by Business Analyst
**Input**: `contracts/sources/orders.source.yaml`, `contracts/control/control_model.yaml`,
`contracts/schemas/source.schema.yaml`, `evidence/gates/P1-intent.md`

## Correction to Stage 1

`P1-intent.md` success criterion #4 assumed the duplicate-order-id file loads into bronze "as-is"
with the duplication merely flagged. Re-reading the contract precisely: `quality_rules` declares
`unique(order_id)` at `severity: error`, and `source.schema.yaml`'s own comment on `severity` is
explicit — `error = quarantine batch, warn = load + alert`. Taken literally, a duplicate-order-id
violation quarantines the **entire file's batch**, not just the duplicate rows. This is corrected
below as Open Question 2 rather than silently overriding the Stage 1 intent doc.

## Derived requirements (unambiguous — directly stated in the contract, no judgment needed)

- R1: Files not matching `harness/landing/orders/orders_*.csv` are ignored.
- R2: `expected_columns: 8` + `reject_on_schema_drift: true` is unambiguous on its own — a file
  with a different column count is always rejected to `quarantine_path`. No open question here.
- R3: `order_date` has a declared `format: "%d/%m/%Y"`, but the day-10 fixture deliberately writes
  10% of rows in ISO format instead. Per CLAUDE.md's bronze rule ("original types preserved as
  string where ambiguous... no business logic") and the model contract's own
  `standardisation.date_format: ISO-8601` (a **silver** responsibility), bronze lands `order_date`
  as a raw string, unparsed. Parsing/standardising it is silver's job, not bronze's. Not an open
  question — CLAUDE.md already resolves this.
- R4: `order_ts`, `gross_amount` are typed at load (timestamp, decimal) — both are well-formed in
  every seed file, no ambiguity in the fixtures to preserve.
- R5: `freshness(order_ts, max_age_hours: 30)` at `severity: warn` will trip for **every** row in
  every file, since the seed data is dated 2026-09-01–10 and the harness runs today (2026-09-15).
  This is expected, not a bug — flagging it now so a wall of freshness warnings in
  `dq_results` on review isn't mistaken for a defect.
- R6: Every accepted row carries the lineage columns from `control_model.yaml`
  (`data_catalogue_id`, `source_record_id`) plus `_run_id`, `_source_file`, `_ingested_at`,
  `_record_hash` per CLAUDE.md. `source_record_id` is monotonic 1-based per file, per the control
  model's own comment.
- R7: `control.run_registry` gets exactly one row per invocation of the bronze loader, regardless
  of how many files it processes in that run (CLAUDE.md rule 7 — no silent runs).

## Open Questions (block G1 under normal process; proceeding under the explicit human
instruction recorded in `P1-intent.md` — defaults below are implemented and flagged, not hidden)

**Q1 — Do the numeric `file_checks` (`min_rows`, `row_count_deviation_pct`, `size_deviation_pct`)
reject the file, or load it with a warning?**
Only `reject_on_schema_drift` carries an explicit action flag; the numeric checks don't. Two
readings are both defensible:
- (a) Treat all `file_checks` as hard gates — reject + quarantine on any violation. Consistent,
  conservative, matches "FQC runs before bronze" framing in `control_model.yaml`.
- (b) Treat only schema drift as a hard gate (it's the only one with an explicit `reject_*` flag);
  numeric checks are soft — record the deviation, load anyway, alert.
**Default implemented: (a).** Rationale: a "file quality check" that never blocks anything is an
odd design, and (a) is the safer default for a first pass. **This is a real business call, not a
technical one — flag for your review.**

**Q2 — Does an error-severity `quality_rules` (DQC) violation quarantine the whole file's batch,
or exclude only the offending rows?**
The contract text says "quarantine batch." Row-level exclusion would require deciding *which*
duplicate row to keep — that's a dedup/merge decision, which CLAUDE.md assigns to silver
(SCD2/merge_key), not bronze. **Default implemented: whole-batch quarantine on any error-severity
violation**, consistent with "no business logic in bronze."

**Consequence of both defaults**: under this reading, of the 10 seed files, only the 7 clean days
land in bronze. The dip day, the schema-drift day, and the duplicate/bad-date day are all
quarantined — the last one entirely, over 25 duplicate rows out of 400+. If that feels like too
blunt an instrument for production, Q1/Q2 are exactly where to redirect me; the fix is a contract
change (e.g. adding a `severity` field to `file_checks`, or a `dedup_before_check` flag), not a
code change, which is the point of building it this way.

## Success criteria carried forward from Stage 1, amended

Criterion #4 in `P1-intent.md` is superseded by Q2's default above: the day-10 file is expected
to be **fully quarantined**, not partially loaded with duplicates visible in bronze.
