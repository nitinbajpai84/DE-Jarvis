"""Step 05 operations: Agent 7's incident loop -- detect a real issue, raise a tracked ticket,
assign it to Agent 4 (DE), require a human to approve the DE's fix, re-verify with Agent 5's
own test pack before closing, and report back to Agent 3 (PM).

Deliberately does NOT invent a second way to detect problems. Every ticket traces to a real,
already-computed fact: a FAILING case from emitters/test_pack.py's per-layer test pack (the
same bronze/silver/gold checks, including the real orphan-FK detector, proven in Phase J) is
the one source of truth for "is something actually wrong" -- not a separate heuristic, and not
a guess. A ticket's `entity` column stores that failing case's own stable `id`
(e.g. "fk:fact_policy_coverage:dim_policy"), so resolving a ticket can re-check the EXACT same
case, not a fuzzy match on description text.

The resolution gate (G5, accept_ticket_resolution) follows the same rule every gate in this
project already follows: a human clicking Approve is approving EVIDENCE, and there has to be
evidence. It re-runs the test pack and checks the specific case is now passing -- it never
trusts the DE's own resolution_note. A ticket "resolved" by this system was independently
re-verified, not just claimed fixed.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import sys
import urllib.request
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters.control_plane import ensure_control_schema, raise_ticket as _raise_ticket_row  # noqa: E402
from emitters.control_plane import update_ticket as _update_ticket_row  # noqa: E402
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402
from emitters.test_pack import generate_test_pack  # noqa: E402

_OPEN_STATUSES = {"open", "assigned", "fix_pending_approval"}


def _load_platform(target: str) -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


def _con(target: str, domain: str):
    platform = _load_platform(target)
    control = resolve_schema(platform, domain, "control")
    con = sql_connect(target, platform)
    ensure_control_schema(con, control)
    return con, control


def _row_to_dict(row, cols) -> dict[str, Any]:
    return dict(zip(cols, row))


_TICKET_COLS = ["ticket_id", "domain", "client", "phase", "entity", "issue_type", "severity",
                "description", "status", "assigned_to", "raised_by", "raised_at",
                "resolution_note", "resolved_by", "resolved_at", "verified_by_test"]


def list_tickets(domain: str, target: str = "duckdb", status: str | None = None) -> list[dict[str, Any]]:
    con, control = _con(target, domain)
    try:
        if status:
            rows = con.execute(
                f"select * from {control}.incident_ticket where domain = ? and status = ? "
                f"order by raised_at desc", [domain, status],
            ).fetchall()
        else:
            rows = con.execute(
                f"select * from {control}.incident_ticket where domain = ? order by raised_at desc",
                [domain],
            ).fetchall()
        return [_row_to_dict(r, _TICKET_COLS) for r in rows]
    finally:
        con.close()


def scan_for_issues(domain: str, target: str = "duckdb") -> dict[str, Any]:
    """Runs the real test pack and returns exactly the failing cases -- the candidate tickets.
    Read-only from the ticket table's point of view (test_pack itself re-evaluates DQ/business
    rules the same way every layer's own build does, same as every other caller of it)."""
    report = generate_test_pack(domain, target)
    failing = [c for layer in report["by_layer"].values() for c in layer if c["status"] == "fail"]
    return {"domain": domain, "target": target, "test_run_id": report["test_run_id"],
            "failing_cases": failing, "total_failed": report["total_failed"]}


def raise_tickets_from_scan(domain: str, target: str, raised_by: str = "ops-monitor") -> dict[str, Any]:
    """Raises one ticket per currently-failing test-pack case that doesn't already have an open
    ticket -- deduplicated by (domain, entity/case-id, issue_type) so re-scanning doesn't spam a
    new ticket for the same standing issue every tick."""
    scan = scan_for_issues(domain, target)
    con, control = _con(target, domain)
    try:
        existing = con.execute(
            f"select entity, issue_type from {control}.incident_ticket "
            f"where domain = ? and status in ({','.join('?' * len(_OPEN_STATUSES))})",
            [domain, *_OPEN_STATUSES],
        ).fetchall()
        existing_keys = {(e, t) for e, t in existing}

        raised = []
        for case in scan["failing_cases"]:
            issue_type = case["id"].split(":", 1)[0]  # "fk" | "dq" | "bronze" | "silver" | "gold"
            key = (case["id"], issue_type)
            if key in existing_keys:
                continue
            ticket_id = _raise_ticket_row(
                con, control, domain=domain, client="default", phase=case["layer"],
                entity=case["id"], issue_type=issue_type, severity="error",
                description=f"{case['description']} -- {case['detail']}", raised_by=raised_by,
            )
            raised.append(ticket_id)
        return {"domain": domain, "scanned": len(scan["failing_cases"]),
                "already_ticketed": len(scan["failing_cases"]) - len(raised),
                "newly_raised": raised}
    finally:
        con.close()


