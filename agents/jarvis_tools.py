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

from emitters import intake_compiler, intent as intent_mod, profiler  # noqa: E402
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


# ---------------------------------------------------------------------------------------
# Step 01 discovery tools (agent 1, Business Analyst). Both read-only, both ungated -- same
# reasoning as compile_intake_preview: nothing here writes to contracts/ or lands data, so
# there is nothing for a human to approve yet. See emitters/profiler.py for the real
# connect-and-sample logic; these are thin wrappers that also log the stage.
# ---------------------------------------------------------------------------------------

@tool
def test_source_connection(connection_json: str, run_id: str, domain: str) -> str:
    """Cheap reachability check for a candidate source, BEFORE spending time sampling it.
    connection_json is a JSON object in the exact same shape a compiled source contract's
    `connection:` block uses -- {"type": "file", "path": "...", ...} for a landing-zone file,
    {"type": "database", "dialect": "databricks", "table": "catalog.schema.table"} for a live
    table, {"type": "api", "endpoint": "..."} for a REST source. Returns {ok, detail}.

    Args:
        connection_json: JSON-encoded connection config (same shape as source.yaml's connection:)
        run_id: the sdlc_run id this action belongs to
        domain: the domain this candidate source belongs to (for stage logging)
    """
    import json as _json
    connection = _json.loads(connection_json)
    result = profiler.test_connection(connection)
    _log("duckdb", domain, run_id, "discover", "business-analyst",
         "completed" if result["ok"] else "failed",
         f"connection test ({connection.get('type')}): {result['detail']}")
    return _json.dumps(result)


