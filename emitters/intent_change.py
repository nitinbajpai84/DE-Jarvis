"""Step 02: what happens when the intent changes.

Saving a revised intent used to replace the file, and that was all -- the gap analysis, the
architecture record and the sources feeding it carried on as if nothing had moved. This module
answers, for any intent version against the one before it:

  What changed     reports added or removed, data points added or removed per report,
                   definitions, SLA, outcome and definition of done
  What it did      gap analysis run on both sides -- which data points the change opened, which
                   it closed, which it simply dropped
  Where to look    for every data point this intent now needs and no profiled or contracted
                   source has: exact-name matches in the latest estate scan. Mechanical, not a
                   guess -- the same exact-name rule gap analysis uses, pointed at the whole
                   estate instead of only the sources already onboarded
  What's stale     the architecture record, if it was captured before this change

and records the human re-signing the new version. Prior versions are kept (emitters/versions.py).
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters import intent as intent_mod, versions  # noqa: E402

_TEXT_FIELDS = ("name", "business_outcome", "definition_of_done")


def _root(domain: str) -> pathlib.Path:
    return intent_mod.INTENT_DIR / versions.check_id(domain)


def _points(intent: dict[str, Any] | None) -> dict[str, set[str]]:
    return {(r.get("name") or "(unnamed report)"): {str(dp).strip() for dp in (r.get("required_data_points") or [])}
            for r in ((intent or {}).get("reports") or [])}


def diff_intents(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    if old is None:
        return {"first_version": True}
    op, np_ = _points(old), _points(new)
    per_report = []
    for name in sorted(set(op) & set(np_)):
        added, removed = sorted(np_[name] - op[name]), sorted(op[name] - np_[name])
        if added or removed:
            per_report.append({"report": name, "data_points_added": added, "data_points_removed": removed})
    odefs = {(d.get("term") or "").strip().lower(): d for d in old.get("definitions") or []}
    ndefs = {(d.get("term") or "").strip().lower(): d for d in new.get("definitions") or []}
    out = {
        "first_version": False,
        "fields_changed": [f for f in _TEXT_FIELDS if (old.get(f) or "") != (new.get(f) or "")],
        "sla_changed": (old.get("sla") or {}) != (new.get("sla") or {}),
        "reports_added": [{"report": n, "data_points": sorted(np_[n])} for n in sorted(set(np_) - set(op))],
        "reports_removed": [{"report": n, "data_points": sorted(op[n])} for n in sorted(set(op) - set(np_))],
        "reports_changed": per_report,
        "definitions_added": sorted(ndefs[t].get("term") for t in set(ndefs) - set(odefs)),
        "definitions_removed": sorted(odefs[t].get("term") for t in set(odefs) - set(ndefs)),
        "definitions_reworded": sorted(ndefs[t].get("term") for t in set(ndefs) & set(odefs)
                                       if (ndefs[t].get("definition") or "").strip() != (odefs[t].get("definition") or "").strip()),
    }
    out["unchanged"] = not any([out["fields_changed"], out["sla_changed"], out["reports_added"],
                                out["reports_removed"], per_report, out["definitions_added"],
                                out["definitions_removed"], out["definitions_reworded"]])
    return out


def _gap_delta(domain: str, intent_id: str, old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Gap analysis for this intent before and after, against the columns known right now --
    so the delta is the intent's own change, not a source that moved in the meantime."""
    columns = intent_mod.known_columns(domain)
    before = intent_mod.compute_gaps([old], columns) if old else []
    after = intent_mod.compute_gaps([new], columns)
    key = lambda g: (g["report"], g["data_point"].strip().lower())  # noqa: E731
    was = {key(g): g for g in before}
    now = {key(g): g for g in after}
    row = lambda g: {"report": g["report"], "data_point": g["data_point"], "found_in": g["found_in"]}  # noqa: E731
    return {
        "newly_open": [row(g) for k, g in now.items() if g["status"] == "open" and was.get(k, {}).get("status") != "open"],
        "newly_resolved": [row(g) for k, g in now.items() if g["status"] == "resolved" and k in was and was[k]["status"] == "open"],
        "no_longer_needed": [row(g) for k, g in was.items() if k not in now],
        "still_open": [row(g) for k, g in now.items() if g["status"] == "open" and was.get(k, {}).get("status") == "open"],
        "open_now": sum(1 for g in after if g["status"] == "open"),
        "open_before": sum(1 for g in before if g["status"] == "open") if old else None,
    }


