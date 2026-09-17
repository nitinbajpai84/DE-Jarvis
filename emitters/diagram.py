"""Step 02 architecture diagrams -- auto-generated from the domain's own contracts, never
hand-drawn and never guessed. A high-level view (source -> bronze -> silver -> gold, real table
counts) and a low-level view (actual silver dimensions/facts, their real columns, and the real
foreign-key relationships declared in contracts/models/<domain>.model.yaml) so someone arriving
at a domain can see where its data actually comes from and how it's actually shaped, without
anyone drawing it by hand or an LLM inventing a plausible-looking box.

Both return {"nodes": [...], "edges": [...]} -- plain structured data, not SVG/markup. The
frontend renders it; this module only reads contracts/ and reports facts.

Deliberately does NOT infer a relationship that isn't declared. asset_management's
fact_awm_position currently has an empty foreign_keys list in its model contract (a real,
honest gap -- see contracts/models/asset_management.model.yaml) -- the low-level diagram shows
that fact with no outgoing edges rather than guessing one from conformance.shared_dimensions,
matching CLAUDE.md rule 4 the same way every other module in this codebase does.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_yaml(path: pathlib.Path) -> dict[str, Any] | None:
    return yaml.safe_load(path.read_text()) if path.exists() else None


def high_level_diagram(domain: str) -> dict[str, Any]:
    """Source contracts -> bronze tables -> silver dims/facts -> gold marts, one node per real
    contracted thing, counted from what's actually on disk right now."""
    sources_dir = REPO_ROOT / "contracts" / "sources" / domain
    sources = sorted(p.stem.removesuffix(".source") for p in sources_dir.glob("*.source.yaml")) if sources_dir.exists() else []
    model = _load_yaml(REPO_ROOT / "contracts" / "models" / f"{domain}.model.yaml") or {}
    gold = _load_yaml(REPO_ROOT / "contracts" / "semantics" / f"{domain}.gold.yaml") or {}
    dims = model.get("dimensions", [])
    facts = model.get("facts", [])
    marts = gold.get("marts", [])

    nodes = []
    edges = []
    for s in sources:
        nodes.append({"id": f"src:{s}", "layer": "source", "label": s})
        nodes.append({"id": f"bronze:{s}", "layer": "bronze", "label": s})
        edges.append({"from": f"src:{s}", "to": f"bronze:{s}"})

    for d in dims:
        nodes.append({"id": f"silver:{d['name']}", "layer": "silver", "label": d["name"], "kind": "dimension"})
        for src in d.get("source", []):
            if any(s == src for s in sources):
                edges.append({"from": f"bronze:{src}", "to": f"silver:{d['name']}"})
    for f in facts:
        nodes.append({"id": f"silver:{f['name']}", "layer": "silver", "label": f["name"], "kind": "fact"})
        for src in f.get("source", []):
            if any(s == src for s in sources):
                edges.append({"from": f"bronze:{src}", "to": f"silver:{f['name']}"})

    for m in marts:
        nodes.append({"id": f"gold:{m['name']}", "layer": "gold", "label": m["name"]})
        source_fact = m.get("source_fact")
        if source_fact:
            edges.append({"from": f"silver:{source_fact}", "to": f"gold:{m['name']}"})
        for jd in m.get("join_dimensions", []) or []:
            edges.append({"from": f"silver:{jd}", "to": f"gold:{m['name']}"})

    return {
        "domain": domain, "nodes": nodes, "edges": edges,
        "counts": {"source": len(sources), "bronze": len(sources), "silver": len(dims) + len(facts), "gold": len(marts)},
    }


def low_level_diagram(domain: str) -> dict[str, Any]:
    """Every silver dimension and fact with its real columns (business/surrogate key for a
    dimension; grain and measures for a fact) and the real foreign_keys the model contract
    declares from fact to dimension -- the actual shape of the conformed layer, not a redrawn
    approximation of it."""
    model = _load_yaml(REPO_ROOT / "contracts" / "models" / f"{domain}.model.yaml") or {}
    dims = model.get("dimensions", [])
    facts = model.get("facts", [])

    nodes = []
    edges = []
    for d in dims:
        nodes.append({
            "id": d["name"], "kind": "dimension", "label": d["name"], "type": d.get("type"),
            "columns": [
                *(f"{k} (business key)" for k in d.get("business_key", [])),
                f"{d.get('surrogate_key')} (surrogate key)" if d.get("surrogate_key") else None,
            ],
        })
        nodes[-1]["columns"] = [c for c in nodes[-1]["columns"] if c]

    for f in facts:
        nodes.append({
            "id": f["name"], "kind": "fact", "label": f["name"], "type": f.get("type"),
            "columns": [
                *(f"{g} (grain)" for g in f.get("grain", [])),
                *(f"{m.get('name')} ({m.get('type', 'measure')})" for m in f.get("measures", []) or []),
            ],
        })
        for fk in f.get("foreign_keys", []) or []:
            dimension, on = fk.get("dimension"), fk.get("on")
            if dimension:
                edges.append({"from": f["name"], "to": dimension, "label": on or ""})

    return {"domain": domain, "nodes": nodes, "edges": edges}
