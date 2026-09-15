# ADR-001: Direct DuckDB loader for P1, not dlt

Date:     2026-09-15
Status:   PROPOSED, NOT ACCEPTED -- see "Independent review finding" below
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
