"""API-facing layer over Step 04 per-layer testing (emitters/test_pack.py). Same "call the real
function directly" pattern as intent.py/architecture.py/discovery.py -- running the test pack
interactively shouldn't require a full agent run.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters import test_pack as test_pack_mod  # noqa: E402


def run(domain: str, target: str) -> dict[str, Any]:
    return test_pack_mod.generate_test_pack(domain, target)
