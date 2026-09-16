"""Read-side queries for the Jarvis Control Room API -- every function here is a thin
wrapper over control.run_registry (and friends), reusing emitters/sql_dialect.py's
SqlConnection so the same query works against duckdb or a live Databricks warehouse.
No business logic lives here that doesn't already live in emitters/ or harness/ --
this module composes existing facts into API-shaped responses, it doesn't derive new ones.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402
from harness.daily_digest import gather as gather_digest  # noqa: E402

PHASES = ["bronze", "silver", "gold"]
DEFAULT_DOMAIN = "insurance"  # Control Room has no domain selector yet (that's Phase 4 of the
                               # multi-domain plan) -- every route defaults here so today's
                               # behavior is unchanged until the selector exists.


def load_platform(target: str) -> dict[str, Any]:
    path = REPO_ROOT / "contracts" / "platform" / f"{target}.yaml"
    if not path.exists():
        raise ValueError(f"unknown target {target!r} -- no {path}")
    return yaml.safe_load(path.read_text())


def available_targets() -> list[str]:
    return sorted(p.stem for p in (REPO_ROOT / "contracts" / "platform").glob("*.yaml"))


def _run_history(con, control: str, phase: str, domain: str) -> dict[str, dict[str, Any]]:
    """Per-source run history for this phase -- status/timing/failure facts only. NOT row or
    column counts: bronze_loader.py is idempotent (skips already-loaded files), so a re-run's
    rows_loaded reflects only what THAT run added, not the table's current size -- and for
    sources loaded before control-plane logging existed, run_registry may have no history at
    all even though the table is fully populated. Row/column counts come from the live tables
    instead (see _live_table_stats); run_registry answers "what happened and when", not
    "how much data is there right now"."""
    rows = con.execute(
        f"select source_id, status, files_quarantined, started_at, ended_at, error_message "
        f"from {control}.run_registry where phase = ? and domain = ? order by source_id, started_at",
        [phase, domain],
    ).fetchall()
    by_source: dict[str, dict[str, Any]] = {}
    for source_id, status, files_quarantined, started_at, ended_at, error_message in rows:
        agg = by_source.setdefault(source_id, {"quarantined": 0, "last_status": None,
                                                 "last_ended_at": None, "last_error": None})
        agg["quarantined"] += files_quarantined or 0
        agg["last_status"] = status
        agg["last_ended_at"] = ended_at
        agg["last_error"] = error_message
    return by_source


_LINEAGE_COLUMNS = {"data_catalogue_id", "source_record_id", "_run_id", "_source_file",
                     "_ingested_at", "_record_hash"}


def _live_table_stats(con, schema: str) -> dict[str, dict[str, int]]:
    """Ground truth for "what's actually in this layer right now": the live tables themselves,
    not a reconstruction from run history. One count(*) and one column-count per table -- cheap
    on local duckdb, a real network round trip each on Databricks, which is why the frontend
    polls this endpoint on a slower cadence than the alerts/status ones. Column counts exclude
    bronze_loader.py's 6 lineage columns, matching the "business columns only" convention
    columns_processed already uses everywhere else (see silver_transform.py's _bronze_columns)."""
    tables = [r[0] for r in con.execute(
        "select table_name from information_schema.tables where table_schema = ? order by table_name",
        [schema],
    ).fetchall()]
    stats = {}
    for t in tables:
        rows = con.execute(f"select count(*) from {schema}.{t}").fetchone()[0]
        col_names = {d[0] for d in con.execute(f"select * from {schema}.{t} limit 0").description}
        stats[t] = {"rows": rows, "columns": len(col_names - _LINEAGE_COLUMNS)}
    return stats


def pipeline_flow(target: str, domain: str = DEFAULT_DOMAIN) -> dict[str, Any]:
    """Current state of each medallion layer: row/column counts from the live tables (ground
    truth), status/failure/quarantine facts from run_registry (run history)."""
    platform = load_platform(target)
    control = resolve_schema(platform, domain, "control")
    con = sql_connect(target, platform)
    try:
        layers = {}
        for phase in PHASES:
            history = _run_history(con, control, phase, domain)
            live = _live_table_stats(con, resolve_schema(platform, domain, phase))
            failed = {sid: h for sid, h in history.items() if h["last_status"] == "failed"}
            layers[phase] = {
                "status": "failed" if failed else ("active" if live else "pending"),
                "table_count": len(live),
                "columns": sum(s["columns"] for s in live.values()),
                "rows": sum(s["rows"] for s in live.values()),
                "quarantined_batches": sum(h["quarantined"] for h in history.values()),
                "tables": sorted(live),
                "failed_tables": [{"source_id": sid, "error": h["last_error"]} for sid, h in failed.items()],
                "last_run_at": max((h["last_ended_at"] for h in history.values() if h["last_ended_at"]), default=None),
            }
        return {"target": target, "domain": domain, "layers": layers}
    finally:
        con.close()


def recent_alerts(target: str, limit: int = 12, domain: str = DEFAULT_DOMAIN) -> dict[str, Any]:
    platform = load_platform(target)
    control = resolve_schema(platform, domain, "control")
    con = sql_connect(target, platform)
    try:
        failures = con.execute(
            f"select source_id, phase, status, error_message, files_quarantined, ended_at "
            f"from {control}.run_registry "
            f"where domain = ? and (status = 'failed' or files_quarantined > 0) "
            f"order by ended_at desc limit ?",
            [domain, limit],
        ).fetchall()
        return {
            "target": target,
            "domain": domain,
            "failures": [
                {"source_id": r[0], "phase": r[1], "status": r[2], "error": r[3],
                 "files_quarantined": r[4], "ended_at": str(r[5]) if r[5] else None}
                for r in failures
            ],
            # Same query harness/daily_digest.py sends to Slack -- reused rather than
            # re-derived, including its fix for this sandboxed environment's clock instability
            # (it asks the data for the latest activity date instead of trusting datetime.now()).
            "latest_digest": gather_digest(target, None),
        }
    finally:
        con.close()
