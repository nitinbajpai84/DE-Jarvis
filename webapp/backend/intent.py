"""API-facing layer over Step 02 intent capture and gap analysis (emitters/intent.py). Calls
the same functions agents/jarvis_tools.py's capture_intent/run_gap_analysis tools call,
directly -- filling in the intent form or checking gaps interactively shouldn't require a full
agent run, same reasoning webapp/backend/discovery.py already applies to Step 01.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters import intent as intent_mod  # noqa: E402


def load_intent(domain: str) -> dict[str, Any] | None:
    return intent_mod.load_intent(domain)


def capture_intent(domain: str, client: str, intent_data: dict, captured_by: str = "human") -> dict[str, Any]:
    path = intent_mod.capture_intent(domain, client, intent_data, captured_by)
    return {"ok": True, "path": str(path)}


def run_gap_analysis(domain: str) -> dict[str, Any]:
    return intent_mod.run_gap_analysis(domain)
