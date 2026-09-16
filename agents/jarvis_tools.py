"""Tools that let the deepagents team (agents_loader.py) drive Jarvis's REAL, already-proven
pipeline -- emitters/intake_compiler.py, bronze_loader.py, silver_transform.py, gold_transform.py
-- instead of a sandboxed stand-in. No pipeline logic lives here; every tool is a thin wrapper
that also writes to control.sdlc_run / sdlc_stage_run so a run started by an agent is a durable,
queryable object (Phase D's Control Room Run view reads these tables), not just LLM chat history.

Six-stage factory -> Jarvis's 10-stage docs/agentic-sdlc.md view (informational, for whoever
builds Phase D's frontend mapping):
  discover  ~ Intent + Requirement Analysis + Codebase Discovery (stages 1-3)
  specify   ~ Design + Plan (stages 4-5)
  freeze    ~ the human gate itself, between Plan and Implementation
  build     ~ Implementation (stage 6)
  validate  ~ Testing & Validation + Review (stages 7-8)
  operate   ~ Impact Analysis + Evidence (stages 9-10)

Only write_intake_contracts is a gated tool (see agents_loader.py's GATE_INTERRUPTS) -- it's the
one step that changes contracts/, the source of truth every other tool and the real bronze/
silver/gold engine trusts. Running bronze/silver/gold against already-approved contracts is
comparatively safe and reversible (re-runnable, domain-scoped, never touches another domain's
schema -- see emitters/sql_dialect.py's resolve_schema), so those aren't gated a second time.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import tool

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters import intake_compiler  # noqa: E402
from emitters.control_plane import ensure_control_schema, log_sdlc_stage  # noqa: E402
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402


# ensure_control_schema() runs real DDL (CREATE TABLE IF NOT EXISTS, ALTER TABLE ADD COLUMN
# checks). DuckDB does not safely allow concurrent DDL from separate connections to the same
# file -- confirmed by a real stress test during Phase C (10 threads each opening their own
# connection produced "Catalog write-write conflict on alter" from several of them). Since an
# agent run makes several close-together tool calls, each opening its own connection via
# _control_con(), re-running the full DDL on every single call isn't just wasteful, it's the
# actual cause of that conflict. Cache "already ensured this (target, domain) this process" so
# DDL runs once, not once per tool call -- the real fix, not a retry papering over the symptom.
_ENSURED: set[tuple[str, str]] = set()


def _control_con(target: str, domain: str):
    import yaml
    platform = yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())
    con = sql_connect(target, platform)
    control = resolve_schema(platform, domain, "control")
    key = (target, control)
    if key not in _ENSURED:
        ensure_control_schema(con, control)
        _ENSURED.add(key)
    return con, control


_LOG_FAILURES_PATH = REPO_ROOT / "harness" / "sdlc_log_failures.log"


def _log(target: str, domain: str, run_id: str, stage: str, agent: str, status: str, detail: str) -> None:
    """Best-effort stage logging -- a logging failure must never take down the actual pipeline
    action it's describing, so this doesn't raise. It's NOT fully silent, though: a first
    version of this swallowed every exception with a bare `except: pass`, and during real
    end-to-end agent testing every stage-logging call failed for a reason that was then
    impossible to diagnose (the pipeline actions themselves all succeeded correctly -- only
    this observability trail went dark). Failures now append to a small log file instead, so
    the next person (or session) hitting this doesn't start from zero.

    Retries the whole connect-and-insert on a transaction conflict (see _ENSURED's docstring
    and log_sdlc_stage's own retry for the two real conflict modes found by stress-testing this
    under concurrency) -- caching ensure_control_schema calls closes most of the window, this
    covers what's left."""
    import random
    import time
    for attempt in range(5):
        try:
            con, control = _control_con(target, domain)
            try:
                log_sdlc_stage(con, control, run_id=run_id, stage=stage, agent=agent, status=status,
                               detail=detail[:2000], started_at=datetime.now(timezone.utc))
            finally:
                con.close()
            return
        except Exception as exc:  # noqa: BLE001
            transient = any(s in str(exc).lower() for s in ("conflict", "duplicate key", "constraint"))
            if attempt < 4 and transient:
                time.sleep(0.05 * (attempt + 1) + random.random() * 0.05)
                continue
            try:
                with _LOG_FAILURES_PATH.open("a") as f:
                    f.write(f"{datetime.now(timezone.utc).isoformat()} run={run_id} stage={stage} "
                            f"domain={domain} target={target}: {type(exc).__name__}: {exc}\n")
            except Exception:  # noqa: BLE001 -- the log-of-last-resort must never itself raise
                pass
            return


