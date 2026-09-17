"""Step 02 intent capture and gap analysis.

Intent: what the customer is actually trying to build this platform FOR -- which reports it
feeds, the SLAs, the definition of done, and where business terms carry more than one
definition across their systems ("whose definition of active customer applies"). Nothing in
the intake workbook captures any of this; `01_Project` is purely technical/environment config.
Persisted to contracts/intent/<domain>/<intent_id>.yaml -- tracked in git, unlike
contracts/discovery/, because a human deliberately authored it; it isn't derived from probing a
live system.

A domain can carry MORE THAN ONE intent -- a real deployment isn't "one report the platform
serves," it's every distinct application/use case that draws on the same underlying data (a
claims dashboard AND an underwriting scorecard AND a regulatory extract, each with its own
owner, SLA and report list). One file per intent, keyed by a slug of its own `name`; a caller
that doesn't give a name lands on "primary" (the pre-multi-intent behavior, unchanged for
existing single-intent callers like the G1 gate tool).

Gap analysis: a MECHANICAL check, not a semantic one, on purpose. Every data point a report
declares it needs is looked up by exact (case-insensitive) name against every column this
platform actually knows about for the domain -- contracted source schemas (approved) and
discovery profiles (Step 01, not yet approved). A name that matches nowhere is an open gap. A
term defined more than once with different wording is an open conflict -- across ALL of a
domain's intents, since two applications sharing one domain still can't disagree about what
"active customer" means. No fuzzy matching, no LLM judgement call about whether two phrasings
mean the same thing -- CLAUDE.md rule 4 (never silently invent/guess) applies exactly as much
here as it does to a DQ rule. A human resolves what this can't decide; this only refuses to hide
that a decision is needed.

Recomputed fresh on every call rather than trusted from the persisted file -- the same reason
every gate tool in agents/jarvis_tools.py re-derives its own evidence: accept_catalogue (G1)
calls run_gap_analysis() directly at approval time, not a stale report from an earlier click.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
import sys
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

INTENT_DIR = REPO_ROOT / "contracts" / "intent"
# gap_analysis.json lives alongside <intent_id>.yaml in the same directory but has a .json
# extension, so list_intents' *.yaml glob never picks it up as an intent record.


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "primary"


# --------------------------------------------------------------------------- intent capture

def _intent_dir(domain: str) -> pathlib.Path:
    return INTENT_DIR / domain


def _intent_path(domain: str, intent_id: str) -> pathlib.Path:
    return _intent_dir(domain) / f"{intent_id}.yaml"


def list_intents(domain: str) -> list[dict[str, Any]]:
    """Every intent captured for this domain, oldest first (stable, predictable ordering for a
    UI list) -- not just the one a legacy single-intent caller happens to load."""
    d = _intent_dir(domain)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.yaml")):
        try:
            record = yaml.safe_load(p.read_text())
        except Exception:  # noqa: BLE001 -- a corrupt file shouldn't blank the whole list
            continue
        if record:
            out.append(record)
    return sorted(out, key=lambda r: r.get("captured_at", ""))


def load_intent(domain: str, intent_id: str | None = None) -> dict[str, Any] | None:
    """With intent_id: that specific intent, or None if it doesn't exist. Without: the first
    intent on record (by capture order) -- the pre-multi-intent behavior every existing caller
    (journey.py's `intent_captured` flag, the G1 gate tool) already relies on, unchanged."""
    if intent_id is not None:
        path = _intent_path(domain, intent_id)
        return yaml.safe_load(path.read_text()) if path.exists() else None
    intents = list_intents(domain)
    return intents[0] if intents else None


def capture_intent(domain: str, client: str, intent: dict[str, Any], captured_by: str,
                    intent_id: str | None = None) -> pathlib.Path:
    """Replaces the live intent record at this intent_id. Deliberately a replace, not a merge -- a
    human revising an intent should see exactly what they submitted, not a silent merge with
    stale fields from an earlier draft. intent_id defaults to a slug of intent['name'] if given,
    else the fixed id "primary" (a caller that's never heard of multi-intent -- e.g. the G1 gate
    tool's existing signature -- always lands on the same single record it always has).

    The replace is no longer destructive: every save is also recorded as a version awaiting
    re-sign (emitters/versions.py), and emitters/intent_change.py reports what the change did."""
    from emitters import versions
    versions.check_id(domain)
    name = (intent.get("name") or "").strip()
    resolved_id = intent_id or (_slugify(name) if name else "primary")
    versions.check_id(resolved_id)
    record = {
        "intent_id": resolved_id, "name": name or resolved_id,
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
    path = _intent_path(domain, resolved_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = _intent_dir(domain)
    history = versions.list_versions(root, resolved_id)
    if not history and path.exists():
        # captured before versioning existed: keep it as v1 so this save's change is visible
        previous = yaml.safe_load(path.read_text()) or {}
        versions.record(root, resolved_id, previous,
                        {"created_by": previous.get("captured_by", "unknown"),
                         "created_at": previous.get("captured_at"), "reason": "baseline",
                         "review": {"status": "pending"}})
        history = [None]
    versions.record(root, resolved_id, record, {
        "created_by": captured_by, "reason": "captured" if not history else "revised",
        "review": {"status": "pending"},
    })
    path.write_text(yaml.safe_dump(record, sort_keys=False, allow_unicode=True))
    return path


def delete_intent(domain: str, intent_id: str) -> bool:
    path = _intent_path(domain, intent_id)
    if not path.exists():
        return False
    path.unlink()
    return True


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


def compute_gaps(intents: list[dict[str, Any]], columns: dict[str, dict[str, list[str]]]) -> list[dict[str, Any]]:
    """Pure: the gap rows for these intents against these known columns. Split out so a change
    can be judged by running it on both sides (the intent before and after, or the columns with
    and without a new source version) without touching what's on disk."""
    gaps = []
    for intent in intents:
        for rep in intent.get("reports", []) or []:
            for dp in rep.get("required_data_points", []) or []:
                hits = _find_data_point(dp, columns)
                gaps.append({
                    "intent": intent.get("name", intent.get("intent_id")),
                    "intent_id": intent.get("intent_id"),
                    "report": rep.get("name", "(unnamed report)"), "data_point": dp,
                    "status": "resolved" if hits else "open",
                    "found_in": hits,
                })
    return gaps


def run_gap_analysis(domain: str) -> dict[str, Any]:
    """The real check: every required_data_point in every report of every intent this domain has
    captured, looked up against every column this platform actually knows about right now.
    Recomputes from disk every call -- contracts/discovery/ and contracts/sources/ can both have
    changed since any intent was captured. Definition conflicts are checked ACROSS every intent
    together, not per-intent -- two applications sharing one domain still can't disagree about
    what "active customer" means, so a term one intent defines one way and another defines a
    different way is exactly the kind of conflict this exists to catch."""
    intents = list_intents(domain)
    if not intents:
        # Nothing to persist -- this branch fires just from a human viewing Step 02 for a
        # domain that hasn't captured any intent yet (journey.py calls run_gap_analysis() on
        # every page load), and writing a file for that would mean every domain anyone ever
        # glanced at gets a contracts/intent/<domain>/ directory with nothing meaningful in it.
        return {"domain": domain, "intent_captured": False, "intents": [], "data_point_gaps": [],
                "definition_conflicts": [], "open_count": 0,
                "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}

    columns = known_columns(domain)
    gaps = compute_gaps(intents, columns)

    conflicts = []
    by_term: dict[str, list[dict[str, str]]] = {}
    for intent in intents:
        for d in intent.get("definitions", []):
            term = (d.get("term") or "").strip().lower()
            if not term:
                continue
            by_term.setdefault(term, []).append({**d, "intent": intent.get("name", intent.get("intent_id"))})
    for term, entries in by_term.items():
        distinct = {(e.get("definition") or "").strip() for e in entries}
        if len(distinct) > 1:
            conflicts.append({"term": entries[0].get("term"), "definitions": entries})

    open_count = sum(1 for g in gaps if g["status"] == "open") + len(conflicts)
    report = {
        "domain": domain, "intent_captured": True,
        "intents": [{"intent_id": i.get("intent_id"), "name": i.get("name")} for i in intents],
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
