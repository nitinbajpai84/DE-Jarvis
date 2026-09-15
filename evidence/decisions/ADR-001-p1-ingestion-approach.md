# ADR-001: Direct DuckDB loader for P1, not dlt

Date:     2026-09-15
Status:   ACCEPTED, as amended -- see "Resolution" below
Phase:    P1

## Context

`README.md`'s tool table assigns ingestion to `dlt` for source/destination agnosticism and schema
evolution. P1 needs a load path that: runs FQC (`file_checks`) *before* any row is committed,
stages loaded rows, runs DQC (`quality_rules`) against the staged batch, and only then promotes
the batch into `bronze.orders` — or quarantines it whole, per `specs/P1/requirements.md`
Q1/Q2. That stage → validate → promote transaction is the forcing constraint: it needs to run
inside one control flow against one DuckDB connection, and `dlt`'s resource/destination
abstraction is built around a different shape (extract → normalize → load, with its own state
and schema-evolution machinery) that doesn't map cleanly onto "hold the batch, decide, then
commit or discard."

## Options considered

| Option | Pros | Cons | Platform portability |
|--------|------|------|----------------------|
| dlt resource + destination | Matches README's stated tool choice; built-in incremental state, schema evolution | Stage→validate→promote doesn't fit dlt's load model without fighting the abstraction for a single append-only glob source; that value isn't exercised yet anyway | Genuinely portable across destinations, but that portability isn't tested by one local CSV source |
| Direct DuckDB Python loader | Full control over the load transaction; simple to test against real seed files; no framework fighting | Not portable as written — will need a real emitter abstraction once a second source or the Databricks port (P5) arrives | DuckDB-only today, by design |

## Decision

Use a direct DuckDB loader (`emitters/control_plane.py` + `emitters/bronze_loader.py`) for P1.
Revisit `dlt` when either (a) a second structured source needs the same load logic (the point
where hand-rolling starts costing more than the abstraction), or (b) P5 needs the same pipeline
to run against Databricks, which is exactly what `dlt`'s destination abstraction is for.

## Consequences

Easier: FQC-before-load, stage→validate→promote, and quarantine are straightforward to express
and test correctly against real files right now. Harder: this loader is DuckDB-specific and will
need to be reworked or wrapped when the Databricks port happens — that rework is deferred debt,
not avoided debt, and should be sized honestly at P5 planning rather than assumed away.

## Portability note

Does not hold on databricks/snowflake/fabric as written. `contracts/sources/orders.source.yaml`
and `contracts/control/control_model.yaml` remain the portable artifacts — the loader consuming
them is the platform-specific part, exactly as `contracts/platform/<target>.yaml` anticipates.

## Independent review finding (Stage 8, 2026-09-15)

The Reviewer agent flagged that this ADR conflicts with CLAUDE.md rule 2 — "Never write
platform-specific SQL by hand... Target is read from `contracts/platform/<target>.yaml`" — which
sits under "Hard rules (non-negotiable)" with **no documented override mechanism**, unlike rule
4's explicit Open-Questions escape valve. An ADR recording a tradeoff is not the same thing as
that rule granting authority to waive itself. Partial remediation applied same-day: the loader
now reads `contracts/platform/<target>.yaml` for schema names rather than hardcoding
`"bronze"`/`"control"` literals, and refuses any target other than `duckdb` rather than silently
assuming portability it doesn't have. It still writes hand-written DuckDB SQL, not `dlt`/
`sqlglot`-mediated code — that part of the deviation stands, undecided, pending your explicit
call. This is the single item from this phase most worth your attention on review; everything
else in this ADR is a disclosed, defensible tradeoff, but this one needs a yes/no, not a nod.

## Resolution (2026-09-15, later the same day)

Human decision: sqlglot dialect transpile, chosen over adopting `dlt` fully or hand-writing a
second Databricks-specific loader. Built `emitters/sql_dialect.py` -- a `SqlConnection` wrapper
unifying `duckdb.DuckDBPyConnection` and `databricks.sql.Connection` behind one
`.execute()`/`.executemany()` interface. Every other module still authors exactly one SQL string
per query, in duckdb dialect; `sql_dialect.py` is the only place that knows a second platform
exists, which is what rule 2 actually asks for.

Verified empirically against a live Databricks warehouse before trusting any of it, not assumed
from documentation:
- `?` positional parameters work despite the connector reporting `paramstyle='named'`.
- `DATEDIFF(HOUR, start, end)` (sqlglot's transpile target for `date_diff`) is valid.
- `PRIMARY KEY` in `CREATE TABLE` is accepted (informational, not enforced) -- fine for this use.
- **`ON CONFLICT` is not valid Databricks SQL, and sqlglot transpiles it to itself rather than
  erroring or rewriting to `MERGE INTO`** -- a silent-failure trap that would only surface at
  execute time. The two upserts in `control_plane.py` that needed this were rewritten as
  DELETE+INSERT, which needs no dialect-specific SQL at all -- not a hand-written `MERGE`
  branch, which would have just reintroduced the rule this ADR exists to satisfy.
- A genuine multi-statement-per-call gap: `SqlConnection.execute()` now refuses more than one
  statement per call outright (Databricks' connector rejects it; sqlglot silently drops all but
  the first transpiled statement otherwise) -- `ensure_control_schema` was changed to loop over
  individual statements rather than relying on either driver to handle a semicolon-joined batch.

Separately, a real performance bug surfaced loading data at scale on Databricks: row-by-row
`INSERT` is fine locally but was measured as impractical over the network (orders' 3,359 rows
staged only ~540 before the run was abandoned). The DBAPI cursor's own `.executemany()` looked
like the fix; measurement showed no improvement, because `databricks-sql-connector`'s
implementation is a bare loop over `.execute()` with, per its own docstring, "no optimizations
of the query (like batching) ... performed." Real fix: `SqlConnection.executemany()` builds one
actual multi-row `INSERT ... VALUES (...), (...), ...` per 500-row chunk. Confirmed by timed
re-run: a 5,000-row table went from not completing in 5+ minutes to ~100 seconds total, most of
which is connection/warehouse overhead, not data transfer.

**Status**: accepted as amended. Proven end-to-end against a real target, not just designed --
all 10 insurance-model sources load correctly on both `duckdb` and `databricks`, verified by
direct row-count comparison across both platforms after the full load.