@tool
def compile_intake_preview(workbook_path: str, run_id: str, domain_hint: str = "unknown") -> str:
    """Preview-compile the intake workbook: validates it against contracts/schema/
    intake_spec.schema.json, then translates it into what Jarvis's source/model/gold contracts
    WOULD look like -- without writing anything to contracts/. Returns a JSON report: on
    success, {ok: true, domain, client, sources, dims, facts, marts, warnings, open_questions};
    on failure, {ok: false, errors: [...]} with the exact sheet/row of each problem. Always call
    this before write_intake_contracts -- it is the only way to see open_questions and warnings
    (multi-source silver conform, unimplemented DQ/business checks, etc.) before they'd matter.

    Args:
        workbook_path: path to the filled-in intake .xlsx
        run_id: the sdlc_run id this action belongs to (for stage logging)
        domain_hint: expected project.domain, only used to log the stage under the right domain's
            control schema before the real domain is known from the workbook itself
    """
    try:
        spec, errors = intake_compiler.compile_workbook(pathlib.Path(workbook_path))
        if errors:
            _log("duckdb", domain_hint, run_id, "specify", "system", "failed", f"{len(errors)} validation errors")
            return _json({"ok": False, "errors": errors})
        compiled = intake_compiler.spec_to_contracts(spec)
        open_qs = [oq for s in compiled["sources"] for oq in s.get("open_questions", [])]
        open_qs += compiled["model"].get("open_questions", [])
        report = {
            "ok": True, "domain": spec["project"]["domain"], "client": spec["project"].get("client", "default"),
            "sources": [s["source_id"] for s in compiled["sources"]],
            "dims": [d["name"] for d in compiled["model"]["dimensions"]],
            "facts": [f["name"] for f in compiled["model"]["facts"]],
            "marts": [m["name"] for m in compiled["gold"]["marts"]],
            "warnings": compiled["warnings"], "open_questions": open_qs,
        }
        _log("duckdb", report["domain"], run_id, "specify", "system", "completed",
             f"{len(report['sources'])} sources, {len(open_qs)} open questions, {len(compiled['warnings'])} warnings")
        return _json(report)
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "errors": [str(exc)]})


@tool
def write_intake_contracts(workbook_path: str, run_id: str) -> str:
    """FREEZE GATE: compiles the intake workbook and WRITES the resulting contracts to
    contracts/sources/<domain>/, contracts/models/<domain>.model.yaml,
    contracts/semantics/<domain>.gold.yaml, and contracts/odcs/<domain>/ -- the one action in
    this tool set that changes the platform's source of truth. This tool is configured as an
    interrupt point (see agents_loader.py's GATE_INTERRUPTS) -- the agent graph pauses before
    this call actually runs, and only resumes once a human approves. If compile_intake_preview
    hasn't already been called and reviewed, call it first.

    Args:
        workbook_path: path to the filled-in intake .xlsx
        run_id: the sdlc_run id this action belongs to
    """
    spec, errors = intake_compiler.compile_workbook(pathlib.Path(workbook_path))
    if errors:
        return _json({"ok": False, "errors": errors})
    compiled = intake_compiler.spec_to_contracts(spec)
    domain = spec["project"]["domain"]
    written = intake_compiler.write_contracts(spec, compiled)
    _log("duckdb", domain, run_id, "freeze", "human+system", "completed",
         f"wrote {len(written)} contract files for domain={domain!r}")
    return _json({"ok": True, "domain": domain, "written": [str(p) for p in written]})


