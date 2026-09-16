"""API-facing layer over real agent runs (agents/run_cli.py). Starting or resuming a run
launches run_cli.py as a detached background process -- an agent invocation is real LLM time
(30s-2min+), far too long to hold an HTTP request open for -- so every route here either reads
control.sdlc_run/sdlc_stage_run (the same poll-the-database pattern the rest of the Control Room
already uses) or kicks off a background process and returns immediately.

Inspecting a paused run's pending action is NOT done by parsing anything this module wrote --
it reconnects the SAME SqliteSaver checkpointer and thread_id agents_loader.py used and calls
the compiled graph's own get_state(), which returns langgraph's real StateSnapshot.interrupts.
That's live truth from the actual paused graph, not a guess at what's pending.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import uuid
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters.control_plane import ensure_control_schema  # noqa: E402
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402
import yaml  # noqa: E402

BOOKKEEPING_TARGET = "duckdb"  # see agents/run_cli.py's own note on this
PYTHON = sys.executable
RUN_CLI = REPO_ROOT / "agents" / "run_cli.py"
PROCESS_LOG_DIR = REPO_ROOT / "harness" / "agent_run_logs"


def _log_file_for(run_id: str):
    """Every background process's stdout/stderr goes here, never to DEVNULL -- a first version
    of start_run()/resume_run() discarded it, and a subprocess that crashed before writing
    anything to control.sdlc_run (found happening for real while diagnosing a run that appeared
    to skip its own approval gate) left zero trace of why. Never repeat that for something this
    safety-relevant."""
    PROCESS_LOG_DIR.mkdir(parents=True, exist_ok=True)
    return (PROCESS_LOG_DIR / f"{run_id}.log").open("a")


# DuckDB does not safely allow concurrent DDL/writes from separate processes to the same file
# (see agents/jarvis_tools.py's _ENSURED cache and emitters/control_plane.py's log_sdlc_stage
# retry loop for the two concurrency bugs that surfaced from this during Phase C/D testing --
# a real "File is already open" IOException was reproduced live: the webapp process and a
# background agent subprocess both touch harness/jarvis.duckdb). This module reads that same
# file on every poll, so it gets the same retry treatment, not just a hope that timing works out.
_ENSURED: set[str] = set()


def _control_con(domain: str):
    import random
    import time
    platform = yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{BOOKKEEPING_TARGET}.yaml").read_text())
    control = resolve_schema(platform, domain, "control")
    for attempt in range(5):
        try:
            con = sql_connect(BOOKKEEPING_TARGET, platform)
            if control not in _ENSURED:
                ensure_control_schema(con, control)
                _ENSURED.add(control)
            return con, control
        except Exception:  # noqa: BLE001
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1) + random.random() * 0.05)


def _any_domain_con():
    """sdlc_run spans every domain that's ever had an agent run against it, but control lives
    per-domain -- list_runs() needs to check the insurance domain at minimum (it always exists)
    and any domain an agent run has since created. Good enough for the number of domains this
    project actually has; a real domain registry is future work, not invented here."""
    from emitters.control_plane import ensure_control_schema as ecs
    domains = ["insurance"]
    sources_dir = REPO_ROOT / "contracts" / "sources"
    if sources_dir.exists():
        domains += [p.name for p in sources_dir.iterdir() if p.is_dir() and p.name != "insurance"]
    seen_runs: dict[str, dict[str, Any]] = {}
    for d in dict.fromkeys(domains):
        try:
            con, control = _control_con(d)
        except Exception:  # noqa: BLE001
            continue
        try:
            rows = con.execute(
                f"select run_id, domain, client, project_code, workbook_path, thread_id, status, "
                f"started_at, updated_at, started_by from {control}.sdlc_run"
            ).fetchall()
            for r in rows:
                seen_runs[r[0]] = {
                    "run_id": r[0], "domain": r[1], "client": r[2], "project_code": r[3],
                    "workbook_path": r[4], "thread_id": r[5], "status": r[6],
                    "started_at": str(r[7]), "updated_at": str(r[8]), "started_by": r[9],
                }
        finally:
            con.close()
    return seen_runs


def list_runs() -> list[dict[str, Any]]:
    runs = _any_domain_con()
    for run_id, r in runs.items():
        con, control = _control_con(r["domain"])
        try:
            stages = con.execute(
                f"select stage, agent, status, detail, started_at from {control}.sdlc_stage_run "
                f"where run_id = ? order by started_at desc limit 1", [run_id],
            ).fetchone()
        finally:
            con.close()
        r["latest_stage"] = ({"stage": stages[0], "agent": stages[1], "status": stages[2],
                              "detail": stages[3], "started_at": str(stages[4])} if stages else None)
    return sorted(runs.values(), key=lambda r: r["started_at"], reverse=True)


def get_run_detail(run_id: str) -> dict[str, Any]:
    runs = _any_domain_con()
    if run_id not in runs:
        raise KeyError(f"no run {run_id!r}")
    run = runs[run_id]
    con, control = _control_con(run["domain"])
    try:
        stages = con.execute(
            f"select stage, agent, status, detail, started_at from {control}.sdlc_stage_run "
            f"where run_id = ? order by started_at", [run_id],
        ).fetchall()
    finally:
        con.close()
    run["stages"] = [{"stage": s[0], "agent": s[1], "status": s[2], "detail": s[3], "started_at": str(s[4])}
                     for s in stages]

    run["pending_action"] = None
    if run["status"] == "awaiting_approval":
        run["pending_action"] = _inspect_pending(run["thread_id"])
    return run


def _inspect_pending(thread_id: str) -> dict[str, Any] | None:
    import sqlite3
    from agents_loader import CHECKPOINT_DB, build_team
    if not CHECKPOINT_DB.exists():
        return None
    conn = sqlite3.connect(str(CHECKPOINT_DB), check_same_thread=False)
    from langgraph.checkpoint.sqlite import SqliteSaver
    checkpointer = SqliteSaver(conn)
    app = build_team(gated=True, checkpointer=checkpointer)  # reconnect to the paused run's
                                                              # exact state, not a fresh one
    snapshot = app.get_state({"configurable": {"thread_id": thread_id}})
    conn.close()
    if not snapshot.interrupts:
        return None
    interrupt = snapshot.interrupts[0]
    value = interrupt.value if hasattr(interrupt, "value") else interrupt
    requests = value.get("action_requests", [])
    # Which gate is this? The graph only knows a tool name; the journey view needs the gate's
    # identity (G1..G4, which step it sits on, what it approves) so an approval screen can say
    # what is actually being decided instead of just echoing a function name at the customer.
    from agents.gates import GATE_BY_TOOL
    gate = next((GATE_BY_TOOL[r["name"]] for r in requests if r.get("name") in GATE_BY_TOOL), None)
    return {"action_requests": requests,
            "review_configs": value.get("review_configs", []),
            "gate": gate}


def start_run(workbook_path: str, domain_hint: str, started_by: str = "control-room") -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    thread_id = f"run-{run_id[:8]}"
    log = _log_file_for(run_id)
    proc = subprocess.Popen(
        [PYTHON, str(RUN_CLI), "start", "--kind", "intake", "--workbook", workbook_path,
         "--domain-hint", domain_hint, "--run-id", run_id, "--thread-id", thread_id,
         "--started-by", started_by],
        cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT,
    )
    return {"run_id": run_id, "thread_id": thread_id, "pid": proc.pid, "status": "starting",
           "log_file": str(PROCESS_LOG_DIR / f"{run_id}.log")}


def start_validation_run(domain: str, target: str, started_by: str = "control-room") -> dict[str, Any]:
    """Same background-process pattern as start_run, but drives Step 04 validation for a
    domain that already has approved contracts and real data -- no workbook, no intake. The PM
    agent calls run_test_pack -> gather_validation_pack -> accept_validation on its own,
    reaching the G3 gate for real instead of a script calling the tools directly."""
    run_id = str(uuid.uuid4())
    thread_id = f"run-{run_id[:8]}"
    log = _log_file_for(run_id)
    proc = subprocess.Popen(
        [PYTHON, str(RUN_CLI), "start", "--kind", "validate", "--domain-hint", domain,
         "--target", target, "--run-id", run_id, "--thread-id", thread_id,
         "--started-by", started_by],
        cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT,
    )
    return {"run_id": run_id, "thread_id": thread_id, "pid": proc.pid, "status": "starting",
           "log_file": str(PROCESS_LOG_DIR / f"{run_id}.log")}


def resume_run(run_id: str, decision: str, message: str | None = None) -> dict[str, Any]:
    runs = _any_domain_con()
    if run_id not in runs:
        raise KeyError(f"no run {run_id!r}")
    run = runs[run_id]
    args = [PYTHON, str(RUN_CLI), "resume", "--run-id", run_id, "--thread-id", run["thread_id"],
            "--domain-hint", run["domain"], "--decision", decision]
    if message:
        args += ["--message", message]
    log = _log_file_for(run_id)
    proc = subprocess.Popen(args, cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT)
    return {"run_id": run_id, "pid": proc.pid, "status": "resuming"}