def _estate_leads(domain: str, target: str, include_unclassified: bool, names: list[str]) -> dict[str, Any]:
    """Exact-name matches for open data points in the latest estate scan this viewer may read."""
    if not names:
        return {"scan_id": None, "matches": {}, "note": None}
    from emitters import estate
    try:
        con, control = estate._con(target, domain)
        try:
            scope_sql, scope_params = estate._scope_filter(include_unclassified)
            row = con.execute(
                f"select scan_id from {control}.estate_scan where domain = ? and target = ? "
                f"and status = 'completed'{scope_sql} order by started_at desc limit 1",
                [domain, target, *scope_params]).fetchone()
            if row is None:
                return {"scan_id": None, "matches": {}, "note": f"No estate scan on {target} yet. Run one on Discovery to search the whole estate for these."}
            wanted = sorted({n.strip().lower() for n in names})
            marks = ", ".join("?" for _ in wanted)
            hits = con.execute(
                f"select lower(column_name), schema_name, table_name, null_pct from {control}.estate_column "
                f"where scan_id = ? and lower(column_name) in ({marks}) order by schema_name, table_name",
                [row[0], *wanted]).fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 -- the change report stands without leads; say why
        return {"scan_id": None, "matches": {}, "note": f"estate lookup unavailable: {type(exc).__name__}: {str(exc)[:160]}"}
    matches: dict[str, list[dict[str, Any]]] = {}
    for col, schema, table, null_pct in hits:
        matches.setdefault(col, []).append({"table": f"{schema}.{table}", "null_pct": null_pct})
    return {"scan_id": row[0], "matches": matches, "note": None}


def _load_version(domain: str, intent_id: str, version: int) -> dict[str, Any]:
    return versions.load(_root(domain), intent_id, version)["artifact"]


def change_view(domain: str, intent_id: str, version: int | None = None, target: str = "duckdb",
                include_unclassified: bool = False) -> dict[str, Any]:
    root = _root(domain)
    versions.check_id(intent_id)
    history = versions.list_versions(root, intent_id)
    if not history:
        live = intent_mod.load_intent(domain, intent_id)
        if live is None:
            raise FileNotFoundError(f"no intent {intent_id!r} for {domain}")
        # captured before versioning and never re-saved: nothing to compare yet
        return {"intent_id": intent_id, "history": [], "meta": None, "intent": live,
                "diff": {"first_version": True}, "gaps": _gap_delta(domain, intent_id, None, live),
                "estate": _estate_leads(domain, target, include_unclassified, []), "stale": []}
    meta = next((m for m in history if m["version"] == version), None) if version else history[-1]
    meta = meta or history[-1]
    current = _load_version(domain, intent_id, meta["version"])
    earlier = [m for m in history if m["version"] < meta["version"]]
    prev = _load_version(domain, intent_id, earlier[-1]["version"]) if earlier else None

    gaps = _gap_delta(domain, intent_id, prev, current)
    open_names = [g["data_point"] for g in gaps["newly_open"] + gaps["still_open"]]
    leads = _estate_leads(domain, target, include_unclassified, open_names)

    stale = []
    from emitters.architecture import load_architecture
    arch = load_architecture(domain)
    if arch and prev is not None and str(arch.get("captured_at") or "") < str(meta.get("created_at") or ""):
        stale.append({"artifact": "architecture",
                      "why": "captured before this intent change. Check the RTO/RPO, layering and SCD choices still fit before G2."})
    return {
        "intent_id": intent_id, "history": history, "meta": meta,
        "compared_with": earlier[-1]["version"] if earlier else None,
        "intent": current, "diff": diff_intents(prev, current),
        "gaps": gaps, "estate": leads, "stale": stale,
        "live": intent_mod.load_intent(domain, intent_id) is not None,
    }


def sign(domain: str, intent_id: str, version: int, by: str, comment: str = "") -> dict[str, Any]:
    """Re-signing is only meaningful for the version that is actually live -- signing an old one
    would record approval of something no longer in force."""
    root = _root(domain)
    versions.check_id(intent_id)
    history = versions.list_versions(root, intent_id)
    if not history:
        raise FileNotFoundError(f"no versions of {intent_id!r} to sign")
    if int(version) != history[-1]["version"]:
        raise ValueError(f"v{version} is no longer the current version (v{history[-1]['version']}). Review the current one.")
    meta = versions.update_meta(root, intent_id, int(version),
                                review={"status": "signed", "by": by, "at": versions.utcnow_iso(),
                                        "comment": comment.strip()})
    return {"ok": True, "version": meta["version"], "review": meta["review"]}


def summary(domain: str, intent_id: str) -> dict[str, Any]:
    history = versions.list_versions(_root(domain), intent_id)
    last = history[-1] if history else None
    return {"version": last["version"] if last else None, "versions": len(history),
            "review_status": (last or {}).get("review", {}).get("status")}
