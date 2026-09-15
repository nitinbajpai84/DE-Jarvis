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
from typing import Any

import duckdb

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
    phase                varchar,
    started_at            timestamp,
    ended_at              timestamp,
    status                varchar,   -- 'completed' | 'failed'
    error_message          varchar,
    files_seen              integer,
    files_accepted           integer,
    files_quarantined         integer,
    rows_loaded                 bigint
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
    action                varchar   -- 'loaded' | 'quarantined'
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
    loader_status               varchar
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
    evaluated_at                timestamp
);

create table if not exists {control}.load_lineage (
    run_id              varchar,
    source_entity        varchar,
    target_entity          varchar,
    data_catalogue_id        bigint,
    loaded_at                  timestamp
);
"""


def ensure_control_schema(con: duckdb.DuckDBPyConnection, control_schema: str) -> None:
    con.execute(_DDL_TEMPLATE.format(control=control_schema))


def compile_source_registration(
    con: duckdb.DuckDBPyConnection, contract: dict[str, Any], control_schema: str
) -> None:
    """Upsert control.data_system / config_data_source_file from a parsed *.source.yaml.

    Config lives in Git as YAML (the contract); these rows are the compiled, queryable
    runtime view of it -- CLAUDE.md's "config is compiled from Git YAML," not authored here.
    Fields with no CSV-file-glob equivalent (e.g. worksheet handling) are left NULL rather
    than guessed.
    """
    source_id = contract["source_id"]
    conn = contract["connection"]

    con.execute(
        f"""
        insert into {control_schema}.data_system
            (data_source_id, data_source_name, data_source_type, data_source_code,
             data_source_country, description, is_active)
        values (?, ?, ?, ?, ?, ?, true)
        on conflict (data_source_id) do update set
            data_source_name = excluded.data_source_name,
            data_source_type = excluded.data_source_type,
            description = excluded.description
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

    con.execute(
        f"""
        insert into {control_schema}.config_data_source_file
            (data_file_code, data_source_id, file_grouping, file_type, file_name_regex,
             file_format, file_extension, landing_layer_file_name, bronze_table_name,
             effective_date_in_file_name, effective_date_format, is_file_delimited,
             delimiter, has_column_headings, storage_location, frequency,
             is_file_mandatory, is_file_active, sla_arrival_cutoff_time)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, true, ?)
        on conflict (data_file_code) do update set
            storage_location = excluded.storage_location
        """,
        [
            f"{source_id}_file", source_id, contract["domain"], ctype,
            name, fmt, extension,
            location, f"bronze.{source_id}",
            True, None, conn.get("delimiter") is not None, conn.get("delimiter"),
            conn.get("header", False), location,
            contract.get("arrival", {}).get("cadence"),
            True, contract.get("arrival", {}).get("expected_by"),
        ],
    )
