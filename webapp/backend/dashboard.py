"""API-facing layer over Step 04 visualisation (emitters/dashboard.py). Same "call the real
function directly" pattern as every other Step 02/04 backend module.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters import dashboard as dashboard_mod  # noqa: E402


def render(domain: str, target: str) -> dict[str, Any]:
    return dashboard_mod.render_dashboard(domain, target)


def save(domain: str, name: str, tiles: list[dict]) -> dict[str, Any]:
    return dashboard_mod.save_dashboard(domain, name, tiles)
