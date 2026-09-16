"""Step 01 discovery: connect to a real source and describe what's actually there, before any
contract exists for it.

Reuses bronze_loader.py's own connection vocabulary -- a `connection` dict with type
file/database/api/unstructured, the exact same shape a compiled source.yaml carries under its
own `connection:` key -- rather than inventing a second format. A profile produced here and a
real contract later describe the same source the same way; there is no translation step, and no
risk of the two silently drifting.

This module only READS. It never lands anything to bronze, never writes to contracts/, and
(for the database connector) samples with LIMIT rather than pulling a whole table. The output
is one profile per source, persisted to contracts/discovery/<domain>/<source_id>.profile.json --
deliberately not evidence/ (nothing has been decided yet) and deliberately not contracts/sources/
(nothing has been approved yet). It is candidate material for a human to curate into the intake
workbook, the same relationship a rough draft has to what gets signed.

Type inference: nowhere else in this codebase infers a column's type from raw values --
everywhere else, type is a fact declared by an already-approved contract. This is the one place
that has no contract to read yet, so _infer_type() below is new, deliberately conservative (falls
back to "string" rather than guess wrong), and is never used for anything but the profile report.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
import sys
from collections import Counter
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DISCOVERY_DIR = REPO_ROOT / "contracts" / "discovery"

_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_BOOL_VALUES = {"true", "false", "y", "n", "yes", "no", "0", "1"}


def _infer_type(values: list[Any]) -> str:
    """Conservative sniffing over a column's non-null sample values: every value has to agree
    for a type more specific than string, so one stray value falls the column back to string
    rather than mask a real mixed-type problem the human should see at Step 01, not discover
    downstream when bronze's FQC schema check rejects it."""
    texts = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
    if not texts:
        return "unknown"
    if all(_INT_RE.match(t) for t in texts):
        return "integer"
    if all(_FLOAT_RE.match(t) or _INT_RE.match(t) for t in texts):
        return "decimal"
    if all(_TIMESTAMP_RE.match(t) for t in texts):
        return "timestamp"
    if all(_DATE_RE.match(t) for t in texts):
        return "date"
    if all(t.lower() in _BOOL_VALUES for t in texts):
        return "boolean"
    return "string"


def _column_profile(name: str, values: list[Any]) -> dict[str, Any]:
    total = len(values)
    non_null = [v for v in values if v is not None and str(v).strip() != ""]
    null_count = total - len(non_null)
    distinct = list(dict.fromkeys(str(v) for v in non_null))  # order-preserving de-dupe
    return {
        "name": name,
        "inferred_type": _infer_type(non_null),
        "null_count": null_count,
        "null_pct": round(100 * null_count / total, 1) if total else 0.0,
        "distinct_count": len(distinct),
        # A candidate key needs every sampled value both present and unique -- distinct_count
        # alone isn't enough, since a column that's mostly null can look falsely unique.
        "is_candidate_key": bool(non_null) and null_count == 0 and len(distinct) == len(non_null),
        "sample_values": distinct[:5],
    }


# --------------------------------------------------------------------------- reachability

