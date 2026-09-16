"""Catalogue upload intake: stage an .xlsx, preview what the real compiler would produce,
and only write contracts to disk on an explicit second "approve" call. This runs the exact
same script a terminal user would (emitters/intake_compiler.py) as a subprocess -- no compiler
logic is reimplemented here, and the --write step is never triggered by the preview call,
matching the human-checkpoint pattern that script already has (preview-by-default, write is
opt-in).

One unified workbook (docs/templates/jarvis_intake_template.xlsx) replaces the three separate
ones this used to compile (source catalogue / silver model / gold model) -- see
emitters/intake_compiler.py's module docstring for what it does and doesn't translate.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import uuid
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR = REPO_ROOT / "webapp" / "uploads"
STATUS_PATH = UPLOAD_DIR / "status.json"
PYTHON = sys.executable

COMPILERS = {
    "intake": ("emitters.intake_compiler", []),
}


def _load_status() -> dict[str, Any]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if STATUS_PATH.exists():
        return json.loads(STATUS_PATH.read_text())
    return {}


def _save_status(status: dict[str, Any]) -> None:
    STATUS_PATH.write_text(json.dumps(status, indent=2))


def list_uploads() -> list[dict[str, Any]]:
    status = _load_status()
    return sorted(status.values(), key=lambda r: r["uploaded_at"], reverse=True)


def _run_compiler(kind: str, workbook_path: pathlib.Path, write: bool) -> dict[str, Any]:
    module, extra_args = COMPILERS[kind]
    args = [PYTHON, "-m", module, "--workbook", str(workbook_path), *extra_args]
    if write:
        args.append("--write")
    proc = subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-8000:],
        "stderr": proc.stderr[-4000:],
    }


def save_and_preview(kind: str, filename: str, content: bytes) -> dict[str, Any]:
    if kind not in COMPILERS:
        raise ValueError(f"kind must be one of {list(COMPILERS)}, got {kind!r}")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex[:12]
    stored_name = f"{upload_id}_{filename}"
    dest = UPLOAD_DIR / stored_name
    dest.write_bytes(content)

    result = _run_compiler(kind, dest, write=False)

    record = {
        "id": upload_id,
        "filename": filename,
        "stored_path": str(dest),
        "kind": kind,
        "uploaded_at": time.time(),
        "preview": result,
        "compiled": False,
        "compile_result": None,
    }
    status = _load_status()
    status[upload_id] = record
    _save_status(status)
    return record


def approve_and_compile(upload_id: str) -> dict[str, Any]:
    status = _load_status()
    if upload_id not in status:
        raise KeyError(f"no upload with id {upload_id!r}")
    record = status[upload_id]
    result = _run_compiler(record["kind"], pathlib.Path(record["stored_path"]), write=True)
    record["compiled"] = result["ok"]
    record["compile_result"] = result
    status[upload_id] = record
    _save_status(status)
    return record
