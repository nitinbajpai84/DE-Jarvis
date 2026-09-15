# P1 Design — Source → Bronze (`orders`)

**Stage**: 4, produced by Solution Architect + Data Architect
**See also**: ADR-001 (ingestion approach)

## Control plane (DuckDB schema `control`)

Subset of `contracts/control/control_model.yaml`, scoped per `specs/P1/discovery.md`:

```sql
control.run_registry(run_id, source_id, phase, started_at, ended_at, status, files_seen,
                      files_accepted, files_quarantined, rows_loaded)
control.file_audit(file_audit_id, run_id, file_name, file_location, size_kb, row_count,
                    column_count, checksum, arrival_time, fqc_passed, action)  -- action: loaded|quarantined
control.data_object_catalogue(data_catalogue_id, data_source_id, file_name, file_location,
                               number_of_rows, file_size_kb, checksum, created_at,
                               loader_status)  -- the lineage anchor per control_model.yaml
control.dq_results(result_id, run_id, data_catalogue_id, rule_type, columns, severity,
                    passed, observed_value, failed_row_count, evaluated_at)
control.load_lineage(run_id, source_entity, target_entity, data_catalogue_id, loaded_at)
```

`batch` / `batch_log` / `activity_log` are not separately materialised in P1 — one
`run_registry` row *is* the batch record for a single-source run. Revisit when orchestration
across concurrent sources needs the extra decomposition (noted in `P1-intent.md` out-of-scope).

## Bronze table

```sql
bronze.orders(
  data_catalogue_id,   -- FK to control.data_object_catalogue: which FILE
  source_record_id,    -- monotonic 1-based: which ROW within that file
  _run_id, _source_file, _ingested_at, _record_hash,
  order_id, customer_id, order_date,  -- STRING, unparsed -- see requirements.md R3
  order_ts, status, currency, gross_amount, country
)
```

`order_date` stays a raw string in bronze deliberately (R3). Everything else types per the
contract's declared `schema:` block — none of the other columns have a format ambiguity in the
fixtures.

## Load algorithm (per file, per requirements.md Q1/Q2 defaults)

1. **FQC (pre-load)**: compute row count, column count, size. Check against `file_checks`
   (`min_rows`, `expected_columns`, `row_count_deviation_pct` vs trailing 7-run median,
   `size_deviation_pct`, `reject_on_schema_drift`). Any violation → write `file_audit` with
   `action=quarantined`, copy file to `quarantine_path`, **do not read rows**. Otherwise continue.
2. **Stage**: read the file, attach lineage columns, insert into a scratch table
   (`bronze._staging_orders`, dropped/recreated per run — not a permanent object).
3. **DQC (on staged batch)**: evaluate every `quality_rules` entry against the staged rows.
   Write one `dq_results` row per rule per file. Any `severity: error` violation anywhere in the
   batch → the whole batch is rejected: no promotion to `bronze.orders`, `file_audit.action`
   updated to `quarantined`, source file copied to `quarantine_path` for consistency with FQC
   rejections. `severity: warn` violations are recorded but do not block promotion.
4. **Promote**: if the batch passed DQC, `INSERT INTO bronze.orders SELECT * FROM
   bronze._staging_orders`, write `data_object_catalogue` + `load_lineage` rows.
5. **Always**: one `run_registry` row per invocation (not per file) — counts of files
   seen/accepted/quarantined and rows loaded, `started_at`/`ended_at`/`status`.

## File layout

- `emitters/control_plane.py` — DDL for the 5 control tables; `compile_source_registration()`
  writes `data_system` + `config_data_source_file` rows from a `*.source.yaml` contract (so the
  control plane reflects the contract, per CLAUDE.md's "config is compiled from Git YAML").
- `emitters/bronze_loader.py` — the load algorithm above, parameterised by `source_id`. Raises
  clearly (doesn't guess) for a `connection.type` it doesn't support yet — only `file`/`csv` is
  implemented for P1.
- `harness/run_bronze.py` — CLI entry: `python harness/run_bronze.py --source orders`.

## What P1 explicitly does not do

No dedup, no date standardisation, no SCD, no aggregation — all silver/gold concerns per
CLAUDE.md's layer table. Bronze's only job is: is this file/batch trustworthy enough to land,
and if so, land it with full traceability.
