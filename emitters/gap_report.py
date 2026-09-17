"""The estate-backed gap report: can this company's estate actually serve what its intents ask
for, and if not, what exactly stands in the way and whose job is it.

The G1 gap analysis in emitters/intent.py answers one question, deliberately narrowly: is each
required data point a column in an approved contract or a discovery profile? That is the right
gate check, and it is unchanged. But "found" and "usable" are different things. A column can be
in a contract and 60% empty; it can exist only in a legacy copy that disagrees with the live one;
it can reference keys that resolve to nothing; it can be someone's phone number with no control
recorded against it. And a column that isn't onboarded yet may already be sitting in the estate,
one profile away from resolved.

This report joins every intent's data points against three bodies of evidence -- approved
contracts, reviewed and unreviewed discovery profiles, and the latest estate scan -- and
classifies each obstacle into one of eight gap types. Each gap names its evidence, a severity,
the agent whose job it is, and the concrete next action. Everything here is measured or read
from a record; nothing is inferred by a model, and matching is by exact column name, the same
refusal to guess as emitters/intent.py.

  missing              the data point exists nowhere: no contract, no profile, not in the estate
  not_onboarded        it is in the estate, but no source for it has been profiled or contracted
  unreviewed           it is only in a discovery profile nobody has accepted
  quality              every place it is found, it is too empty to rely on
  integrity            where it is a reference, some of its values resolve to nothing
  conflicting_copies   it is in a table whose copy elsewhere disagrees -- two sources of truth
  sensitive_uncontrolled  personal data with no control recorded in any contract
  definition_conflict  a business term the intents define in more than one way
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
CONTRACTS_DIR = REPO_ROOT / "contracts"

GAP_TYPES = {
    "missing": {"label": "Missing", "owner": "The Data Detective",
                "why": "No contract, profile or estate table has a column of this name."},
    "not_onboarded": {"label": "In the estate, not onboarded", "owner": "The Data Detective",
                      "why": "The estate holds it, but no source for it has been profiled or contracted."},
    "unreviewed": {"label": "Unreviewed", "owner": "The Delivery Lead",
                   "why": "Only found in a discovery profile nobody has accepted."},
    "quality": {"label": "Too empty", "owner": "The Quality Guardian",
                "why": "Everywhere it is found, too many values are empty to rely on."},
    "integrity": {"label": "Broken references", "owner": "The Superstar Data Engineer",
                  "why": "Some of its values point at records that don't exist."},
    "conflicting_copies": {"label": "Conflicting copies", "owner": "The Chief Architect",
                           "why": "It lives in a table whose copy elsewhere holds different data."},
    "sensitive_uncontrolled": {"label": "Personal data without a control", "owner": "The Chief Architect",
                               "why": "Classified as personal data, but no contract marks it as such."},
    "definition_conflict": {"label": "Definition conflict", "owner": "The Delivery Lead",
                            "why": "The intents define this business term in more than one way."},
}
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _contract_columns(domain: str) -> dict[str, list[dict[str, Any]]]:
    """lower(column) -> [{source_id, pii}] from approved contracts."""
    out: dict[str, list[dict[str, Any]]] = {}
    d = CONTRACTS_DIR / "sources" / domain
    for p in sorted(d.glob("*.source.yaml")) if d.exists() else []:
        try:
            c = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 -- an unreadable contract contributes nothing
            continue
        for col in c.get("schema") or c.get("bronze_schema") or []:
            out.setdefault(str(col.get("name", "")).lower(), []).append(
                {"source_id": c.get("source_id", p.stem), "pii": bool(col.get("pii"))})
    return out


def _profile_columns(domain: str) -> dict[str, list[dict[str, Any]]]:
    """lower(column) -> [{source_id, review_status, null_pct}] from live discovery profiles."""
    from emitters.profiler import list_profiles
    from emitters.source_review import review_summary
    out: dict[str, list[dict[str, Any]]] = {}
    for p in list_profiles(domain):
        status = review_summary(domain, p["source_id"]).get("review_status") or "unversioned"
        for col in p.get("columns", []):
            out.setdefault(str(col.get("name", "")).lower(), []).append(
                {"source_id": p["source_id"], "review_status": status, "null_pct": col.get("null_pct")})
    return out


def _estate_evidence(domain: str, target: str, include_unclassified: bool,
                     wanted: set[str]) -> dict[str, Any]:
    """Everything the latest readable estate scan knows about the wanted column names."""
    from emitters import estate
    out: dict[str, Any] = {"scan": None, "columns": {}, "broken_refs": {}, "diverged": {}, "note": None}
    try:
        con, control = estate._con(target, domain)
    except Exception as exc:  # noqa: BLE001
        out["note"] = f"estate unavailable on {target}: {type(exc).__name__}: {str(exc)[:140]}"
        return out
    try:
        scope_sql, scope_params = estate._scope_filter(include_unclassified)
        row = con.execute(
            f"select scan_id, ended_at, tier_reached, scope from {control}.estate_scan "
            f"where domain = ? and target = ? and status = 'completed'{scope_sql} "
            f"order by started_at desc limit 1", [domain, target, *scope_params]).fetchone()
        if row is None:
            out["note"] = (f"No estate scan on {target} yet, so data points were only checked against "
                           f"contracts and profiles. Run a scan on Discovery to check the whole estate.")
            return out
        scan_id = row[0]
        out["scan"] = {"scan_id": scan_id, "ended_at": estate._iso_utc(row[1]), "tier_reached": row[2], "scope": row[3]}
        if not wanted:
            return out
        names = sorted(wanted)
        marks = ", ".join("?" for _ in names)
        tables = {(s, t): cls for s, t, cls in con.execute(
            f"select schema_name, table_name, classification from {control}.estate_table where scan_id = ?",
            [scan_id]).fetchall()}
        for col, schema, table, null_pct, sens, profiled in con.execute(
            f"select lower(column_name), schema_name, table_name, null_pct, sensitivity, profiled "
            f"from {control}.estate_column where scan_id = ? and lower(column_name) in ({marks})",
            [scan_id, *names]).fetchall():
            out["columns"].setdefault(col, []).append({
                "table": f"{schema}.{table}", "null_pct": null_pct if profiled else None,
                "sensitivity": sens, "owned": tables.get((schema, table)) == domain})
        for col, fs, ft, ts, tt, pct, conf in con.execute(
            f"select lower(from_column), from_schema, from_table, to_schema, to_table, overlap_pct, confidence "
            f"from {control}.estate_relationship where scan_id = ? and confidence <> 'intact' "
            f"and lower(from_column) in ({marks})", [scan_id, *names]).fetchall():
            if tables.get((fs, ft)) == domain:
                out["broken_refs"].setdefault(col, []).append(
                    {"table": f"{fs}.{ft}", "references": f"{ts}.{tt}", "resolved_pct": pct, "confidence": conf})
        for schema, table, mirrors, diff, diff_cols in con.execute(
            f"select schema_name, table_name, mirrors, mirror_diff_rows, mirror_diff_columns from {control}.estate_table "
            f"where scan_id = ? and mirror_state = 'diverged'", [scan_id]).fetchall():
            # None = a scan from before per-column comparison: the whole table is suspect
            cols = None if diff_cols is None else {c.lower() for c in diff_cols.split(",") if c}
            out["diverged"][f"{schema}.{table}"] = {"mirrors": mirrors, "differing_rows": diff, "columns": cols}
            out["diverged"][mirrors] = {"mirrors": f"{schema}.{table}", "differing_rows": diff, "columns": cols}
    except Exception as exc:  # noqa: BLE001 -- the report stands on contracts and profiles; say why
        out["note"] = f"estate lookup failed: {type(exc).__name__}: {str(exc)[:140]}"
    finally:
        con.close()
    return out


def _gap(kind: str, severity: str, action: str, evidence: Any = None) -> dict[str, Any]:
    t = GAP_TYPES[kind]
    return {"type": kind, "label": t["label"], "severity": severity, "owner": t["owner"],
            "why": t["why"], "action": action, "evidence": evidence}


def build_gap_report(domain: str, target: str = "duckdb", include_unclassified: bool = False,
                     null_threshold: float = 20.0) -> dict[str, Any]:
    from emitters.estate import _classify_column
    from emitters.intent import list_intents
    intents = list_intents(domain)
    generated = _dt.datetime.now(_dt.timezone.utc).isoformat()
    if not intents:
        return {"domain": domain, "target": target, "intent_captured": False, "generated_at": generated,
                "gap_types": GAP_TYPES, "data_points": [], "summary": {}, "intents": []}

    points = []
    for it in intents:
        for rep in it.get("reports") or []:
            for dp in rep.get("required_data_points") or []:
                points.append((it.get("name") or it.get("intent_id"), it.get("intent_id"),
                               rep.get("name") or "(unnamed report)", str(dp).strip()))
    wanted = {p[3].lower() for p in points}
    contracted = _contract_columns(domain)
    profiled = _profile_columns(domain)
    est = _estate_evidence(domain, target, include_unclassified, wanted)

    rows = []
    for intent_name, intent_id, report, dp in points:
        key = dp.lower()
        c_hits = contracted.get(key, [])
        p_hits = profiled.get(key, [])
        e_hits = est["columns"].get(key, [])
        gaps = []

        if not c_hits and not p_hits and not e_hits:
            where = "contracts, profiles or the estate scan" if est["scan"] else "contracts or profiles (no estate scan yet)"
            gaps.append(_gap("missing", "high",
                             f"Find the system that holds `{dp}`. It isn't in any of {where}."))
        elif not c_hits and not p_hits:
            tables = sorted({h["table"] for h in e_hits})
            gaps.append(_gap("not_onboarded", "medium",
                             f"Profile {', '.join(tables[:2])}{' and others' if len(tables) > 2 else ''} "
                             f"on Discovery to bring `{dp}` in.", {"estate_tables": tables}))
        elif not c_hits and p_hits and not any(h["review_status"] == "accepted" for h in p_hits):
            srcs = sorted({h["source_id"] for h in p_hits})
            gaps.append(_gap("unreviewed", "medium",
                             f"Review and accept {', '.join(srcs)} on Discovery, or reject it and find a better source.",
                             {"profiles": p_hits}))

        # quality: the best available measurement everywhere it is found
        measured = [h["null_pct"] for h in p_hits if h.get("null_pct") is not None] + \
                   [h["null_pct"] for h in e_hits if h.get("null_pct") is not None]
        if measured and min(measured) >= null_threshold:
            best = min(measured)
            gaps.append(_gap("quality", "high" if best >= 50 else "medium",
                             f"Even the best source of `{dp}` is {best}% empty. Ask its owner whether that is "
                             f"expected, or where the complete values live.",
                             {"best_null_pct": best, "threshold_pct": null_threshold}))

        refs = est["broken_refs"].get(key, [])
        if refs:
            worst = min(r["resolved_pct"] for r in refs)
            gaps.append(_gap("integrity", "high" if worst < 95 else "medium",
                             f"{round(100 - worst, 2)}% of `{dp}` values reference nothing. Fix at source or "
                             f"agree how reports treat those rows.", {"references": refs}))

        clashes = [{"table": h["table"], "mirrors": est["diverged"][h["table"]]["mirrors"],
                    "differing_rows": est["diverged"][h["table"]]["differing_rows"]}
                   for h in e_hits if h["table"] in est["diverged"]
                   and (est["diverged"][h["table"]]["columns"] is None or key in est["diverged"][h["table"]]["columns"])]
        if clashes:
            gaps.append(_gap("conflicting_copies", "high",
                             f"Decide which copy of {clashes[0]['table']} is the source of truth for `{dp}`.",
                             {"copies": clashes}))

        sensitivity = next((h["sensitivity"] for h in e_hits if h.get("sensitivity")), None) or _classify_column(dp)
        if sensitivity and not any(h["pii"] for h in c_hits):
            gaps.append(_gap("sensitive_uncontrolled", "medium",
                             f"`{dp}` looks like {sensitivity} data. Mark it pii in its contract and agree "
                             f"masking before it reaches gold.", {"classification": sensitivity}))

        gaps.sort(key=lambda g: _SEVERITY_ORDER[g["severity"]])
        rows.append({
            "intent": intent_name, "intent_id": intent_id, "report": report, "data_point": dp,
            "found_in": {"contracts": sorted({h["source_id"] for h in c_hits}),
                         "profiles": [{"source_id": h["source_id"], "review_status": h["review_status"]} for h in p_hits],
                         "estate": sorted({h["table"] for h in e_hits})},
            "gaps": gaps, "ready": not gaps,
        })

    from emitters.intent import run_gap_analysis
    conflicts = []
    for c in run_gap_analysis(domain).get("definition_conflicts", []):
        conflicts.append({"term": c["term"], "gaps": [_gap(
            "definition_conflict", "high",
            f"Agree one definition of “{c['term']}” across {len({d.get('intent') for d in c['definitions']})} intents.",
            {"definitions": c["definitions"]})]})

    all_gaps = [g for r in rows for g in r["gaps"]] + [g for c in conflicts for g in c["gaps"]]
    summary = {
        "data_points": len(rows), "ready": sum(1 for r in rows if r["ready"]),
        "gaps": len(all_gaps),
        "by_type": {k: sum(1 for g in all_gaps if g["type"] == k) for k in GAP_TYPES},
        "by_severity": {s: sum(1 for g in all_gaps if g["severity"] == s) for s in _SEVERITY_ORDER},
        "by_owner": {},
    }
    for g in all_gaps:
        summary["by_owner"][g["owner"]] = summary["by_owner"].get(g["owner"], 0) + 1
    per_intent = []
    for it in intents:
        name = it.get("name") or it.get("intent_id")
        mine = [r for r in rows if r["intent"] == name]
        per_intent.append({"intent": name, "intent_id": it.get("intent_id"), "data_points": len(mine),
                           "ready": sum(1 for r in mine if r["ready"]),
                           "score_pct": round(100 * sum(1 for r in mine if r["ready"]) / len(mine), 1) if mine else None})
    rows.sort(key=lambda r: (r["ready"], min((_SEVERITY_ORDER[g["severity"]] for g in r["gaps"]), default=9)))
    return {"domain": domain, "target": target, "intent_captured": True, "generated_at": generated,
            "estate_scan": est["scan"], "note": est["note"], "null_threshold_pct": null_threshold,
            "gap_types": GAP_TYPES, "summary": summary, "intents": per_intent,
            "data_points": rows, "definition_conflicts": conflicts}