@tool
def profile_source(connection_json: str, source_id: str, domain: str, run_id: str,
                    sample_limit: int = 500) -> str:
    """Connects to a candidate source for real and profiles it: per-column inferred type, null
    rate, distinct count, candidate-key flag, and sample values, plus the total row count where
    that's cheap to know. This is the raw material for drafting the catalogue at Step 02 -- it
    does not write to contracts/ or land any data to bronze. Always call test_source_connection
    first. Persists to contracts/discovery/<domain>/<source_id>.profile.json.

    Args:
        connection_json: JSON-encoded connection config (same shape as source.yaml's connection:)
        source_id: a short id for this candidate source, e.g. 'orders' -- becomes its source_id
            if it's later promoted into the intake workbook
        domain: the domain this candidate source belongs to
        run_id: the sdlc_run id this action belongs to
        sample_limit: rows to sample (default 500) -- profiling reads a sample, never the whole
            table/file set, so this stays fast against a large live source
    """
    import json as _json
    connection = _json.loads(connection_json)
    try:
        report = profiler.profile_source(connection, source_id, domain, sample_limit)
        keys = ", ".join(report["candidate_keys"]) or "none found"
        _log("duckdb", domain, run_id, "discover", "business-analyst", "completed",
             f"profiled {source_id}: {len(report['columns'])} columns, "
             f"{report['sampled_rows']}/{report['total_rows']} rows sampled, candidate keys: {keys}")
        return _json.dumps(report)
    except Exception as exc:  # noqa: BLE001
        _log("duckdb", domain, run_id, "discover", "business-analyst", "failed",
             f"profile {source_id} failed: {exc}")
        return _json.dumps({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------------------
# Step 02 intent + gap analysis (agent 3 PM captures intent, agent 1 BA runs the gap check).
# Both ungated, same reasoning as the Step 01 discovery tools -- neither writes to contracts/
# or lands data, so there's nothing yet for a human to approve. See emitters/intent.py.
# ---------------------------------------------------------------------------------------

@tool
def capture_intent(domain: str, client: str, intent_json: str, run_id: str,
                    captured_by: str = "program-manager") -> str:
    """Records what the customer is actually building this for: business outcome, SLAs, the
    reports it feeds (each with the data points it needs), definition of done, and any business
    term whose definition differs across their systems. Overwrites any prior intent record for
    this domain -- a human revising intent should see exactly what they submitted, not a silent
    merge with a stale draft. intent_json is a JSON object:
    {"business_outcome": str, "definition_of_done": str,
     "sla": {"freshness": str, "availability": str},
     "reports": [{"name": str, "description": str, "consumers": [str],
                  "required_data_points": [str]}],
     "definitions": [{"term": str, "definition": str, "source": str}],
     "stakeholders": [{"name": str, "role": str}]}
    Persisted to contracts/intent/<domain>/intent.yaml.

    Args:
        domain: the domain this intent belongs to
        client: the client/tenant (default 'default')
        intent_json: JSON-encoded intent record, shape above
        run_id: the sdlc_run id this action belongs to
        captured_by: who captured this -- a free-text label for the gate record, e.g. the
            agent's own name or "human" if a person filled the form directly
    """
    import json as _json_mod
    intent_dict = _json_mod.loads(intent_json)
    path = intent_mod.capture_intent(domain, client, intent_dict, captured_by)
    _log("duckdb", domain, run_id, "specify", "program-manager", "completed",
         f"intent captured for domain={domain!r}: "
         f"{len(intent_dict.get('reports') or [])} report(s), "
         f"{len(intent_dict.get('definitions') or [])} definition(s)")
    return _json({"ok": True, "path": str(path)})


@tool
def run_gap_analysis(domain: str, run_id: str) -> str:
    """Checks the captured intent against every column this platform actually knows about for
    this domain -- approved contracts AND Step 01 discovery profiles. EXACT name matching only,
    case-insensitive: this never guesses that two different-looking names mean the same thing.
    A required data point that matches no known column is an open gap. A business term defined
    more than once with different wording is an open conflict. Call capture_intent first --
    if no intent exists yet, this reports intent_captured: false rather than an error.

    Args:
        domain: the domain to check
        run_id: the sdlc_run id this action belongs to
    """
    report = intent_mod.run_gap_analysis(domain)
    _log("duckdb", domain, run_id, "specify", "business-analyst",
         "completed" if not report["open_count"] else "failed",
         f"gap analysis for domain={domain!r}: {report['open_count']} open item(s)"
         if report["intent_captured"] else f"gap analysis for domain={domain!r}: no intent captured")
    return _json(report)


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


# ---------------------------------------------------------------------------------------
# Gate tools (see agents/gates.py for the registry that makes these interrupt points).
#
# Every one of these bodies runs ONLY after a human has approved -- that is what the langgraph
# interrupt guarantees -- so writing "APPROVED" into the record from inside the tool is a
# statement of fact, not an assumption.
#
# They also re-derive their own evidence instead of trusting a `summary` argument from the
# model. A gate whose blocking rule is evaluated against text the model wrote is not a gate:
# the thing being checked and the thing doing the checking would be the same author. So
# accept_catalogue recompiles the workbook itself, and the two accept_* tools below read back
# evidence a prior tool persisted to disk. If that evidence is missing or failing, the gate
# refuses even though a human clicked Approve -- the human is approving the evidence, and there
# has to be evidence.
# ---------------------------------------------------------------------------------------

GATE_RECORD_DIR = REPO_ROOT / "evidence" / "gates"
GATE_EVIDENCE_DIR = REPO_ROOT / "evidence" / "runs"


def _write_gate_record(gate_id: str, gate_name: str, run_id: str, domain: str,
                       approved: list[str], sections: str = "") -> pathlib.Path:
    """Writes the gate record in the same shape as evidence/gates/TEMPLATE-gate.md, which the
    hand-written P1 records already follow -- one format whether a human or an agent ran the
    gate, so the evidence trail doesn't fork by author."""
    GATE_RECORD_DIR.mkdir(parents=True, exist_ok=True)
    path = GATE_RECORD_DIR / f"{run_id[:8]}-{gate_id}.md"
    body = [
        "# Gate Record", "",
        f"Run:              {run_id}",
        f"Gate:             {gate_id} {gate_name}",
        f"Domain:           {domain}",
        f"Date:             {datetime.now(timezone.utc).isoformat()}",
        "Approver (human): via Control Room approval (langgraph interrupt resume)", "",
        "## Decision", "APPROVED", "",
        "## What was approved",
    ]
    body += [f"- {line}" for line in approved]
    if sections:
        body += ["", sections]
    body += ["", "## How this was enforced",
             "The agent graph paused before this tool ran and could not proceed without a human",
             "decision. The evidence above was re-derived by the tool itself, not taken from the",
             "model's own summary.", ""]
    path.write_text("\n".join(body))
    return path


def _evidence_path(run_id: str, kind: str) -> pathlib.Path:
    return GATE_EVIDENCE_DIR / f"{run_id[:8]}-{kind}.json"


@tool
def accept_catalogue(workbook_path: str, run_id: str) -> str:
    """G1 GATE -- records the customer's acceptance of the compiled catalogue and intent.
    Pauses for human approval before it runs. Recompiles the workbook itself to check G1's
    blocking rule (open questions must be EMPTY) rather than trusting a summary, and also
    re-runs gap analysis fresh (see emitters/intent.py) -- REFUSES even after approval if any
    open question remains, OR if intent has been captured for this domain and gap analysis
    finds an unresolved data-point gap or a definition conflict. An unanswered ambiguity is a
    blocker, not a note, whether the compiler found it or gap analysis did. If intent was never
    captured for this domain, that's not a failure -- intent capture is additive, not required,
    so a domain with no intent record passes on catalogue completeness alone, same as before
    this existed. Writes evidence/gates/<run>-G1.md on success.

    Args:
        workbook_path: path to the filled-in intake .xlsx that was previewed
        run_id: the sdlc_run id this action belongs to
    """
    spec, errors = intake_compiler.compile_workbook(pathlib.Path(workbook_path))
    if errors:
        return _json({"ok": False, "errors": errors})
    compiled = intake_compiler.spec_to_contracts(spec)
    domain = spec["project"]["domain"]
    open_qs = [oq for s in compiled["sources"] for oq in s.get("open_questions", [])]
    open_qs += compiled["model"].get("open_questions", [])
    if open_qs:
        _log("duckdb", domain, run_id, "freeze", "human+system", "failed",
             f"G1 refused: {len(open_qs)} open question(s) still unanswered")
        return _json({"ok": False, "gate": "G1", "refused": True, "open_questions": open_qs,
                      "reason": "G1 cannot pass while open questions remain. Resolve them in the "
                                "workbook and re-preview."})

    gaps = intent_mod.run_gap_analysis(domain)
    if gaps["intent_captured"] and gaps["open_count"]:
        _log("duckdb", domain, run_id, "freeze", "human+system", "failed",
             f"G1 refused: gap analysis found {gaps['open_count']} unresolved item(s)")
        return _json({"ok": False, "gate": "G1", "refused": True, "gap_analysis": gaps,
                      "reason": "G1 cannot pass while gap analysis has open data-point gaps or "
                                "definition conflicts. Resolve them or update the intent."})

    intent_summary = (
        f"Intent captured: business outcome recorded, {len(gaps['data_point_gaps'])} data "
        f"point(s) checked against known sources, 0 open"
        if gaps["intent_captured"] else "No intent record for this domain yet"
    )
    record = _write_gate_record(
        "G1", "Catalogue & intent", run_id, domain,
        approved=[
            f"Domain: {domain} (client {spec['project'].get('client', 'default')})",
            f"Sources mapped: {', '.join(s['source_id'] for s in compiled['sources'])}",
            f"Dimensions: {', '.join(d['name'] for d in compiled['model']['dimensions']) or 'none'}",
            f"Facts: {', '.join(f['name'] for f in compiled['model']['facts']) or 'none'}",
            f"Gold marts: {', '.join(m['name'] for m in compiled['gold']['marts']) or 'none'}",
            intent_summary,
        ],
        sections="## Open Questions status\nAll closed?  YES\n",
    )
    _log("duckdb", domain, run_id, "freeze", "human+system", "completed",
         f"G1 accepted: catalogue for domain={domain!r}, 0 open questions, "
         f"gap analysis {'clean' if gaps['intent_captured'] else 'n/a'}")
    return _json({"ok": True, "gate": "G1", "domain": domain, "record": str(record), "gap_analysis": gaps})


@tool
def gather_validation_pack(domain: str, target: str, run_id: str) -> str:
    """Assembles the evidence a human needs at the G3 validation gate, and persists it so the
    gate tool can read it back instead of trusting a summary: the regression suite result, the
    data-quality results recorded for this domain, and the live row/table counts per layer.
    Ungated -- this only reads. Call this before accept_validation.

    Args:
        domain: the domain being validated
        target: 'duckdb' or 'databricks'
        run_id: the sdlc_run id this action belongs to
    """
    import json
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_pipeline.py", "-q", f"--target={target}"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    tests_ok = proc.returncode == 0

    con, control = _control_con(target, domain)
    try:
        dq = con.execute(
            f"select rule_type, columns, severity, passed, failed_row_count from {control}.dq_results "
            f"where domain = ? order by evaluated_at desc limit 50", [domain],
        ).fetchall()
    finally:
        con.close()
    dq_rows = [{"rule": r[0], "column": r[1], "severity": r[2], "passed": bool(r[3]),
                "failed_rows": r[4]} for r in dq]
    dq_failed = [d for d in dq_rows if not d["passed"]]

    from webapp.backend.pipeline import pipeline_flow
    flow = pipeline_flow(target, domain)
    layers = {p: {"tables": L["table_count"], "rows": L["rows"]} for p, L in flow["layers"].items()}

    pack = {
        "domain": domain, "target": target,
        "tests": {"passed": tests_ok, "output": proc.stdout[-1200:]},
        "dq": {"checked": len(dq_rows), "failed": len(dq_failed), "failures": dq_failed[:10]},
        "layers": layers,
        "gathered_at": datetime.now(timezone.utc).isoformat(),
    }
    GATE_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    _evidence_path(run_id, "validation").write_text(json.dumps(pack, indent=2, default=str))
    _log(target, domain, run_id, "validate", "test-manager",
         "completed" if tests_ok and not dq_failed else "failed",
         f"validation pack: tests {'passed' if tests_ok else 'FAILED'}, "
         f"{len(dq_failed)}/{len(dq_rows)} DQ checks failing")
    return _json(pack)


@tool
def accept_validation(domain: str, target: str, run_id: str) -> str:
    """G3 GATE -- records the customer's UAT/validation sign-off. Pauses for human approval
    before it runs. Reads back the pack gather_validation_pack persisted for this run and
    REFUSES if it is missing, or if the regression suite failed, or if any data-quality check
    is failing -- approval is approval OF evidence, so there has to be passing evidence.
    Writes evidence/gates/<run>-G3.md on success.

    Args:
        domain: the domain being signed off
        target: 'duckdb' or 'databricks'
        run_id: the sdlc_run id this action belongs to
    """
    import json
    path = _evidence_path(run_id, "validation")
    if not path.exists():
        return _json({"ok": False, "gate": "G3", "refused": True,
                      "reason": "No validation pack for this run -- call gather_validation_pack first."})
    pack = json.loads(path.read_text())
    if not pack["tests"]["passed"] or pack["dq"]["failed"]:
        _log(target, domain, run_id, "validate", "human+system", "failed",
             f"G3 refused: tests_passed={pack['tests']['passed']}, dq_failing={pack['dq']['failed']}")
        return _json({"ok": False, "gate": "G3", "refused": True,
                      "reason": "Validation evidence is not clean.",
                      "tests_passed": pack["tests"]["passed"], "dq_failing": pack["dq"]["failed"]})
    layers = ", ".join(f"{p}: {v['tables']} tables / {v['rows']} rows" for p, v in pack["layers"].items())
    record = _write_gate_record(
        "G3", "Validation / UAT", run_id, domain,
        approved=[f"Target platform: {target}", f"Layers: {layers}",
                  f"Data-quality checks: {pack['dq']['checked']} evaluated, 0 failing"],
        sections=f"## Tests\nRun:      regression suite on {target}\nPassed:   YES\n",
    )
    _log(target, domain, run_id, "validate", "human+system", "completed",
         f"G3 accepted: validation signed off for domain={domain!r}")
    return _json({"ok": True, "gate": "G3", "record": str(record)})


@tool
def run_ops_readiness(domain: str, target: str, run_id: str) -> str:
    """Ops readiness check for the G4 gate: runs the WHOLE pipeline end to end for this domain
    -- every source to bronze, then silver, then gold -- and reports what each layer holds
    afterwards. Persists the result so accept_go_live can read it back. Ungated, but it does
    write data (re-running an idempotent, domain-scoped pipeline), so expect it to take a while.

    Args:
        domain: the domain to exercise
        target: 'duckdb' or 'databricks'
        run_id: the sdlc_run id this action belongs to
    """
    import json
    from emitters.bronze_loader import run as bronze_run
    from emitters.silver_transform import run as silver_run
    from emitters.gold_transform import run as gold_run

    src_dir = REPO_ROOT / "contracts" / "sources" / domain
    sources = sorted(p.name.replace(".source.yaml", "") for p in src_dir.glob("*.source.yaml"))
    steps, failures = [], []
    for sid in sources:
        try:
            bronze_run(sid, target)
            steps.append({"step": f"bronze:{sid}", "ok": True})
        except Exception as exc:  # noqa: BLE001 -- a readiness check reports failures, doesn't raise them
            steps.append({"step": f"bronze:{sid}", "ok": False, "error": str(exc)})
            failures.append(f"bronze:{sid}")
    for name, fn in (("silver", silver_run), ("gold", gold_run)):
        try:
            fn(domain, target)
            steps.append({"step": name, "ok": True})
        except Exception as exc:  # noqa: BLE001
            steps.append({"step": name, "ok": False, "error": str(exc)})
            failures.append(name)

    from webapp.backend.pipeline import pipeline_flow
    flow = pipeline_flow(target, domain)
    report = {
        "domain": domain, "target": target, "sources": sources,
        "steps": steps, "failures": failures, "ready": not failures,
        "layers": {p: {"tables": L["table_count"], "rows": L["rows"]} for p, L in flow["layers"].items()},
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    GATE_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    _evidence_path(run_id, "readiness").write_text(json.dumps(report, indent=2, default=str))
    _log(target, domain, run_id, "operate", "ops-monitor", "completed" if not failures else "failed",
         f"ops readiness: {len(steps) - len(failures)}/{len(steps)} steps clean")
    return _json(report)


@tool
def accept_go_live(domain: str, target: str, run_id: str) -> str:
    """G4 GATE -- records go-live sign-off, handing the pipeline to the daily schedule and the
    on-call loop. Pauses for human approval before it runs. Reads back the report
    run_ops_readiness persisted for this run and REFUSES if it is missing or if any step of the
    end-to-end run failed. Writes evidence/gates/<run>-G4.md on success.

    Args:
        domain: the domain going live
        target: 'duckdb' or 'databricks'
        run_id: the sdlc_run id this action belongs to
    """
    import json
    path = _evidence_path(run_id, "readiness")
    if not path.exists():
        return _json({"ok": False, "gate": "G4", "refused": True,
                      "reason": "No readiness report for this run -- call run_ops_readiness first."})
    report = json.loads(path.read_text())
    if not report["ready"]:
        _log(target, domain, run_id, "operate", "human+system", "failed",
             f"G4 refused: {len(report['failures'])} failing step(s): {report['failures']}")
        return _json({"ok": False, "gate": "G4", "refused": True,
                      "reason": "The end-to-end readiness run did not come back clean.",
                      "failures": report["failures"]})
    layers = ", ".join(f"{p}: {v['tables']} tables / {v['rows']} rows" for p, v in report["layers"].items())
    record = _write_gate_record(
        "G4", "Go-live readiness", run_id, domain,
        approved=[f"Target platform: {target}",
                  f"End-to-end run: {len(report['steps'])} steps, all clean",
                  f"Sources exercised: {', '.join(report['sources'])}",
                  f"Layers: {layers}"],
        sections="## Rollback\nContracts are versioned in git; re-running is idempotent and domain-scoped.\n",
    )
    _log(target, domain, run_id, "operate", "human+system", "completed",
         f"G4 accepted: {domain!r} handed to the daily schedule on {target}")
    return _json({"ok": True, "gate": "G4", "record": str(record)})


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj, default=str)


ALL_TOOLS = [test_source_connection, profile_source,
             capture_intent, run_gap_analysis,
             compile_intake_preview, write_intake_contracts, run_bronze_source,
             run_silver_domain, run_gold_domain, run_regression_tests,
             accept_catalogue, gather_validation_pack, accept_validation,
             run_ops_readiness, accept_go_live]
