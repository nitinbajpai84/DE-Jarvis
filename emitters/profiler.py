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
            # No query params forced onto the caller's endpoint: a real public API (confirmed
            # against data.gov.sg's own v2 endpoints) can 400 on an unrecognized `?limit=`, so a
            # generic reachability check has to hit the URL exactly as given, not assume every
            # API accepts a limit param the way bronze_loader's one hardcoded envelope does.
            import urllib.request
            req = urllib.request.Request(connection["endpoint"],
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


def _record_candidates(payload: Any, path: str = "$", depth: int = 0,
                        out: list[tuple[str, list[dict]]] | None = None) -> list[tuple[str, list[dict]]]:
    """EVERY list-of-objects in the payload, each with the JSON path it was found at.

    Collecting all candidates rather than returning the first match is the point. Real APIs
    routinely wrap the actual records one level down inside a single-element envelope: HDB's
    Carpark Availability feed (data.gov.sg d_ca933a6...) returns
    {"items":[{"timestamp":..., "carpark_data":[ ...~2000 carparks... ]}]}, so "first
    list-of-objects" finds `items` -- one row, two columns, none of the actual data. Confirmed
    live against that endpoint before this was changed, not hypothesised. Depth-capped so a
    pathological payload can't recurse forever."""
    if out is None:
        out = []
    if depth > 5:
        return out
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            out.append((path, payload))
            # Descend into the first element too: a single-row envelope may be hiding the
            # real records underneath it.
            _record_candidates(payload[0], f"{path}[0]", depth + 1, out)
        return out
    if isinstance(payload, dict):
        for k, v in payload.items():
            _record_candidates(v, f"{path}.{k}", depth + 1, out)
    return out


def _find_records(payload: Any) -> tuple[list[dict], str] | None:
    """The best candidate list of records, and where it was found. "Best" is simply the longest
    -- between a 1-row envelope and the 2,000 rows nested inside it, the 2,000 are what someone
    exploring this source came to look at. Ties break toward the shallowest path, so a plain
    {"records":[...]} response never gets outsmarted by something nested inside it.

    The chosen path is returned (and surfaced in the profile) rather than applied silently:
    picking between candidates is a judgement call, so the human can see which one was taken."""
    candidates = _record_candidates(payload)
    if not candidates:
        return None
    best = max(candidates, key=lambda c: (len(c[1]), -c[0].count(".") - c[0].count("[")))
    return best[1], best[0]


def _sample_api(connection: dict, limit: int) -> tuple[list[str], list[list[Any]], int | None]:
    """Fetches the endpoint exactly as configured (no forced query params -- see
    test_connection's own note) and extracts whatever list of records is actually in the
    response, rather than requiring one bespoke envelope. This is deliberately broader than what
    bronze_loader.py can currently ingest (documented there as "does not pretend to be a generic
    client") -- profiling exists to explore a source BEFORE a contract or an ingestion path
    exists for it, so a real API this platform can't load yet should still be explorable."""
    import urllib.request
    req = urllib.request.Request(connection["endpoint"], headers={"User-Agent": "Mozilla/5.0 (jarvis-data-platform)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    found = _find_records(payload)
    if found is None:
        raise ValueError(
            "could not find a list of records anywhere in the response -- this API's shape "
            "isn't one this profiler recognizes yet (scanned the whole payload, to 5 levels "
            "deep, for any list of JSON objects)"
        )
    api_rows, records_path = found
    cols = sorted({k for r in api_rows for k in r.keys()})
    rows = [[r.get(c) for c in cols] for r in api_rows[:limit]]
    _LAST_API_RECORDS_PATH.append(records_path)
    return cols, rows, len(api_rows)


# _sample_api is called through the generic _SAMPLERS dispatch in profile_source(), which has no
# place for a sampler to return an extra field -- this one-slot stash carries the chosen records
# path back out so the profile can report it. Reset per call in profile_source().
_LAST_API_RECORDS_PATH: list[str] = []


def _extract_document_content(text: str) -> dict[str, Any]:
    """A real, cheap-tier Gemini call reads one document and reports back a summary and the
    candidate structured fields it can find -- e.g. an underwriting letter's policy number,
    decision, and premium. File-metadata-only profiling (path/mime/char count) tells you a
    document exists, not what's in it; exploring an unstructured source is supposed to answer
    "what would I even extract a contract around," which needs the content actually read.
    Best-effort: any failure (no API key, a malformed response, a network error) degrades to an
    empty result rather than breaking the whole profile -- this is exploratory enrichment, not a
    load-bearing part of the profile."""
    try:
        import json as _json
        import os
        from langchain.chat_models import init_chat_model

        prompt = (
            "Read this document and respond with ONLY a JSON object (no markdown fences, no "
            "commentary) with exactly two keys: \"summary\" (one sentence describing what kind "
            "of document this is and what it's about) and \"candidate_fields\" (an object of "
            "field_name: value for every distinct, extractable piece of structured data you can "
            "find -- ids, dates, amounts, names, statuses, decisions. Use snake_case keys. If "
            "nothing extractable is present, use an empty object.\n\nDocument:\n" + text[:8000]
        )
        model = init_chat_model("google_genai:gemini-2.5-flash")
        raw = model.invoke(prompt).content.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.index("\n") + 1:] if "\n" in raw else raw
        parsed = _json.loads(raw)
        return {"summary": parsed.get("summary", ""), "candidate_fields": parsed.get("candidate_fields", {})}
    except Exception as exc:  # noqa: BLE001 -- best-effort enrichment, never fails the profile over it
        return {"summary": None, "candidate_fields": {}, "extraction_error": f"{type(exc).__name__}: {exc}"}


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

    _LAST_API_RECORDS_PATH.clear()
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
    if ctype == "api" and _LAST_API_RECORDS_PATH:
        # Which list inside the response these records came from -- visible rather than a silent
        # choice, since a nested envelope means there was more than one candidate.
        report["records_path"] = _LAST_API_RECORDS_PATH[-1]
    if ctype == "unstructured":
        # file_path is column 0 in _sample_unstructured's header -- read each sampled document's
        # own content, not just its metadata, so exploring an unstructured source answers "what's
        # actually in here."
        report["content_extraction"] = [
            {"file_path": r[0], **_extract_document_content(pathlib.Path(r[0]).read_text(errors="replace"))}
            for r in rows
        ]
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
