"""Source -> Bronze loader. Contract-driven: reads contracts/sources/<source_id>.source.yaml
and contracts/platform/<target>.yaml, never hardcodes a table/column/threshold/schema-name
that isn't traceable to a contract (CLAUDE.md rule 1).

Every connection.type normalizes to the same Batch shape (name, header, rows, size_kb,
checksum) before the shared FQC -> stage -> DQC -> promote pipeline runs (see specs/P1/design.md
for the pipeline; connectors below are what's new past P1):
  - file:          one batch per matching CSV file (P1's original scope)
  - database:      one batch = one query result (only dialect=databricks implemented, via the
                    same credentials already in .env for the dbt Databricks target)
  - api:           one batch = one paginated pull (only data.gov.sg's v2 response envelope
                    shape is implemented -- a different API needs its own envelope parsing,
                    not silently assumed to match)
  - unstructured:  one batch = every matching document, one row per document, fields from
                    contract["bronze_schema"] rather than contract["schema"]

  1. FQC (file_checks) on the raw batch, before any row is staged. Fail -> quarantine, done.
     Unstructured sources use file-count/size checks instead of row/column checks.
  2. Stage: attach lineage columns into a scratch table, one batch at a time.
  3. DQC (quality_rules) against the staged batch. Any severity=error violation -> the whole
     batch is quarantined (specs/P1/requirements.md Q1/Q2).
  4. Promote: passing batches are inserted into bronze.<source_id>.
  5. One control.run_registry row per invocation, regardless of outcome (CLAUDE.md rule 7).

Targets both duckdb and databricks through one set of SQL strings, authored once in duckdb
dialect -- emitters/sql_dialect.py transpiles per target (ADR-001's resolution; see that file's
docstring for what was verified before trusting it, and its own docstring for what still isn't
a clean transpile and was restructured instead of dialect-branched).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import pathlib
import shutil
import statistics
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import yaml

from emitters.control_plane import compile_source_registration, ensure_control_schema
from emitters.sql_dialect import SqlConnection, connect as sql_connect

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_IMPLEMENTED_RULE_TYPES = {"not_null", "unique", "accepted_values", "range", "freshness"}
_DUCK_TYPE_MAP = {"timestamp": "timestamp", "decimal": "double", "float": "double",
                   "int": "integer", "bigint": "bigint", "bool": "boolean"}


@dataclass
class Batch:
    name: str            # lineage/file_audit label, e.g. "orders_20260901.csv" or "nyctaxi_trips_extract_20260915"
    location: str         # path, table name, or endpoint URL -- for audit, not re-fetched
    header: list[str]
    rows: list[list[Any]]
    size_kb: float
    checksum: str


def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


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


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(path: pathlib.Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _landing_glob(pattern: str) -> list[pathlib.Path]:
    return sorted(
        p for p in (REPO_ROOT / "harness").glob(
            pathlib.PurePosixPath(pattern).relative_to("harness").as_posix()
        )
        if p.is_file()
    )


def _read_csv(path: pathlib.Path, delimiter: str) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f, delimiter=delimiter))
    return (rows[0], rows[1:]) if rows else ([], [])


# --------------------------------------------------------------------------- connectors

def _extract_file_batches(contract: dict) -> list[Batch]:
    conn = contract["connection"]
    batches = []
    for path in _landing_glob(conn["path"]):
        header, rows = _read_csv(path, conn.get("delimiter", ","))
        batches.append(Batch(path.name, str(path), header, rows,
                              path.stat().st_size / 1024, _sha256_file(path)))
    return batches


def _extract_database_batches(contract: dict) -> list[Batch]:
    conn = contract["connection"]
    if conn["dialect"] != "databricks":
        raise NotImplementedError(f"database dialect {conn['dialect']!r} not implemented -- only databricks is.")
    _load_dotenv()
    from databricks import sql  # imported lazily -- only needed for this connector

    dbconn = sql.connect(
        server_hostname=os.environ["DATABRICKS_HOST"].replace("https://", ""),
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )
    try:
        cur = dbconn.cursor()
        cur.execute(f"select * from {conn['table']}")
        header = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
    finally:
        dbconn.close()

    source_id = contract["source_id"]
    name = f"{source_id}_extract_{datetime.now(timezone.utc):%Y%m%d}"
    payload = json.dumps(rows, default=str).encode()
    return [Batch(name, conn["table"], header, rows, len(payload) / 1024, _sha256_bytes(payload))]


def _extract_api_batches(contract: dict) -> list[Batch]:
    """Only data.gov.sg's v2 {"data": {"rows": [...]}} envelope is implemented. A different
    API needs its own parsing -- this deliberately does not pretend to be a generic client."""
    conn = contract["connection"]
    schema_cols = [c["name"] for c in contract["schema"]]
    url = conn["endpoint"] + "?limit=200"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (jarvis-data-platform)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    api_rows = payload["data"]["rows"]
    rows = [[d.get(c) for c in schema_cols] for d in api_rows]

    source_id = contract["source_id"]
    name = f"{source_id}_extract_{datetime.now(timezone.utc):%Y%m%d}"
    raw = json.dumps(api_rows).encode()
    return [Batch(name, conn["endpoint"], schema_cols, rows, len(raw) / 1024, _sha256_bytes(raw))]


def _extract_unstructured_batches(contract: dict) -> tuple[list[pathlib.Path], Batch]:
    """Returns the raw file list too -- FQC for unstructured checks per-file (size, zero-byte),
    not per-row/column, so the caller needs the files independently of the aggregated Batch."""
    conn = contract["connection"]
    files = _landing_glob(conn["path"])
    header = [c["name"] for c in contract["bronze_schema"]]
    rows = []
    for i, f in enumerate(files, start=1):
        text = f.read_text(errors="replace")
        mime, _ = mimetypes.guess_type(f.name)
        values = {
            "document_id": f"DOC{i:06d}", "file_path": str(f), "mime_type": mime or "text/plain",
            "page_count": 1, "char_count": len(text), "extracted_text": text,
            "extraction_method": "native", "extraction_confidence": 1.0,
        }
        rows.append([values.get(c) for c in header])

    source_id = contract["source_id"]
    name = f"{source_id}_batch_{datetime.now(timezone.utc):%Y%m%d}"
    raw = "\x1e".join(r[header.index("extracted_text")] or "" for r in rows).encode()
    total_kb = sum(f.stat().st_size for f in files) / 1024
    return files, Batch(name, conn["path"], header, rows, total_kb, _sha256_bytes(raw))


def _extract_batches(contract: dict) -> tuple[list[pathlib.Path] | None, list[Batch]]:
    ctype = contract["connection"]["type"]
    if ctype == "file":
        return None, _extract_file_batches(contract)
    if ctype == "database":
        return None, _extract_database_batches(contract)
    if ctype == "api":
        return None, _extract_api_batches(contract)
    if ctype == "unstructured":
        files, batch = _extract_unstructured_batches(contract)
        return files, [batch]
    raise NotImplementedError(f"connection.type={ctype!r} not implemented.")


# --------------------------------------------------------------------------- FQC

def _trailing_median(
    con: SqlConnection, control: str, source_id: str, value_col: str, window: int = 7,
) -> float | None:
    values = [
        r[0] for r in con.execute(
            f"select {value_col} from {control}.file_audit where file_name like ? and action = 'loaded' "
            "order by arrival_time desc limit ?",
            [f"{source_id}_%", window],
        ).fetchall()
    ]
    return statistics.median(values) if len(values) >= 3 else None


def _run_fqc(
    con: SqlConnection, control: str, source_id: str, batch: Batch, checks: dict[str, Any],
) -> tuple[bool, str]:
    row_count, col_count = len(batch.rows), len(batch.header)

    if row_count < checks.get("min_rows", 0):
        return False, f"row_count {row_count} < min_rows {checks['min_rows']}"
    if checks.get("expected_columns") is not None and col_count != checks["expected_columns"]:
        if checks.get("reject_on_schema_drift", True):
            return False, f"column_count {col_count} != expected_columns {checks['expected_columns']} (schema drift)"

    row_median = _trailing_median(con, control, source_id, "row_count")
    if row_median and checks.get("row_count_deviation_pct") is not None:
        dev = abs(row_count - row_median) / row_median * 100
        if dev > checks["row_count_deviation_pct"]:
            return False, f"row_count {row_count} deviates {dev:.0f}% from trailing median {row_median:.0f}"

    size_median = _trailing_median(con, control, source_id, "size_kb")
    if size_median and checks.get("size_deviation_pct") is not None:
        dev = abs(batch.size_kb - size_median) / size_median * 100
        if dev > checks["size_deviation_pct"]:
            return False, f"size_kb {batch.size_kb:.1f} deviates {dev:.0f}% from trailing median {size_median:.1f}"

    return True, "ok"


def _run_fqc_unstructured(files: list[pathlib.Path], checks: dict[str, Any]) -> tuple[bool, str]:
    if len(files) < checks.get("min_files", 0):
        return False, f"{len(files)} files < min_files {checks['min_files']}"
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        if checks.get("max_file_mb") and size_mb > checks["max_file_mb"]:
            return False, f"{f.name} is {size_mb:.1f}MB > max_file_mb {checks['max_file_mb']}"
        if checks.get("reject_zero_byte") and f.stat().st_size == 0:
            return False, f"{f.name} is zero bytes"
    return True, "ok"


# --------------------------------------------------------------------------- stage / DQC

def _stage_rows(
    con: SqlConnection, bronze: str, source_id: str, schema: list[dict], batch: Batch,
    run_id: str, data_catalogue_id: int,
) -> None:
    """Loads one batch's rows into a fresh staging table. Timestamp/decimal-typed columns are
    cast only if the value is a string (a CSV cell); values already typed at the source (e.g. a
    native datetime from the Databricks driver) pass through unchanged. Everything else --
    including date-typed columns -- lands as a raw string (specs/P1/requirements.md R3)."""
    con.execute(f"drop table if exists {bronze}._staging_{source_id}")
    col_defs = ["data_catalogue_id bigint", "source_record_id integer", "_run_id varchar",
                "_source_file varchar", "_ingested_at timestamp", "_record_hash varchar"]
    for col in schema:
        col_defs.append(f'"{col["name"]}" {_DUCK_TYPE_MAP.get(col["type"], "varchar")}')
    con.execute(f"create table {bronze}._staging_{source_id} ({', '.join(col_defs)})")

    ingested_at = datetime.now(timezone.utc)
    insert_sql = f"insert into {bronze}._staging_{source_id} values ({', '.join(['?'] * (6 + len(schema)))})"

    all_rows = []
    for i, raw_row in enumerate(batch.rows, start=1):
        by_name = dict(zip(batch.header, raw_row))
        record_hash = hashlib.sha256("|".join(str(v) for v in raw_row).encode()).hexdigest()
        typed_values = []
        for col in schema:
            val = by_name.get(col["name"])
            if col["type"] == "timestamp" and isinstance(val, str) and val:
                val = datetime.fromisoformat(val)
            elif col["type"] in ("decimal", "float") and isinstance(val, str) and val != "":
                val = float(val)
            typed_values.append(val)
        all_rows.append([data_catalogue_id, i, run_id, batch.name, ingested_at, record_hash, *typed_values])

    _BATCH_SIZE = 1000
    for chunk_start in range(0, len(all_rows), _BATCH_SIZE):
        con.executemany(insert_sql, all_rows[chunk_start:chunk_start + _BATCH_SIZE])


def _eval_quality_rules(
    con: SqlConnection, control: str, bronze: str, source_id: str, rules: list[dict],
    run_id: str, data_catalogue_id: int,
) -> bool:
    table = f"{bronze}._staging_{source_id}"
    results: list[dict] = []
    batch_ok = True

    for rule in rules:
        rtype, cols, severity = rule["rule"], rule["columns"], rule["severity"]
        if rtype not in _IMPLEMENTED_RULE_TYPES:
            raise NotImplementedError(
                f"quality rule type {rtype!r} is declared in the contract but not implemented "
                "in the loader -- refusing to silently skip a data-quality check."
            )

        if rtype == "not_null":
            failed = con.execute(
                f"select count(*) from {table} where " + " or ".join(f'"{c}" is null' for c in cols)
            ).fetchone()[0]
        elif rtype == "unique":
            col_list = ", ".join(f'"{c}"' for c in cols)
            failed = con.execute(f"select count(*) - count(distinct ({col_list})) from {table}").fetchone()[0]
        elif rtype == "accepted_values":
            values = ", ".join(f"'{v}'" for v in rule["params"]["values"])
            failed = con.execute(f'select count(*) from {table} where "{cols[0]}" not in ({values})').fetchone()[0]
        elif rtype == "range":
            # min/max are each optional -- a rule may bound only one side (e.g. char_count
            # has no meaningful upper bound, only a "not too short" floor).
            conds = []
            if "min" in rule["params"]:
                conds.append(f'"{cols[0]}" < {rule["params"]["min"]}')
            if "max" in rule["params"]:
                conds.append(f'"{cols[0]}" > {rule["params"]["max"]}')
            if not conds:
                raise ValueError(f"range rule on {cols[0]!r} has neither min nor max in params")
            failed = con.execute(f"select count(*) from {table} where " + " or ".join(conds)).fetchone()[0]
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


def _quarantine_batch(batch: Batch, files: list[pathlib.Path] | None, quarantine_dir: pathlib.Path) -> None:
    if files is not None:
        for f in files:
            shutil.copy2(f, quarantine_dir / f.name)
        return
    # database/api batches have no physical file -- dump a snapshot instead
    snapshot = [dict(zip(batch.header, row)) for row in batch.rows]
    (quarantine_dir / f"{batch.name}.json").write_text(json.dumps(snapshot, default=str, indent=2))


# --------------------------------------------------------------------------- run

def run(source_id: str, target: str = "duckdb") -> dict[str, Any]:
    contract = _load_contract(source_id)
    platform = _load_platform(target)

    control = platform["storage"]["control"]
    bronze = platform["storage"]["bronze"]

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    counts = {"files_seen": 0, "files_accepted": 0, "files_quarantined": 0, "rows_loaded": 0}
    status, error_message = "failed", None
    con: SqlConnection | None = None
    is_unstructured = contract["connection"]["type"] == "unstructured"
    schema = contract["bronze_schema"] if is_unstructured else contract["schema"]

    try:
        con = sql_connect(target, platform)
        ensure_control_schema(con, control)
        con.execute(f"create schema if not exists {bronze}")
        compile_source_registration(con, contract, control)

        quarantine_dir = REPO_ROOT / contract["file_checks"]["quarantine_path"]
        quarantine_dir.mkdir(parents=True, exist_ok=True)

        files, batches = _extract_batches(contract)

        col_defs = ["data_catalogue_id bigint", "source_record_id integer", "_run_id varchar",
                    "_source_file varchar", "_ingested_at timestamp", "_record_hash varchar"]
        for col in schema:
            col_defs.append(f'"{col["name"]}" {_DUCK_TYPE_MAP.get(col["type"], "varchar")}')
        con.execute(f"create table if not exists {bronze}.{source_id} ({', '.join(col_defs)})")

        for batch in batches:
            counts["files_seen"] += 1
            arrival_time = datetime.now(timezone.utc)

            if is_unstructured:
                fqc_passed, _ = _run_fqc_unstructured(files, contract["file_checks"])
            else:
                fqc_passed, _ = _run_fqc(con, control, source_id, batch, contract["file_checks"])

            file_audit_id = con.execute(f"select coalesce(max(file_audit_id), 0) + 1 from {control}.file_audit").fetchone()[0]

            if not fqc_passed:
                _quarantine_batch(batch, files, quarantine_dir)
                con.execute(
                    f"insert into {control}.file_audit values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [file_audit_id, run_id, batch.name, batch.location, batch.size_kb, len(batch.rows),
                     len(batch.header), batch.checksum, arrival_time, False, "quarantined"],
                )
                counts["files_quarantined"] += 1
                continue

            data_catalogue_id = con.execute(f"select coalesce(max(data_catalogue_id), 0) + 1 from {control}.data_object_catalogue").fetchone()[0]
            _stage_rows(con, bronze, source_id, schema, batch, run_id, data_catalogue_id)
            batch_ok = _eval_quality_rules(con, control, bronze, source_id, contract.get("quality_rules", []), run_id, data_catalogue_id)

            con.execute(
                f"insert into {control}.file_audit values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [file_audit_id, run_id, batch.name, batch.location, batch.size_kb, len(batch.rows),
                 len(batch.header), batch.checksum, arrival_time, True, "loaded" if batch_ok else "quarantined"],
            )
            con.execute(
                f"insert into {control}.data_object_catalogue values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [data_catalogue_id, source_id, batch.name, batch.location, len(batch.rows), batch.size_kb,
                 batch.checksum, arrival_time, "loaded" if batch_ok else "quarantined"],
            )

            if batch_ok:
                cols = ", ".join(["data_catalogue_id", "source_record_id", "_run_id", "_source_file",
                                   "_ingested_at", "_record_hash"] + [f'"{c["name"]}"' for c in schema])
                con.execute(f"insert into {bronze}.{source_id} ({cols}) select {cols} from {bronze}._staging_{source_id}")
                con.execute(
                    f"insert into {control}.load_lineage values (?, ?, ?, ?, ?)",
                    [run_id, batch.name, f"{bronze}.{source_id}", data_catalogue_id, datetime.now(timezone.utc)],
                )
                counts["files_accepted"] += 1
                counts["rows_loaded"] += len(batch.rows)
            else:
                _quarantine_batch(batch, files, quarantine_dir)
                counts["files_quarantined"] += 1

        con.execute(f"drop table if exists {bronze}._staging_{source_id}")
        status = "completed"
        return {"run_id": run_id, **counts}
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: must still record the run, then re-raise
        error_message = str(exc)
        raise
    finally:
        if con is None:
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
