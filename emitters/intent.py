"""Step 02 intent capture and gap analysis.

Intent: what the customer is actually trying to build this platform FOR -- which reports it
feeds, the SLAs, the definition of done, and where business terms carry more than one
definition across their systems ("whose definition of active customer applies"). Nothing in
the intake workbook captures any of this; `01_Project` is purely technical/environment config.
Persisted to contracts/intent/<domain>/intent.yaml -- tracked in git, unlike contracts/discovery/,
because a human deliberately authored it; it isn't derived from probing a live system.

Gap analysis: a MECHANICAL check, not a semantic one, on purpose. Every data point a report
declares it needs is looked up by exact (case-insensitive) name against every column this
platform actually knows about for the domain -- contracted source schemas (approved) and
discovery profiles (Step 01, not yet approved). A name that matches nowhere is an open gap. A
term defined more than once with different wording is an open conflict. No fuzzy matching, no
LLM judgement call about whether two phrasings mean the same thing -- CLAUDE.md rule 4 (never
silently invent/guess) applies exactly as much here as it does to a DQ rule. A human resolves
what this can't decide; this only refuses to hide that a decision is needed.

Recomputed fresh on every call rather than trusted from the persisted file -- the same reason
every gate tool in agents/jarvis_tools.py re-derives its own evidence: accept_catalogue (G1)
calls run_gap_analysis() directly at approval time, not a stale report from an earlier click.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

INTENT_DIR = REPO_ROOT / "contracts" / "intent"


# --------------------------------------------------------------------------- intent capture

def _intent_path(domain: str) -> pathlib.Path:
    return INTENT_DIR / domain / "intent.yaml"


def load_intent(domain: str) -> dict[str, Any] | None:
    path = _intent_path(domain)
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text())


def capture_intent(domain: str, client: str, intent: dict[str, Any], captured_by: str) -> pathlib.Path:
    """Overwrites the intent record for this domain. Deliberately a replace, not a merge -- a
    human revising the intent should see exactly what they submitted, not a silent merge with
    stale fields from an earlier draft. Minimal shape validation only: this is a place to
    capture what the customer said, not a place to second-guess it."""
    record = {
        "domain": domain, "client": client,
        "business_outcome": intent.get("business_outcome", ""),
        "definition_of_done": intent.get("definition_of_done", ""),
        "sla": intent.get("sla") or {},
        "reports": intent.get("reports") or [],
        "definitions": intent.get("definitions") or [],
        "stakeholders": intent.get("stakeholders") or [],
        "captured_by": captured_by,
        "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    path = _intent_path(domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(record, sort_keys=False, allow_unicode=True))
    return path


# --------------------------------------------------------------------------- known columns

def _contracted_columns(domain: str) -> dict[str, list[str]]:
    """source_id -> column names, from APPROVED contracts (contracts/sources/<domain>/*.yaml).
    These are facts, not samples -- every column in the contract, not just a sample of them."""
    out: dict[str, list[str]] = {}
    src_dir = REPO_ROOT / "contracts" / "sources" / domain
    if not src_dir.exists():
        return out
    for p in sorted(src_dir.glob("*.source.yaml")):
        contract = yaml.safe_load(p.read_text())
        out[contract["source_id"]] = [c["name"] for c in contract.get("schema", [])]
    return out


def _discovered_columns(domain: str) -> dict[str, list[str]]:
    """source_id -> column names, from Step 01 profiles (contracts/discovery/<domain>/*.json).
    NOT yet approved -- a gap resolved only by a discovered column is real, but the caller
    should be able to tell the two apart (see found_in below)."""
    out: dict[str, list[str]] = {}
    from emitters.profiler import list_profiles
    for p in list_profiles(domain):
        out[p["source_id"]] = [c["name"] for c in p.get("columns", [])]
    return out


def known_columns(domain: str) -> dict[str, dict[str, list[str]]]:
    return {"contracted": _contracted_columns(domain), "discovered": _discovered_columns(domain)}


# --------------------------------------------------------------------------- gap analysis

def _find_data_point(name: str, columns: dict[str, dict[str, list[str]]]) -> list[str]:
    """Every (source, provenance) where a column matches `name` case-insensitively. Exact-name
    matching only -- 'settlement date' and 'settlement_date' do NOT match each other. That is a
    deliberate refusal to guess at a mapping the platform has no basis for, not an oversight."""
    needle = name.strip().lower()
    hits = []
    for provenance, sources in columns.items():
        for source_id, cols in sources.items():
            if any(c.lower() == needle for c in cols):
                hits.append(f"{source_id} ({provenance})")
    return hits


def run_gap_analysis(domain: str) -> dict[str, Any]:
    """The real check: every required_data_point in every report, looked up against every
    column this platform actually knows about right now. Recomputes from disk every call --
    contracts/discovery/ and contracts/sources/ can both have changed since intent was captured."""
    intent = load_intent(domain)
    if intent is None:
        # Nothing to persist -- this branch fires just from a human viewing Step 02 for a
        # domain that hasn't captured intent yet (journey.py calls run_gap_analysis() on every
        # page load), and writing a file for that would mean every domain anyone ever glanced
        # at gets a contracts/intent/<domain>/ directory with nothing meaningful in it.
        return {"domain": domain, "intent_captured": False, "data_point_gaps": [],
                "definition_conflicts": [], "open_count": 0,
                "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}

    columns = known_columns(domain)
    gaps = []
    for rep in intent.get("reports", []):
        for dp in rep.get("required_data_points", []):
            hits = _find_data_point(dp, columns)
            gaps.append({
                "report": rep.get("name", "(unnamed report)"), "data_point": dp,
                "status": "resolved" if hits else "open",
                "found_in": hits,
            })

    conflicts = []
    by_term: dict[str, list[dict[str, str]]] = {}
    for d in intent.get("definitions", []):
        term = (d.get("term") or "").strip().lower()
        if not term:
            continue
        by_term.setdefault(term, []).append(d)
    for term, entries in by_term.items():
        distinct = {(e.get("definition") or "").strip() for e in entries}
        if len(distinct) > 1:
            conflicts.append({"term": entries[0].get("term"), "definitions": entries})

    open_count = sum(1 for g in gaps if g["status"] == "open") + len(conflicts)
    report = {
        "domain": domain, "intent_captured": True,
        "data_point_gaps": gaps, "definition_conflicts": conflicts,
        "open_count": open_count,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    _write_gap_report(domain, report)
    return report


def _write_gap_report(domain: str, report: dict) -> pathlib.Path:
    path = INTENT_DIR / domain / "gap_analysis.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str))
    return path
