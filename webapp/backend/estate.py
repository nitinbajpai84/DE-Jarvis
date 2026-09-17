"""API-facing layer over the Stage 01 estate scan (emitters/estate.py).

A scan takes seconds on DuckDB and minutes on Databricks, where every query is a network round
trip -- far too long to hold an HTTP request open. So starting one returns an id immediately and
runs the scan on a background thread; the Control Room polls the scan's status row in the
control plane, which the scan itself keeps current. Same "the database is the source of truth
the UI polls" model agent_runs.py already uses for agent runs.

One scan per (domain, target) at a time. A second click while one is running is refused rather
than queued: two scans over the same estate would contend for the same DuckDB file for no
benefit, and the second would report nothing the first won't.
"""
from __future__ import annotations

import pathlib
import sys
import threading
import traceback
import uuid
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters import estate as estate_mod  # noqa: E402

_RUNNING: dict[tuple[str, str], str] = {}
_LOCK = threading.Lock()


def start_scan(domain: str, target: str, tiers: int = 4) -> dict[str, Any]:
    key = (domain, target)
    with _LOCK:
        if key in _RUNNING:
            return {"started": False, "scan_id": _RUNNING[key],
                    "reason": f"a scan of {domain} on {target} is already running"}
        scan_id = uuid.uuid4().hex[:12]
        _RUNNING[key] = scan_id

    def _run():
        try:
            estate_mod.run_scan(domain, target, tiers=tiers, scan_id=scan_id)
        except Exception:  # noqa: BLE001 -- run_scan already records the failure on its own row
            traceback.print_exc()
        finally:
            with _LOCK:
                _RUNNING.pop(key, None)

    threading.Thread(target=_run, name=f"estate-scan-{scan_id}", daemon=True).start()
    return {"started": True, "scan_id": scan_id}


def is_running(domain: str, target: str) -> str | None:
    with _LOCK:
        return _RUNNING.get((domain, target))


def list_scans(domain: str, target: str) -> list[dict[str, Any]]:
    # This process is the only thing that runs scans for the Control Room, so any "running" row
    # it isn't actually running was orphaned by a restart and is closed before being shown.
    estate_mod.close_abandoned(domain, target, is_running(domain, target))
    return estate_mod.list_scans(domain, target)


def report(domain: str, target: str, scan_id: str | None = None) -> dict[str, Any]:
    return estate_mod.build_report(domain, target, scan_id)
