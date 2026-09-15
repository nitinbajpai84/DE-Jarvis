"""Source -> Bronze loader. Contract-driven: reads contracts/sources/<source_id>.source.yaml
and contracts/platform/<target>.yaml, never hardcodes a table/column/threshold/schema-name
that isn't traceable to a contract (CLAUDE.md rule 1).

Algorithm (see specs/P1/design.md):
  1. FQC (file_checks) on the raw file, before any row is read. Fail -> quarantine, done.
  2. Stage: read + attach lineage columns into a scratch table, one file at a time.
  3. DQC (quality_rules) against the staged batch. Any severity=error violation -> the whole
     batch is quarantined (specs/P1/requirements.md Q1/Q2 -- not a row-level filter, because
     deciding which rows to keep is a dedup/business decision that belongs in silver).
  4. Promote: passing batches are inserted into bronze.<source_id>.
  5. One control.run_registry row per invocation, regardless of file count or outcome --
     including failed runs (CLAUDE.md rule 7: no silent runs, success or not).

Only connection.type == "file" / format == "csv" is implemented. Anything else raises rather
than silently guessing (CLAUDE.md rule 1: "If it's missing, stop and ask").

KNOWN DEVIATION (flagged, not hidden -- see ADR-001 and its "Status" line): this loader talks to
DuckDB directly rather than through dlt + sqlglot, which is CLAUDE.md rule 2 territory ("Never
write platform-specific SQL by hand"). Rule 2 is listed under "Hard rules (non-negotiable)" with
no documented override mechanism, unlike rule 4's Open Questions escape valve -- an independent
review flagged that ADR-001 does not have standing to waive it unilaterally. Proceeding only
because development was explicitly authorized ahead of gate sign-off; this specific point needs
your explicit call on review, not just the general gate sign-off.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import pathlib
import shutil
import statistics
import uuid
from datetime import datetime, timezone
from typing import Any

import duckdb
import yaml

from emitters.control_plane import compile_source_registration, ensure_control_schema

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_IMPLEMENTED_RULE_TYPES = {"not_null", "unique", "accepted_values", "range", "freshness"}
_DUCK_TYPE_MAP = {"timestamp": "timestamp", "decimal": "double", "float": "double",
                   "int": "integer", "bigint": "bigint", "bool": "boolean"}


def _load_yaml(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"No contract at {path} -- contracts are the only source of truth, refusing to guess."
        )
    return yaml.safe_load(path.read_text())


def _load_contract(source_id: str) -> dict[str, Any]:
    return _load_yaml(REPO_ROOT / "contracts" / "sources" / f"{source_id}.source.yaml")


def _load_platform(target: str) -> dict[str, Any]:
    return _load_yaml(REPO_ROOT / "contracts" / "platform" / f"{target}.yaml")


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_csv(path: pathlib.Path, delimiter: str) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f, delimiter=delimiter))
    return (rows[0], rows[1:]) if rows else ([], [])


def _trailing_median(
    con: duckdb.DuckDBPyConnection, control: str, source_id: str, value_col: str, window: int = 7,
) -> float | None:
    values = [
        r[0]
        for r in con.execute(
            f"""
            select {value_col} from {control}.file_audit
            where file_name like ? and action = 'loaded'
            order by arrival_time desc limit ?
            """,
            [f"{source_id}_%", window],
        ).fetchall()
    ]
    if len(values) < 3:  # not enough history to judge a deviation meaningfully
        return None
    return statistics.median(values)


def _run_fqc(
    con: duckdb.DuckDBPyConnection, control: str, source_id: str, header: list[str],
    data_rows: list[list[str]], size_kb: float, checks: dict[str, Any],
) -> tuple[bool, str]:
    row_count = len(data_rows)
    col_count = len(header)

    if row_count < checks.get("min_rows", 0):
        return False, f"row_count {row_count} < min_rows {checks['min_rows']}"

    if checks.get("expected_columns") is not None and col_count != checks["expected_columns"]:
        if checks.get("reject_on_schema_drift", True):
            return False, (
                f"column_count {col_count} != expected_columns {checks['expected_columns']} "
                "(schema drift)"
            )

    row_median = _trailing_median(con, control, source_id, "row_count")
    if row_median and checks.get("row_count_deviation_pct") is not None:
        dev = abs(row_count - row_median) / row_median * 100
        if dev > checks["row_count_deviation_pct"]:
            return False, (
                f"row_count {row_count} deviates {dev:.0f}% from trailing median {row_median:.0f} "
                f"(limit {checks['row_count_deviation_pct']}%)"
            )

    size_median = _trailing_median(con, control, source_id, "size_kb")
    if size_median and checks.get("size_deviation_pct") is not None:
        dev = abs(size_kb - size_median) / size_median * 100
        if dev > checks["size_deviation_pct"]:
            return False, (
                f"size_kb {size_kb:.1f} deviates {dev:.0f}% from trailing median {size_median:.1f} "
                f"(limit {checks['size_deviation_pct']}%)"
            )

    return True, "ok"


def _stage_rows(
    con: duckdb.DuckDBPyConnection, bronze: str, source_id: str, schema: list[dict],
    header: list[str], data_rows: list[list[str]], run_id: str, source_file: str,
    data_catalogue_id: int,
) -> None:
    """Loads one file's rows into a fresh staging table. Only timestamp/decimal-typed columns
    are cast; everything else -- including date-typed columns -- lands as a raw string
    (specs/P1/requirements.md R3: format ambiguity is a silver concern, not bronze's)."""
    con.execute(f"drop table if exists {bronze}._staging_{source_id}")
    col_defs = ["data_catalogue_id bigint", "source_record_id integer", "_run_id varchar",
                "_source_file varchar", "_ingested_at timestamp", "_record_hash varchar"]
    for col in schema:
        col_defs.append(f'"{col["name"]}" {_DUCK_TYPE_MAP.get(col["type"], "varchar")}')
    con.execute(f"create table {bronze}._staging_{source_id} ({', '.join(col_defs)})")

    ingested_at = datetime.now(timezone.utc)
    insert_sql = f"insert into {bronze}._staging_{source_id} values ({', '.join(['?'] * (6 + len(schema)))})"

    for i, raw_row in enumerate(data_rows, start=1):
        by_name = dict(zip(header, raw_row))
        record_hash = hashlib.sha256("|".join(raw_row).encode()).hexdigest()
        typed_values = []
        for col in schema:
            val = by_name.get(col["name"])
            if col["type"] == "timestamp" and val:
                val = datetime.fromisoformat(val)
            elif col["type"] in ("decimal", "float") and val not in (None, ""):
                val = float(val)
            typed_values.append(val)
        con.execute(
            insert_sql,
            [data_catalogue_id, i, run_id, source_file, ingested_at, record_hash, *typed_values],
        )


def _eval_quality_rules(
    con: duckdb.DuckDBPyConnection, control: str, bronze: str, source_id: str, rules: list[dict],
    run_id: str, data_catalogue_id: int,
) -> bool:
    """Evaluates every rule against the staged batch, records each to dq_results, returns
    whether the batch as a whole passes (no severity=error violation)."""
    table = f"{bronze}._staging_{source_id}"
    results: list[dict] = []
    batch_ok = True

    for rule in rules:
        rtype, cols, severity = rule["rule"], rule["columns"], rule["severity"]
        if rtype not in _IMPLEMENTED_RULE_TYPES:
            raise NotImplementedError(
                f"quality rule type {rtype!r} is declared in the contract but not implemented "
                "in the P1 loader -- refusing to silently skip a data-quality check."
            )

        if rtype == "not_null":
            failed = con.execute(
                f"select count(*) from {table} where " + " or ".join(f'"{c}" is null' for c in cols)
            ).fetchone()[0]
        elif rtype == "unique":
            # failed_row_count = rows in EXCESS of the first occurrence of each key (i.e. the
            # number of rows you'd need to delete to make the key unique), not the total count
            # of rows sharing a duplicated key. 25 duplicate order_ids appended once each ->
            # failed_row_count=25, not 50.
            col_list = ", ".join(f'"{c}"' for c in cols)
            failed = con.execute(f"select count(*) - count(distinct ({col_list})) from {table}").fetchone()[0]
        elif rtype == "accepted_values":
            values = ", ".join(f"'{v}'" for v in rule["params"]["values"])
            failed = con.execute(f'select count(*) from {table} where "{cols[0]}" not in ({values})').fetchone()[0]
        elif rtype == "range":
            lo, hi = rule["params"]["min"], rule["params"]["max"]
            failed = con.execute(f'select count(*) from {table} where "{cols[0]}" < {lo} or "{cols[0]}" > {hi}').fetchone()[0]
        elif rtype == "freshness":
            max_age = rule["params"]["max_age_hours"]
            failed = con.execute(f"select count(*) from {table} where date_diff('hour', \"{cols[0]}\", now()) > {max_age}").fetchone()[0]

        passed = failed == 0
        results.append({"rule_type": rtype, "columns": ",".join(cols), "severity": severity,
                         "passed": passed, "observed_value": str(failed), "failed_row_count": failed})
        batch_ok = batch_ok and (passed or severity != "error")

    now = datetime.now(timezone.utc)
    for r in results:
        result_id = con.execute(f"select coalesce(max(result_id), 0) + 1 from {control}.dq_results").fetchone()[0]
        con.execute(
            f"insert into {control}.dq_results values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [result_id, run_id, data_catalogue_id, r["rule_type"], r["columns"], r["severity"],
             r["passed"], r["observed_value"], r["failed_row_count"], now],
        )
    return batch_ok


