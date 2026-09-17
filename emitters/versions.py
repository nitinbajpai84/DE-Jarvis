"""Version history for the artifacts a human reviews: source profiles (Step 01) and intents
(Step 02).

Before this, both were a single file overwritten in place. Re-profiling a source replaced its
profile, and re-saving an intent replaced the intent, so "what did this say last week, and what
changed?" had no answer -- which is the one question a review loop exists to answer.

Each artifact keeps its full history beside it:

    <artifact dir>/.history/<artifact_id>/v0001.json
                                         v0002.json ...

Each file is {"meta": {...}, "artifact": {...}}, never rewritten except for its review status.
The live file the rest of the platform already reads (`<source_id>.profile.json`,
`<intent_id>.yaml`) stays exactly where it was, so nothing downstream -- gap analysis, the G1
gate tool, the agents -- needed to change. `.history` is a dotted directory, so the existing
`*.profile.json` / `*.yaml` globs never pick it up.

Filesystem rather than the control plane because the artifacts themselves already live in
contracts/, which on Railway is the persistent volume (docker-entrypoint.sh); a history kept
in a different store from its artifact could disagree with it.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
from typing import Any

HISTORY = ".history"
_VFILE = re.compile(r"^v(\d{4,})\.json$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$")


def utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def check_id(artifact_id: str) -> str:
    """Artifact ids become directory names -- refuse anything that could climb out of the
    history directory (`..`, slashes) rather than trying to sanitize it."""
    if not _SAFE_ID.match(artifact_id or "") or ".." in artifact_id:
        raise ValueError(f"invalid id {artifact_id!r}: use letters, digits, '_', '-' or '.'")
    return artifact_id


def _dir(root: pathlib.Path, artifact_id: str) -> pathlib.Path:
    return root / HISTORY / check_id(artifact_id)


def _numbers(root: pathlib.Path, artifact_id: str) -> list[int]:
    d = _dir(root, artifact_id)
    if not d.exists():
        return []
    return sorted(int(m.group(1)) for p in d.iterdir() if (m := _VFILE.match(p.name)))


def record(root: pathlib.Path, artifact_id: str, artifact: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Appends a new version and returns its meta. The number is claimed with an exclusive
    create, so two saves landing at the same moment get consecutive versions instead of one
    silently overwriting the other."""
    d = _dir(root, artifact_id)
    d.mkdir(parents=True, exist_ok=True)
    n = (_numbers(root, artifact_id) or [0])[-1] + 1
    while True:
        path = d / f"v{n:04d}.json"
        full = {**meta, "version": n, "created_at": meta.get("created_at") or utcnow_iso()}
        try:
            with path.open("x", encoding="utf-8") as fh:
                json.dump({"meta": full, "artifact": artifact}, fh, indent=2, default=str)
            return full
        except FileExistsError:
            n += 1


def list_versions(root: pathlib.Path, artifact_id: str) -> list[dict[str, Any]]:
    """Meta only, oldest first."""
    out = []
    for n in _numbers(root, artifact_id):
        try:
            out.append(load(root, artifact_id, n)["meta"])
        except Exception:  # noqa: BLE001 -- one unreadable version shouldn't hide the rest
            continue
    return out


def load(root: pathlib.Path, artifact_id: str, version: int) -> dict[str, Any]:
    path = _dir(root, artifact_id) / f"v{int(version):04d}.json"
    if not path.exists():
        raise FileNotFoundError(f"{artifact_id} has no version {version}")
    return json.loads(path.read_text(encoding="utf-8"))


def update_meta(root: pathlib.Path, artifact_id: str, version: int, **changes: Any) -> dict[str, Any]:
    """The only mutation a version ever gets: its review outcome. The artifact body is never
    touched after it is recorded."""
    path = _dir(root, artifact_id) / f"v{int(version):04d}.json"
    doc = load(root, artifact_id, version)
    doc["meta"].update(changes)
    path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    return doc["meta"]
