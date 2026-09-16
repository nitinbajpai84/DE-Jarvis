"""The journey the Control Room renders: agents/gates.py's registry, plus what is actually
true for this domain right now.

The registry says what the journey IS; this module says where the customer currently stands in
it, and every number here is observed, not asserted -- source contracts counted on disk, rows
counted in the live tables, gate records read from evidence/gates/. A step with no real signal
behind it reports `observed: null` rather than a plausible-looking zero, because "we haven't
built this yet" and "this is built and empty" have to look different to someone being walked
through the product.
"""
from __future__ import annotations

import pathlib
import re
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.gates import journey_view  # noqa: E402
from webapp.backend import pipeline  # noqa: E402

GATE_DIR = REPO_ROOT / "evidence" / "gates"
_GATE_FILE = re.compile(r"^(?P<run>[0-9a-f]{8})-(?P<gate>G\d)\.md$")


def _gate_records(domain: str) -> dict[str, list[dict[str, str]]]:
    """Gate records this domain has actually passed. Agent-written records are named
    <run8>-<gate>.md and carry Run/Domain/Date headers; the hand-written P1-* records from the
    project's own design phases don't match that pattern and are left out on purpose -- they
    document building Jarvis, not onboarding this customer."""
    found: dict[str, list[dict[str, str]]] = {}
    if not GATE_DIR.exists():
        return found
    for path in sorted(GATE_DIR.glob("*.md")):
        m = _GATE_FILE.match(path.name)
        if not m:
            continue
        head = path.read_text()[:600]
        rec_domain = next((l.split(":", 1)[1].strip() for l in head.splitlines()
                           if l.startswith("Domain:")), None)
        if rec_domain != domain:
            continue
        date = next((l.split(":", 1)[1].strip() for l in head.splitlines()
                     if l.startswith("Date:")), "")
        found.setdefault(m.group("gate"), []).append(
            {"run": m.group("run"), "date": date, "file": str(path.relative_to(REPO_ROOT))}
        )
    return found


def _source_contracts(domain: str) -> list[str]:
    d = REPO_ROOT / "contracts" / "sources" / domain
    return sorted(p.name.replace(".source.yaml", "") for p in d.glob("*.source.yaml")) if d.exists() else []


def _observed(step_id: str, domain: str, target: str) -> dict[str, Any] | None:
    """Real state per step, or None where the capability doesn't exist yet to produce any."""
    if step_id == "onboard":
        sources_root = REPO_ROOT / "contracts" / "sources"
        domains = sorted(p.name for p in sources_root.iterdir() if p.is_dir()) if sources_root.exists() else []
        return {"domains": domains, "current": domain}

    if step_id == "sources":
        # Read-only inventory of what's already under contract. NOT the same thing as the
        # discovery this step is meant to do -- flagged as such so the UI can say so.
        return {"contracted_sources": _source_contracts(domain), "discovered_sources": None}

    if step_id == "catalogue":
        model = REPO_ROOT / "contracts" / "models" / f"{domain}.model.yaml"
        gold = REPO_ROOT / "contracts" / "semantics" / f"{domain}.gold.yaml"
        return {"source_contracts": len(_source_contracts(domain)),
                "model_contract": model.exists(), "gold_contract": gold.exists(),
                "intent_captured": None}

    if step_id == "build":
        flow = pipeline.pipeline_flow(target, domain)
        return {"layers": {p: {"tables": L["table_count"], "rows": L["rows"], "status": L["status"]}
                           for p, L in flow["layers"].items()}}

    if step_id == "validate":
        con, control = None, None
        try:
            platform = pipeline.load_platform(target)
            from emitters.control_plane import ensure_control_schema
            from emitters.sql_dialect import connect as sql_connect, resolve_schema
            control = resolve_schema(platform, domain, "control")
            con = sql_connect(target, platform)
            ensure_control_schema(con, control)
            rows = con.execute(
                f"select count(*), sum(case when passed then 0 else 1 end) "
                f"from {control}.dq_results where domain = ?", [domain]).fetchone()
        finally:
            if con is not None:
                con.close()
        checked = rows[0] or 0
        return {"dq_checked": checked, "dq_failing": rows[1] or 0,
                "generated_tests": None, "dashboards": None}

    if step_id == "operate":
        alerts = pipeline.recent_alerts(target, domain=domain)
        return {"failures": len(alerts["failures"]),
                "latest_digest_date": (alerts["latest_digest"] or {}).get("date"),
                "open_tickets": None}

    return None


def journey(domain: str, target: str = "duckdb") -> dict[str, Any]:
    view = journey_view()
    records = _gate_records(domain)
    steps = []
    for step in view["steps"]:
        try:
            observed = _observed(step["id"], domain, target)
        except Exception as exc:  # noqa: BLE001 -- one unreachable layer must not blank the whole journey
            observed = {"error": str(exc)}
        gates = [{**g, "records": records.get(g["id"], []),
                  "passed": bool(records.get(g["id"]))} for g in step["gates"]]
        steps.append({**step, "observed": observed, "gates": gates})
    return {"domain": domain, "target": target, "agents": view["agents"], "steps": steps}
