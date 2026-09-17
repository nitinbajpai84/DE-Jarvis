"""Control-plane DDL and contract-compilation, per contracts/control/control_model.yaml.

P1 scope: run_registry, file_audit, data_object_catalogue, dq_results, load_lineage.
batch/batch_log/activity_log are deliberately not materialised yet -- see ADR-001 and
specs/P1/design.md. One run_registry row stands in for a batch record until a second,
concurrent source makes true batch/activity decomposition necessary.

Schema name is a parameter, not a literal -- sourced from contracts/platform/<target>.yaml's
storage.control (CLAUDE.md rule 1: nothing hardcoded that isn't traceable to a contract).
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone
from typing import Any

from emitters.sql_dialect import SqlConnection

_DDL_TEMPLATE = """
create schema if not exists {control};

create table if not exists {control}.data_system (
    data_source_id      varchar primary key,
    data_source_name    varchar,
    data_source_type    varchar,
    data_source_code    varchar,
    data_source_country varchar,
    description         varchar,
    is_active            boolean
);

create table if not exists {control}.config_data_source_file (
    data_file_code              varchar primary key,
    data_source_id               varchar,
    file_grouping                 varchar,
    file_type                     varchar,
    file_name_regex               varchar,
    file_format                   varchar,
    file_extension                 varchar,
    landing_layer_file_name        varchar,
    bronze_table_name              varchar,
    effective_date_in_file_name    boolean,
    effective_date_format          varchar,
    is_file_delimited              boolean,
    delimiter                      varchar,
    has_column_headings            boolean,
    storage_location                varchar,
    frequency                       varchar,
    is_file_mandatory               boolean,
    is_file_active                  boolean,
    sla_arrival_cutoff_time          varchar
);

create table if not exists {control}.run_registry (
    run_id              varchar primary key,
    source_id           varchar,
    phase                varchar,   -- 'bronze' | 'silver' | 'gold'
    started_at            timestamp,
    ended_at              timestamp,
    status                varchar,   -- 'completed' | 'failed'
    error_message          varchar,
    files_seen              integer,
    files_accepted           integer,
    files_quarantined         integer,
    rows_loaded                 bigint,
    columns_processed            integer,
    domain                        varchar,
    client                         varchar
);

create table if not exists {control}.file_audit (
    file_audit_id    bigint primary key,
    run_id            varchar,
    file_name         varchar,
    file_location      varchar,
    size_kb             double,
    row_count           integer,
    column_count        integer,
    checksum             varchar,
    arrival_time          timestamp,
    fqc_passed            boolean,
    action                varchar,  -- 'loaded' | 'quarantined'
    domain                 varchar,
    client                   varchar
);

create table if not exists {control}.data_object_catalogue (
    data_catalogue_id   bigint primary key,
    data_source_id       varchar,
    file_name             varchar,
    file_location          varchar,
    number_of_rows          integer,
    file_size_kb             double,
    checksum                  varchar,
    created_at                 timestamp,
    loader_status               varchar,
    domain                       varchar,
    client                        varchar
);

create table if not exists {control}.dq_results (
    result_id          bigint primary key,
    run_id              varchar,
    data_catalogue_id    bigint,
    rule_type             varchar,
    columns                varchar,
    severity                varchar,
    passed                   boolean,
    observed_value            varchar,
    failed_row_count          integer,
    evaluated_at                timestamp,
    domain                       varchar,
    client                        varchar
);

create table if not exists {control}.load_lineage (
    run_id              varchar,
    source_entity        varchar,
    target_entity          varchar,
    data_catalogue_id        bigint,
    loaded_at                  timestamp
);

create table if not exists {control}.sdlc_run (
    run_id           varchar primary key,
    domain            varchar,
    client             varchar,
    project_code        varchar,
    workbook_path         varchar,
    thread_id               varchar,   -- deepagents/langgraph checkpoint thread id -- how a
                                        -- later 'approve' call resumes this exact run
    status                   varchar,   -- 'running' | 'awaiting_approval' | 'approved' |
                                        -- 'rejected' | 'completed' | 'failed'
    started_at                 timestamp,
    updated_at                   timestamp,
    started_by                     varchar
);

