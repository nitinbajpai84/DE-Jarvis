"""Step 01 review loop: upload a source, see exactly what came in, compare it with what was
there before, and accept or reject it.

The platform's principle is that nothing an agent produces is a one-shot blob. For a source that
means four things:

  Preview    the first rows as sampled, next to the column profile (profiler.PREVIEW_ROWS)
  Version    every profile is kept -- re-profiling or re-uploading adds a version and never
             overwrites (emitters/versions.py)
  Diff       what a new version changes against the one before it: columns, types, null
             rates, keys, row counts -- and, the part a reviewer actually needs, which intent
             data points it newly satisfies or newly breaks
  Decide     accept signs a version off; reject withdraws it, and the source falls back to the
             most recent version that wasn't rejected

What the rest of the platform reads (`<source_id>.profile.json`, and through it gap analysis)
is always the latest version that hasn't been rejected. A pending version is live -- a customer
who uploads a corrected file expects gap analysis to see it now -- but it is visibly unreviewed,
and "confirmed" counts only accepted ones.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
import uuid
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters import profiler, versions  # noqa: E402

MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# What an upload is profiled as. Tabular files go through the CSV sampler; documents through
# the unstructured one, which reads their content with Gemini. Anything else is refused rather
# than profiled as garbage -- an .xlsx read as CSV produces a "profile" of binary noise.
_TABULAR = {".csv": ",", ".tsv": "\t"}
_DOCUMENT = {".txt", ".md"}
_NULL_SHIFT_POINTS = 5.0


def _root(domain: str) -> pathlib.Path:
    return profiler.DISCOVERY_DIR / versions.check_id(domain)


# --------------------------------------------------------------------------- access

def allowed_path(connection: dict, domain: str, is_admin: bool) -> tuple[bool, str]:
    """File and document sources are globs into the server's landing area, which holds every
    tenant's files. An admin may point anywhere under harness/; a company login only at its own
    uploads, or at a landing path one of its own approved source contracts already names. Without
    this, Star Investments could profile harness/landing/claims/* -- insurance's files -- by
    typing the path."""
    if connection.get("type") not in ("file", "unstructured"):
        return True, ""
    raw = str(connection.get("path") or "")
    if ".." in pathlib.PurePosixPath(raw.replace("\\", "/")).parts:
        return False, "Paths may not contain '..'."
    if is_admin:
        return True, ""
    rel = raw[len("harness/"):] if raw.startswith("harness/") else raw
    own = [f"landing/uploads/{domain}/"]
    try:
        import yaml
        for p in sorted((REPO_ROOT / "contracts" / "sources" / domain).glob("*.source.yaml")):
            cpath = str((yaml.safe_load(p.read_text()) or {}).get("connection", {}).get("path") or "")
            if cpath:
                cpath = cpath[len("harness/"):] if cpath.startswith("harness/") else cpath
                own.append(cpath.rsplit("/", 1)[0] + "/")
    except Exception:  # noqa: BLE001 -- unreadable contracts just mean fewer allowed prefixes
        pass
    if any(rel.startswith(prefix) for prefix in own):
        return True, ""
    return False, (f"That path is outside {domain}'s files. Upload the file instead, or use a "
                   f"landing path from one of {domain}'s own source contracts.")


# --------------------------------------------------------------------------- diff

def _where(profile: dict[str, Any]) -> dict[str, Any]:
    conn = dict(profile.get("connection") or {})
    if str(conn.get("path") or "").startswith("landing/uploads/"):
        conn["path"] = "(uploaded file)"
    return conn


def diff_profiles(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """What changed between two profiles of the same source, in the terms a reviewer checks.
    Null-rate moves under 5 points are sampling noise and not reported."""
    if old is None:
        return {"first_version": True}
    oc = {c["name"]: c for c in old.get("columns", [])}
    nc = {c["name"]: c for c in new.get("columns", [])}
    type_changes = [{"column": n, "before": oc[n]["inferred_type"], "after": nc[n]["inferred_type"]}
                    for n in nc if n in oc and oc[n]["inferred_type"] != nc[n]["inferred_type"]]
    null_shifts = [{"column": n, "before": oc[n]["null_pct"], "after": nc[n]["null_pct"]}
                   for n in nc if n in oc
                   and abs((nc[n]["null_pct"] or 0) - (oc[n]["null_pct"] or 0)) >= _NULL_SHIFT_POINTS]
    ok, nk = set(old.get("candidate_keys", [])), set(new.get("candidate_keys", []))
    ofields = {k for e in old.get("content_extraction", []) or [] for k in (e.get("candidate_fields") or {})}
    nfields = {k for e in new.get("content_extraction", []) or [] for k in (e.get("candidate_fields") or {})}
    out = {
        "first_version": False,
        "rows": {"before": old.get("total_rows"), "after": new.get("total_rows")},
        "columns_added": [n for n in nc if n not in oc],
        "columns_removed": [n for n in oc if n not in nc],
        "type_changes": type_changes,
        "null_shifts": null_shifts,
        "keys_gained": sorted(nk - ok),
        "keys_lost": sorted(ok - nk),
        # every upload lands in its own directory, so two uploads always differ in path -- only
        # a change of where the data is read from (or how) is worth a reviewer's attention
        "connection_changed": _where(old) != _where(new),
    }
    if ofields or nfields:
        out["fields_added"], out["fields_removed"] = sorted(nfields - ofields), sorted(ofields - nfields)
    out["unchanged"] = not any([out["columns_added"], out["columns_removed"], type_changes, null_shifts,
                                out["keys_gained"], out["keys_lost"],
                                out["rows"]["before"] != out["rows"]["after"],
                                out.get("fields_added"), out.get("fields_removed")])
    return out


def gap_impact(domain: str, source_id: str, old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    """Which intent data points this version changes the answer for. Runs gap analysis twice
    in memory -- once with this source's old columns, once with the new -- against every other
    source exactly as it stands, so a data point still satisfied elsewhere is not reported as
    broken."""
    from emitters import intent as intent_mod
    intents = intent_mod.list_intents(domain)
    if not intents:
        return {"intent_captured": False, "newly_resolved": [], "newly_open": []}
    base = intent_mod.known_columns(domain)

    def cols_with(profile):
        disc = {k: v for k, v in base["discovered"].items() if k != source_id}
        if profile is not None:
            disc[source_id] = [c["name"] for c in profile.get("columns", [])]
        return {"contracted": base["contracted"], "discovered": disc}

    before = intent_mod.compute_gaps(intents, cols_with(old))
    after = intent_mod.compute_gaps(intents, cols_with(new))
    key = lambda g: (g["intent"], g["report"], g["data_point"])  # noqa: E731
    was = {key(g): g["status"] for g in before}
    pick = lambda g: {"intent": g["intent"], "report": g["report"], "data_point": g["data_point"]}  # noqa: E731
    return {
        "intent_captured": True,
        "newly_resolved": [pick(g) for g in after if g["status"] == "resolved" and was.get(key(g)) == "open"],
        "newly_open": [pick(g) for g in after if g["status"] == "open" and was.get(key(g)) == "resolved"],
    }


# --------------------------------------------------------------------------- state

def _artifact(domain: str, source_id: str, version: int) -> dict[str, Any]:
    doc = versions.load(_root(domain), source_id, version)
    return {**doc["artifact"], "version": doc["meta"]["version"]}


def _live_version(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    live = [m for m in history if m.get("review", {}).get("status") != "rejected"]
    return live[-1] if live else None


def _predecessor(history: list[dict[str, Any]], version: int) -> dict[str, Any] | None:
    """What a version is compared with: the most recent earlier version that wasn't rejected --
    i.e. what was on record when it arrived. A rejected upload in between is not the baseline."""
    earlier = [m for m in history if m["version"] < version and m.get("review", {}).get("status") != "rejected"]
    return earlier[-1] if earlier else None


def _sync_live(domain: str, source_id: str) -> dict[str, Any] | None:
    """Makes the live profile file match the latest non-rejected version -- or removes it when
    every version has been rejected, so gap analysis stops counting a source nobody accepted."""
    root = _root(domain)
    live = _live_version(versions.list_versions(root, source_id))
    path = root / f"{source_id}.profile.json"
    if live is None:
        if path.exists():
            path.unlink()
        return None
    artifact = _artifact(domain, source_id, live["version"])
    path.write_text(json.dumps(artifact, indent=2, default=str))
    return artifact


def review_summary(domain: str, source_id: str) -> dict[str, Any]:
    """Compact status for the profile list: live version, its review state, and whether an
    unreviewed version is waiting."""
    history = versions.list_versions(_root(domain), source_id)
    live = _live_version(history)
    return {
        "version": live["version"] if live else None,
        "review_status": (live or {}).get("review", {}).get("status"),
        "versions": len(history),
        "pending": sum(1 for m in history if m.get("review", {}).get("status") == "pending"),
    }


def version_view(domain: str, source_id: str, version: int | None = None) -> dict[str, Any]:
    """One version in full -- profile, preview rows, meta -- with its diff and gap impact against
    what was on record before it. Defaults to the live version."""
    root = _root(domain)
    versions.check_id(source_id)
    history = versions.list_versions(root, source_id)
    if not history:
        current = root / f"{source_id}.profile.json"
        if not current.exists():
            raise FileNotFoundError(f"no source {source_id!r} for {domain}")
        profile = json.loads(current.read_text())
        return {"source_id": source_id, "history": [], "meta": None, "profile": profile,
                "diff": {"first_version": True}, "impact": None}
    target = next((m for m in history if m["version"] == version), None) if version else _live_version(history)
    if target is None:
        target = history[-1]
    profile = _artifact(domain, source_id, target["version"])
    prev_meta = _predecessor(history, target["version"])
    prev = _artifact(domain, source_id, prev_meta["version"]) if prev_meta else None
    return {
        "source_id": source_id,
        "history": history,
        "meta": target,
        "compared_with": prev_meta["version"] if prev_meta else None,
        "profile": profile,
        "diff": diff_profiles(prev, profile),
        "impact": gap_impact(domain, source_id, prev, profile),
    }


def decide(domain: str, source_id: str, version: int, decision: str, by: str, comment: str = "") -> dict[str, Any]:
    if decision not in ("accept", "reject"):
        raise ValueError("decision must be accept or reject")
    root = _root(domain)
    versions.check_id(source_id)
    meta = next((m for m in versions.list_versions(root, source_id) if m["version"] == int(version)), None)
    if meta is None:
        raise FileNotFoundError(f"{source_id} has no version {version}")
    if decision == "reject" and not comment.strip():
        # a rejection with no reason is useless to whoever re-uploads next
        raise ValueError("Say why it's rejected — the next upload is judged against that.")
    status = "accepted" if decision == "accept" else "rejected"
    versions.update_meta(root, source_id, int(version),
                         review={"status": status, "by": by, "at": versions.utcnow_iso(), "comment": comment.strip()})
    live = _sync_live(domain, source_id)
    return {"ok": True, "status": status, "live_version": live["version"] if live else None}


# --------------------------------------------------------------------------- new versions

def upload_source(domain: str, source_id: str, filename: str, content: bytes, by: str,
                  note: str = "", sample_limit: int = 500) -> dict[str, Any]:
    """Saves an uploaded file under the domain's own upload area and profiles it as a new
    version. Each upload gets its own directory, so the version's connection path points at
    exactly that file forever -- a later re-upload can't change what an earlier version read."""
    versions.check_id(domain)
    versions.check_id(source_id)
    if not content:
        raise ValueError("The file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"The file is {len(content) // (1024 * 1024)} MB; the limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", pathlib.Path(filename or "upload").name).strip("._") or "upload"
    ext = pathlib.Path(safe).suffix.lower()
    if ext not in _TABULAR and ext not in _DOCUMENT:
        raise ValueError(f"{ext or 'This file type'} isn't supported yet. Upload CSV, TSV, TXT or MD "
                         f"(save a spreadsheet as CSV first).")

    upload_id = uuid.uuid4().hex[:10]
    dest_dir = profiler.HARNESS_DIR / "landing" / "uploads" / domain / source_id / upload_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / safe).write_bytes(content)
    rel = (dest_dir / safe).relative_to(profiler.HARNESS_DIR).as_posix()

    if ext in _TABULAR:
        connection = {"type": "file", "path": rel, "format": "csv", "delimiter": _TABULAR[ext],
                      "header": True, "encoding": "utf-8"}
    else:
        connection = {"type": "unstructured", "path": rel}

    had_versions = bool(versions.list_versions(_root(domain), source_id)) or (_root(domain) / f"{source_id}.profile.json").exists()
    file_meta = {"name": safe, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                 "upload_id": upload_id, "note": note.strip() or None}
    try:
        report = profiler.profile_source(connection, source_id, domain, sample_limit, created_by=by,
                                         reason="re-upload" if had_versions else "upload", file_meta=file_meta)
    except Exception:
        # an unreadable file leaves no version behind, so it shouldn't leave its bytes either
        import shutil
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return version_view(domain, source_id, report["version"])


def reprofile(domain: str, source_id: str, by: str, sample_limit: int = 500) -> dict[str, Any]:
    """Re-reads the live version's connection as it is now -- the source changed, not the file
    we were given -- and records the result as a new version."""
    root = _root(domain)
    versions.check_id(source_id)
    path = root / f"{source_id}.profile.json"
    if not path.exists():
        raise FileNotFoundError(f"no live profile for {source_id!r}")
    current = json.loads(path.read_text())
    report = profiler.profile_source(current["connection"], source_id, domain, sample_limit,
                                     created_by=by, reason="re-profile")
    return version_view(domain, source_id, report["version"])
