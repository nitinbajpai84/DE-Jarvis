"""Standalone entry point for one agent run -- launched as a background OS process by
webapp/backend/agent_runs.py (an agent invocation can take 30s-2min+ of real LLM time, far too
long to hold an HTTP request open for), and equally runnable by hand from a terminal.

Two subcommands, matching the two moments a human ever touches a run:
  start    kick off a new run against a workbook. Exits once the run either completes or hits
           the Freeze gate (write_intake_contracts) and pauses -- the pause is durable (see
           agents_loader.py's SqliteSaver checkpointer), so this process exiting is not losing
           anything; 'resume' picks it back up from a different process, possibly minutes later.
  resume   resume a paused run with a human decision (approve/reject), reconnecting the SAME
           checkpointer + thread_id. This is the literal mechanism proven in
           evidence/runs/phase-c-agent-activation.md, now given a CLI a background process (or
           a person) can call.

control.sdlc_run.status is the single source of truth the Control Room polls -- 'running' while
this process is doing something, 'awaiting_approval' the moment it pauses, 'completed'/'failed'/
'rejected' at the end. Written before this process can crash silently: if it dies mid-run for a
reason that isn't a clean interrupt, the row is left 'running' rather than lying about success.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import uuid

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
for line in (REPO_ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        import os
        os.environ.setdefault(k.strip(), v.strip())

from agents_loader import build_team  # noqa: E402
from emitters.control_plane import (  # noqa: E402
    ensure_control_schema, start_sdlc_run, update_sdlc_run_status,
)
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402
import yaml  # noqa: E402

# Agent-driven runs log against duckdb's control schema regardless of which platform bronze/
# silver/gold ultimately target -- run_bronze_source/run_silver_domain/run_gold_domain still
# take a real target param per call, this only fixes where the SDLC run/stage bookkeeping
# itself lives. A real limitation, not hidden: a Databricks-driven agent run's own audit trail
# would need this generalized, which hasn't been needed yet.
BOOKKEEPING_TARGET = "duckdb"


def _control(domain_hint: str):
    platform = yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{BOOKKEEPING_TARGET}.yaml").read_text())
    con = sql_connect(BOOKKEEPING_TARGET, platform)
    control = resolve_schema(platform, domain_hint, "control")
    ensure_control_schema(con, control)
    return con, control


def cmd_start(args: argparse.Namespace) -> None:
    run_id = args.run_id or str(uuid.uuid4())
    thread_id = args.thread_id or f"run-{run_id[:8]}"

    con, control = _control(args.domain_hint)
    try:
        start_sdlc_run(con, control, run_id=run_id, domain=args.domain_hint, client="default",
                       project_code=args.domain_hint, workbook_path=str(args.workbook),
                       thread_id=thread_id, started_by=args.started_by)
    finally:
        con.close()

    app = build_team(gated=True)
    config = {"configurable": {"thread_id": thread_id}}
    task = (
        f"A client has uploaded an intake workbook at '{args.workbook}'. Call "
        f"compile_intake_preview with workbook_path='{args.workbook}', run_id='{run_id}', "
        f"domain_hint='{args.domain_hint}'. Report the summary, warnings and open_questions "
        f"plainly. If it compiled with ok=true and you judge it reasonable to proceed (few or "
        f"no open_questions), call write_intake_contracts with the same workbook_path and "
        f"run_id to move it to the Freeze gate. If there are errors or open_questions that need "
        f"a human's judgement, do NOT call write_intake_contracts -- explain clearly what needs "
        f"resolving instead, and stop."
    )

    try:
        result = app.invoke({"messages": [{"role": "user", "content": task}]}, config=config)
    except Exception as exc:  # noqa: BLE001
        con, control = _control(args.domain_hint)
        try:
            update_sdlc_run_status(con, control, run_id, "failed")
        finally:
            con.close()
        print(f"FAILED: {exc}", file=sys.stderr)
        raise

    con, control = _control(args.domain_hint)
    try:
        if "__interrupt__" in result:
            update_sdlc_run_status(con, control, run_id, "awaiting_approval")
            print(f"AWAITING_APPROVAL run_id={run_id} thread_id={thread_id}")
        else:
            update_sdlc_run_status(con, control, run_id, "completed")
            print(f"COMPLETED run_id={run_id}")
    finally:
        con.close()


def cmd_resume(args: argparse.Namespace) -> None:
    con, control = _control(args.domain_hint)
    try:
        update_sdlc_run_status(con, control, args.run_id, "running")
    finally:
        con.close()

    app = build_team(gated=True)
    config = {"configurable": {"thread_id": args.thread_id}}
    decision: dict = {"type": args.decision}
    if args.decision == "reject" and args.message:
        decision["message"] = args.message
    from langgraph.types import Command
    try:
        result = app.invoke(Command(resume={"decisions": [decision]}), config=config)
    except Exception as exc:  # noqa: BLE001
        con, control = _control(args.domain_hint)
        try:
            update_sdlc_run_status(con, control, args.run_id, "failed")
        finally:
            con.close()
        print(f"FAILED: {exc}", file=sys.stderr)
        raise

    con, control = _control(args.domain_hint)
    try:
        if "__interrupt__" in result:
            update_sdlc_run_status(con, control, args.run_id, "awaiting_approval")
            print(f"AWAITING_APPROVAL run_id={args.run_id}")
        elif args.decision == "reject":
            update_sdlc_run_status(con, control, args.run_id, "rejected")
            print(f"REJECTED run_id={args.run_id}")
        else:
            update_sdlc_run_status(con, control, args.run_id, "completed")
            print(f"COMPLETED run_id={args.run_id}")
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("--workbook", required=True)
    p_start.add_argument("--domain-hint", required=True)
    p_start.add_argument("--run-id", default=None)
    p_start.add_argument("--thread-id", default=None)
    p_start.add_argument("--started-by", default="control-room")
    p_start.set_defaults(func=cmd_start)

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("--run-id", required=True)
    p_resume.add_argument("--thread-id", required=True)
    p_resume.add_argument("--domain-hint", required=True)
    p_resume.add_argument("--decision", choices=["approve", "reject"], required=True)
    p_resume.add_argument("--message", default=None)
    p_resume.set_defaults(func=cmd_resume)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