create table if not exists {control}.incident_ticket (
    ticket_id         bigint primary key,
    domain             varchar,
    client              varchar,
    phase                varchar,   -- 'bronze' | 'silver' | 'gold'
    entity                varchar,   -- source_id / dimension / fact / mart the issue is about
    issue_type             varchar,   -- 'quarantine' | 'dq_failure' | 'orphan_fk' | 'test_pack_failure'
    severity                 varchar,   -- 'warning' | 'error'
    description               varchar,
    status                     varchar,   -- 'open' | 'assigned' | 'fix_pending_approval' |
                                            -- 'resolved' | 'rejected' | 'closed'
    assigned_to                  varchar,   -- agent name, e.g. 'de-silver'
    raised_by                      varchar,
    raised_at                        timestamp,
    resolution_note                    varchar,
    resolved_by                          varchar,
    resolved_at                            timestamp,
    verified_by_test                        boolean
);

create table if not exists {control}.sdlc_stage_run (
    id                bigint primary key,
    run_id             varchar,
    stage               varchar,  -- discover | specify | freeze | build | validate | operate
                                   -- (deepagents' six-stage factory -- see agents/jarvis_tools.py's
                                   -- module docstring for how this maps to the 10-stage
                                   -- docs/agentic-sdlc.md view the rest of the Control Room uses)
    agent                  varchar,
    status                    varchar,  -- 'started' | 'completed' | 'failed' | 'awaiting_human'
    detail                      varchar,
    started_at                    timestamp,
    ended_at                        timestamp
);
"""


# Tables that predate the multi-domain design, and the columns each has picked up since it
# was first created -- "create table if not exists" is a no-op against an already-existing
# table, so every column added after a table's first release needs its own migration entry
# here. (table, column, ddl_type, backfill_value-or-None) -- backfill runs only for columns
# added in the domain/client migration, so pre-existing single-domain rows don't end up
# NULL and invisible to a domain-scoped query the moment one is added.
_MIGRATIONS: list[tuple[str, str, str, str | None]] = [
    ("run_registry", "columns_processed", "integer", None),
    ("run_registry", "domain", "varchar", "insurance"),
    ("run_registry", "client", "varchar", "default"),
    ("file_audit", "domain", "varchar", "insurance"),
    ("file_audit", "client", "varchar", "default"),
    ("data_object_catalogue", "domain", "varchar", "insurance"),
    ("data_object_catalogue", "client", "varchar", "default"),
    ("dq_results", "domain", "varchar", "insurance"),
    ("dq_results", "client", "varchar", "default"),
]


_SCHEMA_ENSURED: set[tuple[str, str, str]] = set()


def ensure_control_schema(con: SqlConnection, control_schema: str) -> None:
    # Called at the top of nearly every control-plane read/write, so on a page load that fires
    # a dozen API calls in parallel (the Control Room does exactly this), several land here at
    # once. DuckDB is single-writer -- a "create table if not exists" or "alter table" racing
    # another connection's DDL raises TransactionException ("write-write conflict"), confirmed
    # live via the Operations screen's concurrent /api/tickets + /api/agent-runs/* calls, not
    # theoretical. Same retry-on-conflict pattern already proven for log_sdlc_stage/raise_ticket:
    # the DDL is idempotent, so re-running it after another connection's commit is always safe.
    import random
    import time
    # On a remote warehouse the setup below is ~20 round trips (11 CREATEs, 9 column checks):
    # measured at 10 s per call on Databricks, and a Databricks journey opened enough connections
    # to spend 90 s of a page load re-creating tables that already existed. It runs once per
    # process per warehouse+schema there. DuckDB keeps running it every time: it's local and
    # cheap, and tests point fresh database files at the same schema name.
    key = None
    if con.dialect != "duckdb":
        import os
        key = (con.dialect, os.environ.get("DATABRICKS_HOST", ""), control_schema)
        if key in _SCHEMA_ENSURED:
            return
    for attempt in range(5):
        try:
            _ensure_control_schema_once(con, control_schema)
            if key:
                _SCHEMA_ENSURED.add(key)
            return
        except Exception:  # noqa: BLE001 -- retry on a concurrent-DDL conflict; re-raise otherwise
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1) + random.random() * 0.03)


def _ensure_control_schema_once(con: SqlConnection, control_schema: str) -> None:
    # SqlConnection.execute() takes one statement at a time (Databricks' connector rejects
    # more than one per call) -- split the template rather than relying on either driver to
    # handle a semicolon-joined batch.
    for statement in _DDL_TEMPLATE.format(control=control_schema).split(";"):
        statement = statement.strip()
        if statement:
            con.execute(statement)

    # `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` transpiles to itself on Databricks (looks
    # valid, isn't -- Databricks SQL doesn't have that clause, confirmed against a real
    # warehouse, not assumed from sqlglot's output) -- same class of trap as ON CONFLICT.
    # Check-then-alter is the portable version; a fresh table already has every column from
    # the DDL above, so its columns are skipped here, not double-added.
    for table, column, ddl_type, backfill in _MIGRATIONS:
        try:
            cur = con.execute(f"select * from {control_schema}.{table} limit 0")
            existing_cols = {d[0] for d in cur.description}
        except Exception:  # noqa: BLE001 -- table genuinely doesn't exist yet on this run; nothing to migrate
            continue
        if column in existing_cols:
            continue
        con.execute(f"alter table {control_schema}.{table} add column {column} {ddl_type}")
        if backfill is not None:
            con.execute(f"update {control_schema}.{table} set {column} = ? where {column} is null", [backfill])


def log_run(
    con: SqlConnection, control_schema: str, *, run_id: str, source_id: str, phase: str,
    started_at, ended_at, status: str, error_message: str | None, files_seen: int,
    files_accepted: int, files_quarantined: int, rows_loaded: int, columns_processed: int,
    domain: str = "insurance", client: str = "default",
) -> None:
    """One control.run_registry row -- shared by bronze/silver/gold so 'every run writes to
    run_registry' (CLAUDE.md rule 7) actually holds for all three layers, not just bronze. Used
    by silver_transform.py and gold_transform.py; bronze_loader.py's own insert predates this
    helper and works correctly, left as-is rather than churned for a pure refactor."""
    con.execute(
        f"insert into {control_schema}.run_registry values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [run_id, source_id, phase, started_at, ended_at, status, error_message,
         files_seen, files_accepted, files_quarantined, rows_loaded, columns_processed,
         domain, client],
    )


def start_sdlc_run(
    con: SqlConnection, control_schema: str, *, run_id: str, domain: str, client: str,
    project_code: str, workbook_path: str, thread_id: str, started_by: str,
) -> None:
    now = datetime.now(timezone.utc)
    con.execute(
        f"insert into {control_schema}.sdlc_run values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [run_id, domain, client, project_code, workbook_path, thread_id, "running", now, now, started_by],
    )


def update_sdlc_run_status(con: SqlConnection, control_schema: str, run_id: str, status: str) -> None:
    now = datetime.now(timezone.utc)
    con.execute(
        f"update {control_schema}.sdlc_run set status = ?, updated_at = ? where run_id = ?",
        [status, now, run_id],
    )


def log_sdlc_stage(
    con: SqlConnection, control_schema: str, *, run_id: str, stage: str, agent: str,
    status: str, detail: str, started_at, ended_at=None,
) -> None:
    """One row per stage transition -- a stage can log more than once (e.g. 'started' then
    'completed'), so this is an append-only trail, not an upsert; the Control Room's Run view
    (Phase D) reads the latest row per stage for current status and the full history for the
    timeline underneath it.

    The 'select max(id)+1, then insert' pattern every other control-plane table in this file
    uses is NOT atomic, and this is the one table where that has actually bitten: an agent's
    tool calls land here from langgraph's tool-execution layer closely enough together that two
    calls can read the same max(id) before either commits, and the second insert then fails on
    the primary key -- confirmed via a real interrupt/resume agent run during Phase C, not
    theoretical. A short retry-on-conflict loop, rather than a schema change (an IDENTITY column
    would need dialect-specific DDL, the exact kind of platform branching this project avoids
    everywhere else)."""
    import random
    import time
    for attempt in range(5):
        try:
            next_id = con.execute(f"select coalesce(max(id), 0) + 1 from {control_schema}.sdlc_stage_run").fetchone()[0]
            con.execute(
                f"insert into {control_schema}.sdlc_stage_run values (?, ?, ?, ?, ?, ?, ?, ?)",
                [next_id, run_id, stage, agent, status, detail, started_at, ended_at],
            )
            return
        except Exception:  # noqa: BLE001 -- retry on a concurrent-insert conflict; re-raise if it's something else
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1) + random.random() * 0.03)


def raise_ticket(
    con: SqlConnection, control_schema: str, *, domain: str, client: str, phase: str,
    entity: str, issue_type: str, severity: str, description: str, raised_by: str,
) -> int:
    """One control.incident_ticket row -- the Ops Manager's real, trackable record of a
    detected issue, as opposed to a Slack message that scrolls away. Same retry-on-conflict
    pattern as log_sdlc_stage's ID assignment (proven necessary by a real Phase C bug, not
    theoretical): an Ops Manager tick and a human clicking "raise ticket" in the Control Room
    can land close enough together to read the same max(ticket_id) before either commits."""
    import random
    import time
    now = datetime.now(timezone.utc)
    for attempt in range(5):
        try:
            ticket_id = con.execute(
                f"select coalesce(max(ticket_id), 0) + 1 from {control_schema}.incident_ticket"
            ).fetchone()[0]
            con.execute(
                f"insert into {control_schema}.incident_ticket values "
                f"(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [ticket_id, domain, client, phase, entity, issue_type, severity, description,
                 "open", None, raised_by, now, None, None, None, None],
            )
            return ticket_id
        except Exception:  # noqa: BLE001 -- retry on a concurrent-insert conflict; re-raise otherwise
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1) + random.random() * 0.03)


def update_ticket(con: SqlConnection, control_schema: str, ticket_id: int, **fields) -> None:
    """Generic field updater for one ticket -- status transitions (raise -> assign ->
    fix_pending_approval -> resolved/rejected -> closed) all go through this rather than a
    separate function per transition, since every transition is "set some columns, same row."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    con.execute(
        f"update {control_schema}.incident_ticket set {set_clause} where ticket_id = ?",
        [*fields.values(), ticket_id],
    )


