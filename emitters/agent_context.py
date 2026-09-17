"""The context layer: what an agent knows about a company before it says a word.

An agent with a model and no context is a well-spoken stranger. This module assembles, per
agent and per company, a context pack from the platform's own records -- the estate scan, the
gap report, the sources and their review state, the landing zone, the intents and their
sign-off, the architecture record, open incident tickets -- and gives every item an id the agent
must cite. Nothing here is generated; each item is a rendering of something measured or signed.

Each agent reads the sections its job needs (AGENT_SECTIONS). The pack has a character budget,
so a large estate can't crowd out everything else: sections are rendered in priority order and
truncated with a note saying so, never silently.

Context is rebuilt on every turn rather than cached in the conversation, so an agent never
answers from what the estate looked like an hour ago.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any, Callable

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SECTION_BUDGET = 2400
PACK_BUDGET = 9000

AGENT_SECTIONS = {
    "detective": ["sources", "landing", "estate", "gaps", "intents"],
    "architect": ["architecture", "estate", "gaps", "intents", "sources"],
    "delivery": ["intents", "gaps", "sources", "architecture", "tickets"],
    "engineer": ["sources", "landing", "estate", "tickets", "gaps"],
    "quality": ["gaps", "estate", "tickets", "sources"],
    "insight": ["intents", "gaps", "estate"],
    "nightwatch": ["tickets", "landing", "sources", "estate"],
}


def _clip(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    return text[:budget].rsplit("\n", 1)[0] + "\n… (truncated to fit the context budget)"


def _estate(domain: str, target: str, wide: bool) -> tuple[str, str]:
    from emitters.estate import build_report
    r = build_report(domain, target, include_unclassified=wide)
    if not r.get("scan"):
        return "Estate scan", f"No estate scan has been run for {domain} on {target} yet."
    c, rel, m = r["coverage"], r["relationships"], r["mirrors"]
    lines = [f"Scan {r['scan']['scan_id']} on {target}, tier {r['scan']['tier_reached']}, finished {r['scan']['ended_at']}.",
             f"{c['schemas']} schemas, {c['tables']} tables, {c['columns']} columns, {c['rows']:,} rows; "
             f"{c['profiled_tables']} profiled, {c['enriched_tables']} described by the model.",
             f"References: {len(rel['intact'])} intact, {len(rel['orphans'])} with orphans, {len(rel['weak'])} weak. "
             f"Copies: {len(m['identical'])} identical, {len(m['diverged'])} diverged. "
             f"Sensitive columns: {r['sensitivity']['total']}."]
    if c.get("unclassified_schemas"):
        lines.append(f"Schemas owned by no company: {', '.join(c['unclassified_schemas'])}.")
    lines.append("Open questions:")
    lines += [f"- [{q['severity']}] {q['question']}" for q in r["open_questions"][:8]]
    return "Estate scan", "\n".join(lines)


def _gaps(domain: str, target: str, wide: bool) -> tuple[str, str]:
    from emitters.gap_report import build_gap_report
    r = build_gap_report(domain, target, include_unclassified=wide)
    if not r["intent_captured"]:
        return "Gap report", "No intent captured yet, so there is nothing to check the estate against."
    s = r["summary"]
    lines = [f"{s['ready']} of {s['data_points']} required data points are ready; {s['gaps']} gaps "
             f"({s['by_severity']['high']} high, {s['by_severity']['medium']} medium)."]
    if r.get("note"):
        lines.append(r["note"])
    for row in r["data_points"]:
        if row["gaps"]:
            g = row["gaps"][0]
            more = f" (+{len(row['gaps']) - 1} more)" if len(row["gaps"]) > 1 else ""
            lines.append(f"- {row['data_point']} for {row['intent']}: {g['label']} [{g['severity']}], owner "
                         f"{g['owner']}. {g['action']}{more}")
    for c in r["definition_conflicts"]:
        lines.append(f"- term “{c['term']}”: {c['gaps'][0]['action']}")
    return "Gap report", "\n".join(lines)


def _sources(domain: str, target: str, wide: bool) -> tuple[str, str]:
    from emitters.profiler import list_profiles
    from emitters.source_review import review_summary
    contracts = sorted(p.name.replace(".source.yaml", "")
                       for p in (REPO_ROOT / "contracts" / "sources" / domain).glob("*.source.yaml")) \
        if (REPO_ROOT / "contracts" / "sources" / domain).exists() else []
    lines = [f"Under contract ({len(contracts)}): {', '.join(contracts) or 'none'}."]
    profiles = list_profiles(domain)
    lines.append(f"Profiled in discovery ({len(profiles)}):")
    for p in profiles:
        rv = review_summary(domain, p["source_id"])
        conn = p.get("connection") or {}
        where = conn.get("path") or conn.get("table") or conn.get("endpoint") or ""
        lines.append(f"- {p['source_id']} ({conn.get('type')}, {where}): {len(p.get('columns', []))} columns, "
                     f"{p.get('total_rows')} rows; v{rv['version'] or '?'} {rv['review_status'] or 'unversioned'}; "
                     f"keys {', '.join(p.get('candidate_keys') or []) or 'none'}")
    return "Sources", "\n".join(lines)


def _landing(domain: str, target: str, wide: bool) -> tuple[str, str]:
    from emitters.landing import inventory
    inv = inventory(domain)
    lines = [f"Layout: {inv['root']}/<structured|unstructured|api|database>/<source>/"]
    for c in inv["categories"]:
        srcs = ", ".join(f"{s['source_id']} ({s['files']} files{', ' + str(s['uploads']) + ' uploaded' if s['uploads'] else ''})"
                         for s in c["sources"]) or "nothing landed"
        lines.append(f"- {c['category']}: {srcs}")
    return "Landing zone", "\n".join(lines)


def _intents(domain: str, target: str, wide: bool) -> tuple[str, str]:
    from emitters.intent import list_intents
    from emitters.intent_change import summary
    intents = list_intents(domain)
    if not intents:
        return "Intents", "No intent captured yet."
    lines = []
    for it in intents:
        sm = summary(domain, it["intent_id"])
        lines.append(f"- {it.get('name')} (v{sm['version'] or '?'}, {sm['review_status'] or 'unversioned'}): "
                     f"{it.get('business_outcome') or 'no outcome stated'}")
        for rep in it.get("reports") or []:
            lines.append(f"  report {rep.get('name')}: needs {', '.join(rep.get('required_data_points') or []) or 'nothing listed'}")
        if it.get("sla"):
            lines.append(f"  SLA {it['sla']}")
        for d in it.get("definitions") or []:
            lines.append(f"  term “{d.get('term')}” = {d.get('definition')}")
    return "Intents", "\n".join(lines)


def _architecture(domain: str, target: str, wide: bool) -> tuple[str, str]:
    from emitters.architecture import load_architecture
    a = load_architecture(domain)
    if not a:
        return "Architecture", "No architecture record captured yet."
    lines = [f"Captured {a.get('captured_at')} by {a.get('captured_by')}. Platform {a.get('platform_binding')}; "
             f"RTO {a.get('rto')}; RPO {a.get('rpo')}.",
             f"Layering: {a.get('layering_rationale')}", f"Volumes: {a.get('volume_expectations')}"]
    lines += [f"- {e.get('entity')}: {e.get('scd_type')}" for e in a.get("entity_scd") or []]
    lines += [f"- risk: {r.get('risk')} / mitigation: {r.get('mitigation')}" for r in a.get("risks") or []]
    return "Architecture", "\n".join(lines)


def _tickets(domain: str, target: str, wide: bool) -> tuple[str, str]:
    from emitters.ops_tickets import list_tickets
    tickets = list_tickets(domain, target)
    open_ = [t for t in tickets if t.get("status") not in ("resolved", "rejected")]
    lines = [f"{len(open_)} open of {len(tickets)} tickets on {target}."]
    for t in open_[:8]:
        lines.append(f"- ticket {t.get('ticket_id')} [{t.get('status')}, {t.get('severity')}] {t.get('phase')} "
                     f"{t.get('entity')}: {t.get('issue_type')} -- {(t.get('description') or '')[:160]}"
                     f"{'; assigned to ' + t['assigned_to'] if t.get('assigned_to') else ''}")
    return "Incident tickets", "\n".join(lines)


_BUILDERS: dict[str, Callable[[str, str, bool], tuple[str, str]]] = {
    "estate": _estate, "gaps": _gaps, "sources": _sources, "landing": _landing,
    "intents": _intents, "architecture": _architecture, "tickets": _tickets,
}
_PREFIX = {"estate": "E", "gaps": "G", "sources": "S", "landing": "L", "intents": "I", "architecture": "A", "tickets": "T"}


def build_context(domain: str, agent: str, target: str = "duckdb", include_unclassified: bool = False,
                  budget: int = PACK_BUDGET) -> list[dict[str, Any]]:
    """The agent's context pack, most important section first. A section that can't be read
    says why instead of disappearing -- "I couldn't see the tickets" is information too."""
    items, used = [], 0
    for section in AGENT_SECTIONS.get(agent, []):
        try:
            title, text = _BUILDERS[section](domain, target, include_unclassified)
            ok = True
        except Exception as exc:  # noqa: BLE001 -- one unreadable record never blanks the whole pack
            title, text, ok = section.title(), f"Unavailable: {type(exc).__name__}: {str(exc)[:160]}", False
        remaining = budget - used
        if remaining <= 200:
            items.append({"id": f"{_PREFIX[section]}1", "section": section, "title": title, "ok": ok,
                          "text": "(left out: the context budget was used by higher-priority sections)", "chars": 0})
            continue
        text = _clip(text, min(SECTION_BUDGET, remaining))
        used += len(text)
        items.append({"id": f"{_PREFIX[section]}1", "section": section, "title": title, "ok": ok,
                      "text": text, "chars": len(text)})
    return items


def render(items: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"[{i['id']}] {i['title']}\n{i['text']}" for i in items)
