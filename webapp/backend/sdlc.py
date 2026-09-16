"""SDLC stage status for the API -- reuses harness/agent_dashboard.py's hand-curated
STAGES/GATES/PHASES/_file_exists directly rather than re-deriving them, so the web UI and
the static agent_dashboard.html can never disagree about what's actually been evidenced.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from agents_loader import _parse, AGENT_DIR  # noqa: E402
from harness.agent_dashboard import STAGES, GATES, PHASES, _file_exists  # noqa: E402


def _mtime(rel: str) -> float | None:
    path_part = rel.split(" (")[0]
    full = REPO_ROOT / path_part
    return full.stat().st_mtime if full.exists() else None


def stage_status() -> dict[str, Any]:
    agents = {a["name"]: a for a in (_parse(p) for p in sorted(AGENT_DIR.glob("*.md")))}

    stages = []
    for n, name, owner in STAGES:
        # A stage counts as evidenced if ANY recorded phase has every one of its listed
        # artifacts present on disk -- same honesty rule agent_dashboard.py applies per phase,
        # just rolled up to "has this stage ever actually been done, with proof".
        evidence: list[str] = []
        done = False
        latest_mtime = None
        for phase in PHASES:
            artifacts = phase["stages"].get(n, [])
            if artifacts and all(_file_exists(a) for a in artifacts):
                done = True
                evidence.extend(f"{phase['name']}: {a}" for a in artifacts)
                for a in artifacts:
                    m = _mtime(a)
                    if m and (latest_mtime is None or m > latest_mtime):
                        latest_mtime = m
        stages.append({
            "n": n,
            "name": name,
            "owner": owner,
            "gate": GATES.get(n),
            "done": done,
            "evidence": evidence,
            "latest_mtime": latest_mtime,
        })

    # "Active" isn't simulated -- it's whichever done stage has the most recently modified
    # evidence file on disk, i.e. whatever was actually worked on last.
    active_n = None
    best = None
    for s in stages:
        if s["done"] and s["latest_mtime"] and (best is None or s["latest_mtime"] > best):
            best = s["latest_mtime"]
            active_n = s["n"]

    return {
        "agents": [
            {"name": a["name"], "model": a["model"], "description": a["description"]}
            for a in agents.values()
        ],
        "stages": stages,
        "active_stage": active_n,
    }
