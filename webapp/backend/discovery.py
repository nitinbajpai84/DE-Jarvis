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

from emitters import intent_change, profiler, source_review  # noqa: E402


def test_connection(connection: dict) -> dict[str, Any]:
    return profiler.test_connection(connection)


def profile_source(connection: dict, source_id: str, domain: str, sample_limit: int = 500,
                   created_by: str = "human") -> dict[str, Any]:
    return profiler.profile_source(connection, source_id, domain, sample_limit, created_by=created_by)


def list_profiles(domain: str) -> list[dict[str, Any]]:
    """Live profiles, each with its review state -- the list is where a reviewer sees that an
    unreviewed version is waiting."""
    return [{**p, "review": source_review.review_summary(domain, p["source_id"])}
            for p in profiler.list_profiles(domain)]


allowed_path = source_review.allowed_path
upload_source = source_review.upload_source
reprofile = source_review.reprofile
version_view = source_review.version_view
decide = source_review.decide
intent_change_view = intent_change.change_view
sign_intent = intent_change.sign
