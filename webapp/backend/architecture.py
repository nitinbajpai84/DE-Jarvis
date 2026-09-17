"""API-facing layer over Step 02 architecture capture and consistency checking
(emitters/architecture.py). Same "call the real function directly" pattern as
webapp/backend/intent.py and discovery.py.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters import architecture as architecture_mod  # noqa: E402
from emitters import intake_compiler  # noqa: E402


def load_architecture(domain: str) -> dict[str, Any] | None:
    return architecture_mod.load_architecture(domain)


def capture_architecture(domain: str, client: str, architecture_data: dict, captured_by: str = "human") -> dict[str, Any]:
    path = architecture_mod.capture_architecture(domain, client, architecture_data, captured_by)
    return {"ok": True, "path": str(path)}


def check_consistency(domain: str, workbook_path: str) -> dict[str, Any]:
    """Needs a workbook because the check compares the architecture record against what THIS
    exact compile would produce -- unlike gap analysis, there's no "current state on disk"
    equivalent to check against without recompiling."""
    spec, errors = intake_compiler.compile_workbook(pathlib.Path(workbook_path))
    if errors:
        return {"ok": False, "errors": errors}
    compiled = intake_compiler.spec_to_contracts(spec)
    return architecture_mod.check_consistency(domain, spec, compiled["model"])


def interpret_architecture_image(image_bytes: bytes, mime_type: str) -> dict[str, Any]:
    return architecture_mod.interpret_architecture_image(image_bytes, mime_type)
