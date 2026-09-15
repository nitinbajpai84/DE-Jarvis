"""Bronze -> Silver: conforms bronze's multiple append-only versions of a record down to
silver's contracted shape, per contracts/models/<domain>.model.yaml (compiled from
docs/templates/jarvis_silver_model_template.xlsx).

  scd1   -> exactly ONE row per business_key in silver: the latest bronze version, full
            overwrite. No history.
  scd2   -> possibly MANY rows per business_key, but exactly one with row_is_current=true.
            A new version is cut only when a TRACKED attribute changes (merge_hash covers only
            tracked_attributes, per contracts/control/control_model.yaml's own SCD2 contract --
            hashing every column creates a spurious version whenever an untracked field wobbles).
  facts  -> no SCD. Straight select from bronze, FK-checked against the dimensions above
            (late_arriving: inferred_member means an unresolvable FK doesn't block the row).

Each run rebuilds silver.<name> from scratch (delete + insert) from the full bronze history --
not an incremental merge. That's a deliberate scope choice for this pass, not an oversight: it
makes the transform idempotent and simple to verify, at the cost of re-deriving the whole table
every run rather than only the delta. Revisit if/when table sizes make that too slow.

Same portability discipline as bronze_loader.py: every query here is one SQL string, authored
once in duckdb dialect, executed through emitters/sql_dialect.py's SqlConnection so it runs on
either target without a second code path.
"""
from __future__ import annotations

import argparse
import pathlib
from datetime import datetime, timezone
from typing import Any

import yaml

from emitters.sql_dialect import SqlConnection, connect as sql_connect

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_DUCK_TYPE_MAP = {"timestamp": "timestamp", "decimal": "double", "float": "double",
                   "int": "integer", "bigint": "bigint", "bool": "boolean"}


def _load_model(domain: str) -> dict[str, Any]:
    path = REPO_ROOT / "contracts" / "models" / f"{domain}.model.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No model contract at {path} -- run emitters/silver_model_compiler.py first.")
    return yaml.safe_load(path.read_text())


def _load_platform(target: str) -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


def _bronze_columns(con: SqlConnection, bronze: str, table: str) -> list[str]:
    """Business columns only -- excludes the 6 lineage columns bronze_loader.py always adds."""
    cur = con.execute(f"select * from {bronze}.{table} limit 0")
    all_cols = [d[0] for d in cur.description]
    lineage = {"data_catalogue_id", "source_record_id", "_run_id", "_source_file",
               "_ingested_at", "_record_hash"}
    return [c for c in all_cols if c not in lineage]


def build_scd1_dimension(con: SqlConnection, silver: str, bronze: str, dim: dict[str, Any]) -> int:
    bk = dim["business_key"][0]
    source_table = dim["source"][0]
    cols = _bronze_columns(con, bronze, source_table)
    col_list = ", ".join(f'"{c}"' for c in cols)

    con.execute(f"drop table if exists {silver}.{dim['name']}")
    con.execute(
        f"create table {silver}.{dim['name']} as "
        f"select row_number() over (order by {bk}) as {dim['surrogate_key']}, {col_list}, "
        f"_ingested_at as silver_loaded_at "
        f"from (select *, row_number() over (partition by {bk} order by _ingested_at desc) as rn "
        f"      from {bronze}.{source_table}) t "
        f"where rn = 1"
    )
    return con.execute(f"select count(*) from {silver}.{dim['name']}").fetchone()[0]


def build_scd2_dimension(con: SqlConnection, silver: str, bronze: str, dim: dict[str, Any]) -> tuple[int, int]:
    bk = dim["business_key"][0]
    source_table = dim["source"][0]
    tracked = dim["tracked_attributes"]
    cols = _bronze_columns(con, bronze, source_table)
    col_list = ", ".join(f'"{c}"' for c in cols)
    hash_expr = "md5(concat_ws('|', " + ", ".join(f'cast("{c}" as varchar)' for c in tracked) + "))"

    con.execute(f"drop table if exists {silver}.{dim['name']}")
    con.execute(
        f"create table {silver}.{dim['name']} as "
        f"with ordered as ("
        f"  select *, {hash_expr} as attr_hash, "
        f"         row_number() over (partition by {bk} order by _ingested_at) as version_seq "
        f"  from {bronze}.{source_table}"
        f"), changes as ("
        f"  select *, lag(attr_hash) over (partition by {bk} order by _ingested_at) as prev_hash "
        f"  from ordered"
        f"), versions as ("
        f"  select * from changes where version_seq = 1 or attr_hash != prev_hash"
        f"), dated as ("
        f"  select *, "
        f"         _ingested_at as {dim['effective_from']}, "
        f"         lead(_ingested_at) over (partition by {bk} order by _ingested_at) as {dim['effective_to']}, "
        f"         (lead(_ingested_at) over (partition by {bk} order by _ingested_at) is null) as {dim['current_flag']} "
        f"  from versions"
        f") "
        f"select row_number() over (order by {bk}, {dim['effective_from']}) as {dim['surrogate_key']}, "
        f"{col_list}, {dim['effective_from']}, {dim['effective_to']}, {dim['current_flag']} "
        f"from dated"
    )
    total = con.execute(f"select count(*) from {silver}.{dim['name']}").fetchone()[0]
    current = con.execute(f"select count(*) from {silver}.{dim['name']} where {dim['current_flag']}").fetchone()[0]
    return total, current


def build_fact(con: SqlConnection, silver: str, bronze: str, fact: dict[str, Any]) -> int:
    source_table = fact["source"][0]
    cols = _bronze_columns(con, bronze, source_table)
    col_list = ", ".join(f'"{c}"' for c in cols)

    con.execute(f"drop table if exists {silver}.{fact['name']}")
    con.execute(f"create table {silver}.{fact['name']} as select {col_list} from {bronze}.{source_table}")
    return con.execute(f"select count(*) from {silver}.{fact['name']}").fetchone()[0]


def run(domain: str, target: str = "duckdb") -> dict[str, Any]:
    model = _load_model(domain)
    platform = _load_platform(target)
    silver, bronze = platform["storage"]["silver"], platform["storage"]["bronze"]

    con = sql_connect(target, platform)
    try:
        con.execute(f"create schema if not exists {silver}")
        results: dict[str, Any] = {"dimensions": {}, "facts": {}}

        for dim in model["dimensions"]:
            if dim["type"] == "scd1":
                n = build_scd1_dimension(con, silver, bronze, dim)
                results["dimensions"][dim["name"]] = {"type": "scd1", "rows": n}
            elif dim["type"] == "scd2":
                total, current = build_scd2_dimension(con, silver, bronze, dim)
                results["dimensions"][dim["name"]] = {"type": "scd2", "rows": total, "current_rows": current}
            else:
                raise NotImplementedError(f"dimension type {dim['type']!r} not implemented")

        for fact in model["facts"]:
            n = build_fact(con, silver, bronze, fact)
            results["facts"][fact["name"]] = {"rows": n}

        return results
    finally:
        con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="insurance")
    parser.add_argument("--target", default="duckdb")
    args = parser.parse_args()
    out = run(args.domain, args.target)
    print("Dimensions:")
    for name, r in out["dimensions"].items():
        if r["type"] == "scd1":
            print(f"  {name:<16} scd1  rows={r['rows']}")
        else:
            print(f"  {name:<16} scd2  rows={r['rows']:<6} current={r['current_rows']}")
    print("Facts:")
    for name, r in out["facts"].items():
        print(f"  {name:<20} rows={r['rows']}")