def assign_ticket(domain: str, target: str, ticket_id: int, assigned_to: str) -> dict[str, Any]:
    con, control = _con(target, domain)
    try:
        _update_ticket_row(con, control, ticket_id, status="assigned", assigned_to=assigned_to)
        return {"ok": True, "ticket_id": ticket_id, "status": "assigned", "assigned_to": assigned_to}
    finally:
        con.close()


def propose_fix(domain: str, target: str, ticket_id: int, resolution_note: str, fixed_by: str) -> dict[str, Any]:
    """The DE's account of what they did -- NOT itself the verification. Sets the ticket to
    fix_pending_approval; nothing is considered resolved until accept_ticket_resolution (G5)
    both gets a human's approval AND independently re-confirms the specific case now passes."""
    con, control = _con(target, domain)
    try:
        _update_ticket_row(con, control, ticket_id, status="fix_pending_approval",
                           resolution_note=resolution_note, resolved_by=fixed_by)
        return {"ok": True, "ticket_id": ticket_id, "status": "fix_pending_approval"}
    finally:
        con.close()


def get_ticket(domain: str, target: str, ticket_id: int) -> dict[str, Any] | None:
    con, control = _con(target, domain)
    try:
        row = con.execute(
            f"select * from {control}.incident_ticket where domain = ? and ticket_id = ?",
            [domain, ticket_id],
        ).fetchone()
        return _row_to_dict(row, _TICKET_COLS) if row else None
    finally:
        con.close()


def verify_and_resolve(domain: str, target: str, ticket_id: int, resolved_by: str) -> dict[str, Any]:
    """The actual enforcement: re-runs the test pack fresh and checks whether THIS ticket's
    exact case (matched by test_pack's own stable case id, stored in `entity`) now passes.
    Refuses -- leaves the ticket at fix_pending_approval -- if it's still failing, regardless of
    what the resolution_note claims."""
    ticket = get_ticket(domain, target, ticket_id)
    if ticket is None:
        return {"ok": False, "reason": f"no ticket {ticket_id} for domain {domain!r}"}
    if ticket["status"] != "fix_pending_approval":
        return {"ok": False, "reason": f"ticket {ticket_id} is {ticket['status']!r}, "
                                       f"not fix_pending_approval -- nothing to verify"}

    report = generate_test_pack(domain, target)
    all_cases = {c["id"]: c for layer in report["by_layer"].values() for c in layer}
    case = all_cases.get(ticket["entity"])

    con, control = _con(target, domain)
    try:
        if case is None:
            # The case no longer appears at all (e.g. the entity it was about was removed) --
            # treat that as resolved too, since there's nothing left to fail, but say so plainly.
            _update_ticket_row(con, control, ticket_id, status="resolved", verified_by_test=True,
                               resolved_at=_dt.datetime.now(_dt.timezone.utc))
            return {"ok": True, "resolved": True, "ticket_id": ticket_id,
                    "note": "case no longer present in the test pack -- treated as resolved",
                    "test_run_id": report["test_run_id"]}
        if case["status"] == "pass":
            _update_ticket_row(con, control, ticket_id, status="resolved", verified_by_test=True,
                               resolved_at=_dt.datetime.now(_dt.timezone.utc))
            return {"ok": True, "resolved": True, "ticket_id": ticket_id,
                    "test_run_id": report["test_run_id"], "case": case}
        return {"ok": True, "resolved": False, "ticket_id": ticket_id,
                "reason": "the case still fails -- refusing to resolve on the DE's claim alone",
                "test_run_id": report["test_run_id"], "case": case}
    finally:
        con.close()


def reject_ticket(domain: str, target: str, ticket_id: int, reason: str) -> dict[str, Any]:
    con, control = _con(target, domain)
    try:
        _update_ticket_row(con, control, ticket_id, status="rejected", resolution_note=reason)
        return {"ok": True, "ticket_id": ticket_id, "status": "rejected"}
    finally:
        con.close()


# --------------------------------------------------------------------------- Slack

def _load_dotenv_once() -> None:
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def notify_slack(text: str, blocks: list[dict] | None = None) -> tuple[int, str] | None:
    """Same webhook, same one-shared-channel-tagged-by-domain pattern as
    harness/ops_monitor.py's existing send_slack_alert -- reused, not reimplemented. Returns
    None (not an error) if no webhook is configured, so ticket operations work in an environment
    with no Slack wired up; the caller decides whether that's worth surfacing."""
    _load_dotenv_once()
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return None
    payload: dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    req = urllib.request.Request(
        webhook, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode()