def compile_source_registration(
    con: SqlConnection, contract: dict[str, Any], control_schema: str, bronze_schema: str
) -> None:
    """Upsert control.data_system / config_data_source_file from a parsed *.source.yaml.

    Config lives in Git as YAML (the contract); these rows are the compiled, queryable
    runtime view of it -- CLAUDE.md's "config is compiled from Git YAML," not authored here.
    Fields with no CSV-file-glob equivalent (e.g. worksheet handling) are left NULL rather
    than guessed.
    """
    source_id = contract["source_id"]
    conn = contract["connection"]

    # DELETE + INSERT rather than an upsert: Databricks/Delta has no ON CONFLICT, and sqlglot
    # transpiles it to itself rather than a valid MERGE INTO -- a silent no-op, not a real fix
    # (see ADR-001's resolution note). Delete-then-insert needs no dialect-specific SQL at all,
    # which is the actual point of "write once" -- not a workaround bolted on afterward.
    con.execute(f"delete from {control_schema}.data_system where data_source_id = ?", [source_id])
    con.execute(
        f"""
        insert into {control_schema}.data_system
            (data_source_id, data_source_name, data_source_type, data_source_code,
             data_source_country, description, is_active)
        values (?, ?, ?, ?, ?, ?, true)
        """,
        [source_id, source_id, contract["domain"], source_id, None,
         f"{contract['domain']} / {contract['classification']}"],
    )

    # Not every connection.type has a "path" -- database uses "table", api uses "endpoint".
    # Derive the location/name/format fields per type rather than assuming file-shaped.
    ctype = conn.get("type")
    if ctype == "file":
        location, name, fmt = conn["path"], pathlib.PurePosixPath(conn["path"]).name, conn.get("format")
        extension = pathlib.PurePosixPath(conn["path"]).suffix.lstrip(".")
    elif ctype == "database":
        location, name, fmt, extension = conn["table"], conn["table"], conn.get("dialect"), None
    elif ctype == "api":
        location, name, fmt, extension = conn["endpoint"], conn["endpoint"].rsplit("/", 1)[-1], "json", None
    elif ctype == "unstructured":
        location, name = conn["path"], pathlib.PurePosixPath(conn["path"]).name
        fmt, extension = ",".join(conn.get("formats", [])), None
    else:
        raise NotImplementedError(f"connection.type={ctype!r} not implemented in compile_source_registration")

    con.execute(f"delete from {control_schema}.config_data_source_file where data_file_code = ?",
                [f"{source_id}_file"])
    con.execute(
        f"""
        insert into {control_schema}.config_data_source_file
            (data_file_code, data_source_id, file_grouping, file_type, file_name_regex,
             file_format, file_extension, landing_layer_file_name, bronze_table_name,
             effective_date_in_file_name, effective_date_format, is_file_delimited,
             delimiter, has_column_headings, storage_location, frequency,
             is_file_mandatory, is_file_active, sla_arrival_cutoff_time)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, true, ?)
        """,
        [
            f"{source_id}_file", source_id, contract["domain"], ctype,
            name, fmt, extension,
            location, f"{bronze_schema}.{source_id}",
            True, None, conn.get("delimiter") is not None, conn.get("delimiter"),
            conn.get("header", False), location,
            contract.get("arrival", {}).get("cadence"),
            True, contract.get("arrival", {}).get("expected_by"),
        ],
    )
