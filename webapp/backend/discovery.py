"""API-facing layer over Step 01 discovery (emitters/profiler.py). Calls the same real
connect-and-sample functions agents/jarvis_tools.py's test_source_connection/profile_source
tools call, directly -- interactively testing whether a connection reaches a source shouldn't
require spinning up a full Gemini-backed agent run, the same reasoning webapp/backend/pipeline.py
already applies to reading bronze/silver/gold state.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters import profiler  # noqa: E402


def test_connection(connection: dict) -> dict[str, Any]:
    return profiler.test_connection(connection)


def profile_source(connection: dict, source_id: str, domain: str, sample_limit: int = 500) -> dict[str, Any]:
    return profiler.profile_source(connection, source_id, domain, sample_limit)


def list_profiles(domain: str) -> list[dict[str, Any]]:
    return profiler.list_profiles(domain)