@tool
def run_bronze_source(source_id: str, target: str, run_id: str, domain: str) -> str:
    """Runs the real bronze loader (emitters/bronze_loader.py) for one source_id against
    'duckdb' or 'databricks'. Idempotent -- safe to call again for the same source, already-
    loaded files are skipped, not reprocessed. domain is read from the source's own contract at
    load time (see bronze_loader.py's run()); pass it here only so this tool can log the stage
    under the right control schema.

    Args:
        source_id: the source_id to load, e.g. 'vi_products'
        target: 'duckdb' or 'databricks'
        run_id: the sdlc_run id this action belongs to
        domain: the domain this source belongs to (for stage logging)
    """
    from emitters.bronze_loader import run as bronze_run
    try:
        summary = bronze_run(source_id, target)
        _log(target, domain, run_id, "build", "de-bronze", "completed", f"{source_id}: {summary}")
        return _json({"ok": True, "summary": summary})
    except Exception as exc:  # noqa: BLE001
        _log(target, domain, run_id, "build", "de-bronze", "failed", f"{source_id}: {exc}")
        return _json({"ok": False, "error": str(exc)})


@tool
def run_silver_domain(domain: str, target: str, run_id: str) -> str:
    """Runs the real silver transform (emitters/silver_transform.py) for every dimension/fact
    in contracts/models/<domain>.model.yaml. Rebuilds each table from scratch (delete+insert
    from bronze), so it's safe to re-run.

    Args:
        domain: the domain to build silver for, e.g. 'insurance'
        target: 'duckdb' or 'databricks'
        run_id: the sdlc_run id this action belongs to
    """
    from emitters.silver_transform import run as silver_run
    try:
        summary = silver_run(domain, target)
        _log(target, domain, run_id, "build", "de-silver", "completed", f"{summary}")
        return _json({"ok": True, "summary": summary})
    except Exception as exc:  # noqa: BLE001
        _log(target, domain, run_id, "build", "de-silver", "failed", str(exc))
        return _json({"ok": False, "error": str(exc)})


@tool
def run_gold_domain(domain: str, target: str, run_id: str) -> str:
    """Runs the real gold transform (emitters/gold_transform.py) for every mart in
    contracts/semantics/<domain>.gold.yaml, including its business-rule checks (variance_vs_
    history, dimension_mapping_inconsistency, new_or_missing_dimension), logged to
    control.dq_results.

    Args:
        domain: the domain to build gold for
        target: 'duckdb' or 'databricks'
        run_id: the sdlc_run id this action belongs to
    """
    from emitters.gold_transform import run as gold_run
    try:
        summary = gold_run(domain, target)
        _log(target, domain, run_id, "validate", "de-gold", "completed", f"{summary}")
        return _json({"ok": True, "summary": summary})
    except Exception as exc:  # noqa: BLE001
        _log(target, domain, run_id, "validate", "de-gold", "failed", str(exc))
        return _json({"ok": False, "error": str(exc)})


@tool
def run_regression_tests(target: str, run_id: str, domain: str) -> str:
    """Runs Jarvis's existing pytest suite (tests/test_pipeline.py) against 'duckdb' or
    'databricks'. KNOWN LIMITATION: this suite is currently hard-coded to the insurance domain
    (see its own DOMAIN constant) -- it does not yet parametrize per-domain, so it proves "the
    existing live insurance domain is still healthy after this change", not "the new domain's
    own build is correct". Report this limitation plainly rather than implying broader coverage
    than actually exists.

    Args:
        target: 'duckdb' or 'databricks'
        run_id: the sdlc_run id this action belongs to
        domain: the domain being worked on (for stage logging only -- the suite itself always
            tests insurance)
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_pipeline.py", "-q", f"--target={target}"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
    )
    ok = proc.returncode == 0
    _log(target, domain, run_id, "validate", "test-manager", "completed" if ok else "failed",
         proc.stdout[-1500:])
    return _json({"ok": ok, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:]})


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj, default=str)


ALL_TOOLS = [compile_intake_preview, write_intake_contracts, run_bronze_source,
             run_silver_domain, run_gold_domain, run_regression_tests]