def test_connection(connection: dict) -> dict[str, Any]:
    """Cheap reachability check, no sampling: can this connection be reached at all. Returns
    {ok, detail} rather than raising, so a bad connection config is something the caller can
    show the customer, not a stack trace."""
    ctype = connection.get("type")
    try:
        if ctype == "file":
            files = _glob_files(connection)
            if not files:
                return {"ok": False, "detail": f"no files match {connection.get('path')!r}"}
            return {"ok": True, "detail": f"{len(files)} file(s) match, most recent {files[-1].name}"}

        if ctype == "database":
            if connection.get("dialect") != "databricks":
                return {"ok": False, "detail": f"dialect {connection.get('dialect')!r} not implemented -- only databricks is"}
            con = _databricks_connect()
            try:
                cur = con.cursor()
                cur.execute(f"select 1 from {connection['table']} limit 1")
                cur.fetchall()
                return {"ok": True, "detail": f"reached {connection['table']}"}
            finally:
                con.close()

        if ctype == "api":
            import urllib.request
            req = urllib.request.Request(connection["endpoint"] + "?limit=1",
                                          headers={"User-Agent": "Mozilla/5.0 (jarvis-data-platform)"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.status
            return {"ok": status == 200, "detail": f"HTTP {status}"}

        if ctype == "unstructured":
            files = _glob_files(connection)
            if not files:
                return {"ok": False, "detail": f"no files match {connection.get('path')!r}"}
            return {"ok": True, "detail": f"{len(files)} file(s) match"}

        return {"ok": False, "detail": f"connection.type={ctype!r} not implemented"}
    except Exception as exc:  # noqa: BLE001 -- reachability check reports failure, doesn't raise it
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- sampling per type

def _glob_files(connection: dict) -> list[pathlib.Path]:
    pattern = connection["path"]
    base = REPO_ROOT / "harness"
    rel = pathlib.PurePosixPath(pattern)
    rel = rel.relative_to("harness") if pattern.startswith("harness/") else rel
    return sorted(p for p in base.glob(rel.as_posix()) if p.is_file())


def _databricks_connect():
    import os
    _load_dotenv_once()
    from databricks import sql
    return sql.connect(
        server_hostname=os.environ["DATABRICKS_HOST"].replace("https://", ""),
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )


_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    import os
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    _DOTENV_LOADED = True


def _sample_file(connection: dict, limit: int) -> tuple[list[str], list[list[Any]], int | None]:
    import csv
    files = _glob_files(connection)
    if not files:
        raise FileNotFoundError(f"no files match {connection['path']!r}")
    delimiter = connection.get("delimiter", ",")
    header: list[str] = []
    rows: list[list[Any]] = []
    total = 0
    for f in files:
        with f.open(newline="", encoding=connection.get("encoding", "utf-8")) as fh:
            reader = csv.reader(fh, delimiter=delimiter)
            file_rows = list(reader)
        if not file_rows:
            continue
        if not header:
            header = file_rows[0]
        body = file_rows[1:] if connection.get("header", True) else file_rows
        total += len(body)
        if len(rows) < limit:
            rows.extend(body[: limit - len(rows)])
    return header, rows, total


def _sample_database(connection: dict, limit: int) -> tuple[list[str], list[list[Any]], int | None]:
    con = _databricks_connect()
    try:
        cur = con.cursor()
        cur.execute(f"select * from {connection['table']} limit {int(limit)}")
        header = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        cur.execute(f"select count(*) from {connection['table']}")
        total = cur.fetchone()[0]
        return header, rows, total
    finally:
        con.close()


def _sample_api(connection: dict, limit: int) -> tuple[list[str], list[list[Any]], int | None]:
    """Same v2 {"data": {"rows": [...]}} envelope bronze_loader.py's _extract_api_batches
    assumes -- profiling a shape this platform can't ingest yet would be a false promise."""
    import urllib.request
    url = connection["endpoint"] + f"?limit={int(limit)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (jarvis-data-platform)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    api_rows = payload["data"]["rows"]
    cols = sorted({k for r in api_rows for k in r.keys()})
    rows = [[r.get(c) for c in cols] for r in api_rows[:limit]]
    return cols, rows, payload.get("data", {}).get("total") or len(api_rows)


def _sample_unstructured(connection: dict, limit: int) -> tuple[list[str], list[list[Any]], int | None]:
    import mimetypes
    files = _glob_files(connection)[:limit]
    header = ["file_path", "mime_type", "char_count"]
    rows = []
    for f in files:
        text = f.read_text(errors="replace")
        mime, _ = mimetypes.guess_type(f.name)
        rows.append([str(f), mime or "text/plain", len(text)])
    return header, rows, len(_glob_files(connection))


_SAMPLERS = {"file": _sample_file, "database": _sample_database,
             "api": _sample_api, "unstructured": _sample_unstructured}


# --------------------------------------------------------------------------- public entry point

def profile_source(connection: dict, source_id: str, domain: str, sample_limit: int = 500) -> dict[str, Any]:
    """Connects for real, pulls a sample, and reports a column-level profile: inferred type,
    null rate, distinct count, candidate-key flag, sample values -- the raw material a human (or
    the BA agent) curates into 03_Data_Sources / 04_Attributes rows in the intake workbook.
    Nothing here writes to contracts/ or lands data to bronze."""
    ctype = connection.get("type")
    sampler = _SAMPLERS.get(ctype)
    if sampler is None:
        raise NotImplementedError(f"connection.type={ctype!r} not implemented for profiling")

    header, rows, total_rows = sampler(connection, sample_limit)
    columns = [_column_profile(name, [r[i] if i < len(r) else None for r in rows])
               for i, name in enumerate(header)]
    candidate_keys = [c["name"] for c in columns if c["is_candidate_key"]]

    report = {
        "source_id": source_id, "domain": domain, "connection": connection,
        "sampled_rows": len(rows), "total_rows": total_rows,
        "columns": columns, "candidate_keys": candidate_keys,
        "profiled_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    _write_profile(domain, source_id, report)
    return report


def _write_profile(domain: str, source_id: str, report: dict) -> pathlib.Path:
    out_dir = DISCOVERY_DIR / domain
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{source_id}.profile.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return path


def list_profiles(domain: str) -> list[dict[str, Any]]:
    """Every source profiled so far for this domain, newest-known-facts-only (one file per
    source_id, overwritten on re-profile) -- read by webapp/backend/journey.py and the Step 01
    screen."""
    out_dir = DISCOVERY_DIR / domain
    if not out_dir.exists():
        return []
    profiles = []
    for p in sorted(out_dir.glob("*.profile.json")):
        try:
            profiles.append(json.loads(p.read_text()))
        except Exception:  # noqa: BLE001 -- a corrupt profile file shouldn't blank the whole list
            continue
    return profiles