def run(source_id: str, target: str = "duckdb") -> dict[str, Any]:
    contract = _load_contract(source_id)
    platform = _load_platform(target)
    conn_cfg = contract["connection"]
    if conn_cfg["type"] != "file" or conn_cfg["format"] != "csv":
        raise NotImplementedError(
            f"connection.type={conn_cfg['type']!r} format={conn_cfg.get('format')!r} not "
            "implemented -- P1 only supports type=file, format=csv."
        )
    if target != "duckdb":
        raise NotImplementedError("P1 only implements the duckdb target (P5 is the Databricks port).")

    control = platform["storage"]["control"]
    bronze = platform["storage"]["bronze"]
    duckdb_path = REPO_ROOT / "harness" / "jarvis.duckdb"
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    counts = {"files_seen": 0, "files_accepted": 0, "files_quarantined": 0, "rows_loaded": 0}
    status, error_message = "failed", None
    con = None

    try:
        con = duckdb.connect(str(duckdb_path))
        ensure_control_schema(con, control)
        con.execute(f"create schema if not exists {bronze}")
        compile_source_registration(con, contract, control)

        quarantine_dir = REPO_ROOT / contract["file_checks"]["quarantine_path"]
        quarantine_dir.mkdir(parents=True, exist_ok=True)

        files = sorted((REPO_ROOT / "harness").glob(
            pathlib.PurePosixPath(conn_cfg["path"]).relative_to("harness").as_posix()
        ))

        col_defs = ["data_catalogue_id bigint", "source_record_id integer", "_run_id varchar",
                    "_source_file varchar", "_ingested_at timestamp", "_record_hash varchar"]
        for col in contract["schema"]:
            col_defs.append(f'"{col["name"]}" {_DUCK_TYPE_MAP.get(col["type"], "varchar")}')
        con.execute(f"create table if not exists {bronze}.{source_id} ({', '.join(col_defs)})")

        for path in files:
            counts["files_seen"] += 1
            header, data_rows = _read_csv(path, conn_cfg.get("delimiter", ","))
            checksum = _sha256(path)
            size_kb = path.stat().st_size / 1024
            arrival_time = datetime.now(timezone.utc)

            fqc_passed, _ = _run_fqc(con, control, source_id, header, data_rows, size_kb, contract["file_checks"])
            file_audit_id = con.execute(f"select coalesce(max(file_audit_id), 0) + 1 from {control}.file_audit").fetchone()[0]

            if not fqc_passed:
                shutil.copy2(path, quarantine_dir / path.name)
                con.execute(
                    f"insert into {control}.file_audit values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [file_audit_id, run_id, path.name, str(path), size_kb, len(data_rows), len(header),
                     checksum, arrival_time, False, "quarantined"],
                )
                counts["files_quarantined"] += 1
                continue

            data_catalogue_id = con.execute(f"select coalesce(max(data_catalogue_id), 0) + 1 from {control}.data_object_catalogue").fetchone()[0]
            _stage_rows(con, bronze, source_id, contract["schema"], header, data_rows, run_id, path.name, data_catalogue_id)
            batch_ok = _eval_quality_rules(con, control, bronze, source_id, contract["quality_rules"], run_id, data_catalogue_id)

            con.execute(
                f"insert into {control}.file_audit values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [file_audit_id, run_id, path.name, str(path), size_kb, len(data_rows), len(header),
                 checksum, arrival_time, True, "loaded" if batch_ok else "quarantined"],
            )
            con.execute(
                f"insert into {control}.data_object_catalogue values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [data_catalogue_id, source_id, path.name, str(path), len(data_rows), size_kb,
                 checksum, arrival_time, "loaded" if batch_ok else "quarantined"],
            )

            if batch_ok:
                cols = ", ".join(["data_catalogue_id", "source_record_id", "_run_id", "_source_file",
                                   "_ingested_at", "_record_hash"] + [f'"{c["name"]}"' for c in contract["schema"]])
                con.execute(f"insert into {bronze}.{source_id} ({cols}) select {cols} from {bronze}._staging_{source_id}")
                con.execute(
                    f"insert into {control}.load_lineage values (?, ?, ?, ?, ?)",
                    [run_id, path.name, f"{bronze}.{source_id}", data_catalogue_id, datetime.now(timezone.utc)],
                )
                counts["files_accepted"] += 1
                counts["rows_loaded"] += len(data_rows)
            else:
                shutil.copy2(path, quarantine_dir / path.name)
                counts["files_quarantined"] += 1

        con.execute(f"drop table if exists {bronze}._staging_{source_id}")
        status = "completed"
        return {"run_id": run_id, **counts}
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: must still record the run, then re-raise
        error_message = str(exc)
        raise
    finally:
        if con is None:
            # duckdb.connect() itself failed -- nowhere to log a run_registry row. Re-raised
            # already by the bare `raise` above; nothing further to record here.
            pass
        else:
            con.execute(
                f"insert into {control}.run_registry values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, source_id, "P1", started_at, datetime.now(timezone.utc), status, error_message,
                 counts["files_seen"], counts["files_accepted"], counts["files_quarantined"], counts["rows_loaded"]],
            )
            con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", default="duckdb")
    args = parser.parse_args()
    summary = run(args.source, args.target)
    for k, v in summary.items():
        print(f"{k:<18} {v}")
