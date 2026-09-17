"""Proposals: the one way an agent gets to change anything.

An agent in conversation can only read (emitters/agent_brain.py). When it thinks something should
change -- an intent needs a data point added, a source version should be accepted, a scan should
be run, a ticket should go to someone -- it files a proposal instead. A proposal is a typed,
inspectable record: what would be done, with which exact parameters, why, and citing what. It sits
in the control plane until a person approves or declines it, and only an approval runs it -- via
the same functions the Control Room's own buttons call, under the approver's name.

The kinds are a closed list (KINDS). Each has a schema the parameters are checked against when
the proposal is filed, a preview of what applying it would do, and an executor. There is no
"run arbitrary action" kind; an agent that wants something outside this list can only recommend
it in words (kind "recommendation"), which a person acts on themselves.

Why not let a confident agent just do it: the platform's gates exist because a plausible-looking
change is exactly the kind that goes wrong quietly. A proposal makes the agent's judgement visible
at the moment it matters, and keeps the person's decision on record next to it.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys
import threading
import uuid
from typing import Any, Callable

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters.control_plane import ensure_control_schema  # noqa: E402
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402

_DDL = """create table if not exists {c}.agent_proposal (
    proposal_id varchar primary key, domain varchar, target varchar, agent varchar, kind varchar,
    title varchar, rationale varchar, params varchar, evidence varchar, session_id varchar,
    status varchar, proposed_at timestamp, decided_by varchar, decided_at timestamp,
    decision_note varchar, result varchar, preview varchar)"""
# added after the first live table existed; check-then-alter like estate._MIGRATIONS
_MIGRATIONS = [("preview", "varchar")]
_READY: set[tuple[str, str]] = set()
_LOCK = threading.Lock()
_COLS = ["proposal_id", "domain", "target", "agent", "kind", "title", "rationale", "params", "evidence",
         "session_id", "status", "proposed_at", "decided_by", "decided_at", "decision_note", "result", "preview"]

STATUSES = ("proposed", "approved", "declined", "failed")


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _iso(v: Any) -> Any:
    return v.isoformat() + "Z" if isinstance(v, _dt.datetime) else v


def _con(domain: str):
    import yaml
    platform = yaml.safe_load((REPO_ROOT / "contracts" / "platform" / "duckdb.yaml").read_text())
    control = resolve_schema(platform, domain, "control")
    con = sql_connect("duckdb", platform)
    if ("duckdb", control) not in _READY:
        with _LOCK:
            if ("duckdb", control) not in _READY:
                ensure_control_schema(con, control)
                con.execute(_DDL.format(c=control))
                cols = {d[0].lower() for d in con.execute(f"select * from {control}.agent_proposal limit 0").description}
                for column, ddl_type in _MIGRATIONS:
                    if column not in cols:
                        con.execute(f"alter table {control}.agent_proposal add column {column} {ddl_type}")
                _READY.add(("duckdb", control))
    return con, control


# --------------------------------------------------------------------------- kinds
#
# Each kind: the parameters it takes (name -> (type, required)), a preview of what approving it
# would do, and the executor. Executors call the same functions the Control Room's buttons do.

def _req(params: dict[str, Any], schema: dict[str, tuple[type, bool]]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for name, (typ, required) in schema.items():
        v = params.get(name)
        if v is None or v == "" or v == []:
            if required:
                raise ValueError(f"'{name}' is required")
            continue
        if typ is list and isinstance(v, str):
            v = [x.strip() for x in v.split(",") if x.strip()]
        if not isinstance(v, typ):
            raise ValueError(f"'{name}' must be {typ.__name__}")
        clean[name] = v
    extra = set(params) - set(schema)
    if extra:
        raise ValueError(f"unexpected parameter(s): {', '.join(sorted(extra))}")
    return clean


def _intent_preview(domain: str, p: dict[str, Any]) -> str:
    from emitters.intent import load_intent
    it = load_intent(domain, p.get("intent_id")) if p.get("intent_id") else None
    if it:
        rep = next((r for r in it.get("reports") or [] if r.get("name") == p["report"]), None)
        have = set(rep.get("required_data_points") or []) if rep else set()
        new = [d for d in p["data_points"] if d not in have]
        return (f"Add {', '.join(new) or 'nothing new'} to report “{p['report']}” of intent "
                f"“{it.get('name')}” ({'existing' if rep else 'new'} report). A new intent version is "
                f"recorded and needs sign-off.")
    return (f"Create intent “{p.get('name') or p.get('intent_id')}” with report “{p['report']}” "
            f"needing {', '.join(p['data_points'])}.")


def _intent_apply(domain: str, target: str, p: dict[str, Any], by: str, wide: bool) -> dict[str, Any]:
    from emitters.intent import capture_intent, load_intent
    it = load_intent(domain, p.get("intent_id")) if p.get("intent_id") else None
    if it is None and not p.get("name"):
        raise ValueError("either an existing intent_id or a name for a new intent is needed")
    record = dict(it or {"name": p["name"], "business_outcome": p.get("business_outcome", ""), "reports": [], "definitions": []})
    reports = [dict(r) for r in record.get("reports") or []]
    rep = next((r for r in reports if r.get("name") == p["report"]), None)
    if rep is None:
        rep = {"name": p["report"], "description": p.get("description", ""), "required_data_points": []}
        reports.append(rep)
    rep["required_data_points"] = list(dict.fromkeys(list(rep.get("required_data_points") or []) + p["data_points"]))
    record["reports"] = reports
    path = capture_intent(domain, record.get("client", "default"), record, by, intent_id=it["intent_id"] if it else None)
    return {"intent_id": path.stem, "report": p["report"], "data_points": rep["required_data_points"]}


def _arch_preview(domain: str, p: dict[str, Any]) -> str:
    from emitters.architecture import load_architecture
    a = load_architecture(domain)
    fields = {k: v for k, v in p.items()}
    return (f"{'Update' if a else 'Create'} the architecture record: set {', '.join(f'{k} = {v!r}' for k, v in fields.items())}."
            + (" Other fields are kept as they are." if a else ""))


def _arch_apply(domain: str, target: str, p: dict[str, Any], by: str, wide: bool) -> dict[str, Any]:
    from emitters.architecture import capture_architecture, load_architecture
    a = dict(load_architecture(domain) or {})
    if "add_risk" in p:
        a["risks"] = list(a.get("risks") or []) + [{"risk": p["add_risk"], "mitigation": p.get("mitigation", "")}]
    if "entity" in p:
        scd = [e for e in (a.get("entity_scd") or []) if e.get("entity") != p["entity"]]
        a["entity_scd"] = scd + [{"entity": p["entity"], "scd_type": p["scd_type"]}]
    for k in ("rto", "rpo", "platform_binding", "layering_rationale", "volume_expectations"):
        if k in p:
            a[k] = p[k]
    capture_architecture(domain, a.get("client", "default"), a, by)
    return {"updated": sorted(p)}


def _scan_preview(domain: str, p: dict[str, Any]) -> str:
    return f"Run an estate scan of {domain} on {p.get('target', 'duckdb')} to tier {p.get('tiers', 4)}. Nothing is changed; the report is refreshed."


def _scan_apply(domain: str, target: str, p: dict[str, Any], by: str, wide: bool) -> dict[str, Any]:
    from webapp.backend import estate
    # scope comes from the person approving, never from the proposal: an agent in a company's
    # conversation must not be able to widen a scan to admin-only schemas by asking for it
    return estate.start_scan(domain, p.get("target") or target, int(p.get("tiers", 4)), include_unclassified=wide)


def _review_preview(domain: str, p: dict[str, Any]) -> str:
    return (f"{'Accept' if p['decision'] == 'accept' else 'Reject'} version {p['version']} of source "
            f"“{p['source_id']}”" + (f" -- {p['comment']}" if p.get("comment") else "") + ".")


def _review_apply(domain: str, target: str, p: dict[str, Any], by: str, wide: bool) -> dict[str, Any]:
    from emitters.source_review import decide
    return decide(domain, p["source_id"], int(p["version"]), p["decision"], by, p.get("comment", ""))


def _ticket_preview(domain: str, p: dict[str, Any]) -> str:
    return f"Assign ticket {p['ticket_id']} to {p['assigned_to']}."


def _ticket_apply(domain: str, target: str, p: dict[str, Any], by: str, wide: bool) -> dict[str, Any]:
    from emitters.ops_tickets import assign_ticket
    return assign_ticket(domain, target, int(p["ticket_id"]), p["assigned_to"])


def _reco_preview(domain: str, p: dict[str, Any]) -> str:
    return f"Nothing runs automatically. Approving records that the team accepts this recommendation for {p.get('owner', 'someone')} to act on."


def _reco_apply(domain: str, target: str, p: dict[str, Any], by: str, wide: bool) -> dict[str, Any]:
    return {"accepted": True, "owner": p.get("owner")}


KINDS: dict[str, dict[str, Any]] = {
    "add_intent_data_points": {
        "label": "Add data points to an intent",
        "schema": {"intent_id": (str, False), "name": (str, False), "business_outcome": (str, False),
                   "report": (str, True), "description": (str, False), "data_points": (list, True),
                   "new_report": (bool, False)},
        "preview": _intent_preview, "apply": _intent_apply,
    },
    "update_architecture": {
        "label": "Update the architecture record",
        "schema": {"rto": (str, False), "rpo": (str, False), "platform_binding": (str, False),
                   "layering_rationale": (str, False), "volume_expectations": (str, False),
                   "entity": (str, False), "scd_type": (str, False), "add_risk": (str, False), "mitigation": (str, False)},
        "preview": _arch_preview, "apply": _arch_apply,
    },
    "run_estate_scan": {
        "label": "Run an estate scan",
        "schema": {"target": (str, False), "tiers": (int, False)},
        "preview": _scan_preview, "apply": _scan_apply,
    },
    "review_source_version": {
        "label": "Accept or reject a source version",
        "schema": {"source_id": (str, True), "version": (int, True), "decision": (str, True), "comment": (str, False)},
        "preview": _review_preview, "apply": _review_apply,
    },
    "assign_ticket": {
        "label": "Assign an incident ticket",
        "schema": {"ticket_id": (int, True), "assigned_to": (str, True)},
        "preview": _ticket_preview, "apply": _ticket_apply,
    },
    "recommendation": {
        "label": "Recommendation",
        "schema": {"owner": (str, False), "steps": (list, False)},
        "preview": _reco_preview, "apply": _reco_apply,
    },
}


def validate(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown proposal kind {kind!r}; one of {', '.join(KINDS)}")
    clean = _req(params or {}, KINDS[kind]["schema"])
    if kind == "review_source_version" and clean["decision"] not in ("accept", "reject"):
        raise ValueError("decision must be accept or reject")
    if kind == "review_source_version" and clean["decision"] == "reject" and not clean.get("comment"):
        raise ValueError("a rejection needs a comment saying why")
    if kind == "update_architecture" and not clean:
        raise ValueError("nothing to update")
    if kind == "update_architecture" and ("entity" in clean) != ("scd_type" in clean):
        raise ValueError("entity and scd_type go together")
    if kind == "run_estate_scan" and clean.get("tiers") not in (None, 1, 2, 3, 4):
        raise ValueError("tiers must be 1-4")
    if kind == "run_estate_scan" and clean.get("target") not in (None, "duckdb", "databricks"):
        raise ValueError("target must be duckdb or databricks")
    return clean


# --------------------------------------------------------------------------- records

def _row(r) -> dict[str, Any]:
    d = dict(zip(_COLS, r))
    d["params"] = json.loads(d["params"]) if d["params"] else {}
    d["evidence"] = json.loads(d["evidence"]) if d["evidence"] else []
    d["result"] = json.loads(d["result"]) if d["result"] else None
    d["proposed_at"], d["decided_at"] = _iso(d["proposed_at"]), _iso(d["decided_at"])
    d["kind_label"] = KINDS.get(d["kind"], {}).get("label", d["kind"])
    if not d["preview"]:
        d["preview"] = _preview(d["kind"], d["domain"], d["params"])
    return d


def _preview(kind: str, domain: str, params: dict[str, Any]) -> str:
    """What approving would do, worded against the state at the moment it is built. Stored when the
    proposal is filed: rebuilt later it describes a different world -- an approved "add loss_date"
    re-previewed afterwards read "Add nothing new", because loss_date was by then already there."""
    try:
        return KINDS[kind]["preview"](domain, params)
    except Exception as exc:  # noqa: BLE001 -- a preview that can't be built is said, not hidden
        return f"(preview unavailable: {type(exc).__name__}: {str(exc)[:120]})"


def _check_targets(domain: str, kind: str, p: dict[str, Any]) -> None:
    """What a proposal points at must exist when it is filed -- a proposal to accept version 7 of a
    source that has three versions is refused at once, not left to fail when someone approves it."""
    if kind == "add_intent_data_points":
        from emitters.intent import list_intents
        from emitters.versions import check_id
        if p.get("intent_id"):
            wanted = p["intent_id"].strip().lower()
            # match against the intents on record rather than opening a file named after the
            # parameter: an id is a file path, and a name ("ZZ proposal check") is still unambiguous
            intents = list_intents(domain)
            by_id = [i for i in intents if (i.get("intent_id") or "").lower() == wanted]
            by_name = [i for i in intents if (i.get("name") or "").strip().lower() == wanted]
            match = by_id or (by_name if len(by_name) == 1 else [])
            if not match:
                raise ValueError(f"there is no intent {p['intent_id']!r}; give a name to create a new one instead")
            p["intent_id"] = check_id(match[0]["intent_id"])
            # The report must be one the intent already has, unless a new one is asked for. Found
            # in the live smoke test: an agent wrote "Claims by month report" for the report
            # "Claims by month", and approval quietly created a second, misnamed report instead of
            # adding the data point where it belonged.
            reports = [r.get("name") or "" for r in match[0].get("reports") or []]
            if not p.get("new_report"):
                def norm(n: str) -> str:
                    n = " ".join(n.lower().split())
                    return n[:-len(" report")] if n.endswith(" report") else n
                same_report = [r for r in reports if norm(r) == norm(p["report"])]
                if len(same_report) != 1:
                    raise ValueError(f"intent {p['intent_id']!r} has no report called {p['report']!r}; its reports are "
                                     f"{reports or 'none'} -- use one of those names, or set new_report true to add a report")
                p["report"] = same_report[0]
        elif not p.get("name"):
            raise ValueError("give an existing intent_id, or a name for a new intent")
    elif kind == "review_source_version":
        from emitters import source_review, versions
        versions.check_id(p["source_id"])
        known = [m["version"] for m in versions.list_versions(source_review._root(domain), p["source_id"])]
        if p["version"] not in known:
            raise ValueError(f"source {p['source_id']!r} has no version {p['version']} (it has {known or 'none'})")
    elif kind == "assign_ticket":
        from emitters.ops_tickets import get_ticket
        if get_ticket(domain, "duckdb", p["ticket_id"]) is None:
            raise ValueError(f"there is no ticket {p['ticket_id']}")
    elif kind == "update_architecture" and "scd_type" in p and p["scd_type"] not in ("scd1", "scd2"):
        raise ValueError("scd_type must be scd1 or scd2")


def propose(domain: str, target: str, agent: str, kind: str, title: str, rationale: str,
            params: dict[str, Any], evidence: list[str] | None = None, session_id: str | None = None) -> dict[str, Any]:
    clean = validate(kind, params)
    _check_targets(domain, kind, clean)
    if not (title or "").strip() or not (rationale or "").strip():
        raise ValueError("a proposal needs a title and a rationale")
    pid = uuid.uuid4().hex[:12]
    con, c = _con(domain)
    try:
        con.execute(f"insert into {c}.agent_proposal values ({', '.join('?' * len(_COLS))})",
                    [pid, domain, target, agent, kind, title.strip(), rationale.strip(), json.dumps(clean),
                     json.dumps(evidence or []), session_id, "proposed", _now(), None, None, None, None,
                     _preview(kind, domain, clean)])
    finally:
        con.close()
    return get(domain, pid)


def get(domain: str, proposal_id: str) -> dict[str, Any]:
    con, c = _con(domain)
    try:
        r = con.execute(f"select {', '.join(_COLS)} from {c}.agent_proposal where proposal_id = ? and domain = ?",
                        [proposal_id, domain]).fetchone()
    finally:
        con.close()
    if r is None:
        raise KeyError(f"no proposal {proposal_id!r} for {domain}")
    return _row(r)


def list_proposals(domain: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    con, c = _con(domain)
    try:
        if status:
            rows = con.execute(f"select {', '.join(_COLS)} from {c}.agent_proposal where domain = ? and status = ? "
                               f"order by proposed_at desc limit {int(limit)}", [domain, status]).fetchall()
        else:
            rows = con.execute(f"select {', '.join(_COLS)} from {c}.agent_proposal where domain = ? "
                               f"order by proposed_at desc limit {int(limit)}", [domain]).fetchall()
    finally:
        con.close()
    return [_row(r) for r in rows]


def decide(domain: str, proposal_id: str, decision: str, by: str, note: str = "",
           approver_is_admin: bool = False) -> dict[str, Any]:
    """Approve runs the executor under the approver's name; decline records why. A proposal is
    decided once -- a second decision is refused rather than re-run."""
    if decision not in ("approve", "decline"):
        raise ValueError("decision must be approve or decline")
    p = get(domain, proposal_id)
    if p["status"] != "proposed":
        raise ValueError(f"proposal is already {p['status']}")
    if decision == "decline" and not note.strip():
        raise ValueError("say why it's declined -- the agent learns from it")
    status, result = "declined", None
    if decision == "approve":
        try:
            result = KINDS[p["kind"]]["apply"](domain, p["target"], p["params"], by, approver_is_admin)
            status = "approved"
        except Exception as exc:  # noqa: BLE001 -- a failed apply is recorded as such, never as approved
            status, result = "failed", {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    con, c = _con(domain)
    try:
        con.execute(f"update {c}.agent_proposal set status = ?, decided_by = ?, decided_at = ?, decision_note = ?, "
                    f"result = ? where proposal_id = ?",
                    [status, by, _now(), note.strip() or None, json.dumps(result, default=str) if result is not None else None, proposal_id])
    finally:
        con.close()
    if status == "declined":
        # the agent that proposed it learns why, as a correction it recalls next time
        try:
            from emitters import agent_memory
            agent_memory.remember(domain, p["agent"], f"declined: {p['title']}",
                                  f"The team declined this proposal ({p['kind_label']}): {note.strip()}",
                                  source="proposal decision", kind="correction", session_id=p.get("session_id"))
        except Exception:  # noqa: BLE001 -- memory is an enhancement
            pass
    return get(domain, proposal_id)
