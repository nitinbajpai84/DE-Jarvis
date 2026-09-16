"""API-facing layer over Step 05 ops tickets (emitters/ops_tickets.py). Same "call the real
function directly" pattern as every other Step 02/04/05 backend module -- interactive use from
the Control Room shouldn't require a full agent run for anything except the G5 gate itself.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters import ops_tickets as ops_tickets_mod  # noqa: E402


def list_tickets(domain: str, target: str, status: str | None = None) -> list[dict[str, Any]]:
    return ops_tickets_mod.list_tickets(domain, target, status)


def scan(domain: str, target: str) -> dict[str, Any]:
    result = ops_tickets_mod.raise_tickets_from_scan(domain, target, raised_by="control-room")
    if result["newly_raised"]:
        ops_tickets_mod.notify_slack(
            f":rotating_light: [{domain}] Ops scan raised {len(result['newly_raised'])} new "
            f"ticket(s) from the Control Room: {result['newly_raised']}."
        )
    return result


def assign(domain: str, target: str, ticket_id: int, assigned_to: str) -> dict[str, Any]:
    return ops_tickets_mod.assign_ticket(domain, target, ticket_id, assigned_to)


def propose_fix(domain: str, target: str, ticket_id: int, resolution_note: str) -> dict[str, Any]:
    return ops_tickets_mod.propose_fix(domain, target, ticket_id, resolution_note, fixed_by="human")


def reject(domain: str, target: str, ticket_id: int, reason: str) -> dict[str, Any]:
    return ops_tickets_mod.reject_ticket(domain, target, ticket_id, reason)
