"""The landing zone: where every source's raw data arrives before bronze, laid out by company
(domain) first and by kind of source second.

    harness/landing/
      <domain>/                     one company's data, never mixed with another's
        structured/<source_id>/     files with rows and columns -- CSV, TSV, Excel
        unstructured/<source_id>/   documents and scraped content -- TXT, MD, HTML, PDF
        api/<source_id>/            raw responses captured from an API, one file per pull
        database/<source_id>/       extracts pulled from a database connection, one per pull

Uploads through the Control Room land under their source as `uploads/<upload_id>/<file>`,
beside (not inside) the files a source contract's glob reads, so an unreviewed upload can never
be swept into bronze by accident.

Why this replaced the flat `landing/<source_id>/` layout: it held every tenant's files side by
side (insurance's `claims/` next to asset management's `awm_positions/`), so isolation had to be
enforced path by path, and API and database sources left nothing in the landing zone at all --
they were read straight into bronze with no raw copy to audit or replay. Now a company's files
are one prefix (`landing/<domain>/`), and every kind of source lands something.

`migrate()` moves an existing flat layout into this one and rewrites the source contracts and
live discovery profiles that point into it. It is idempotent and is run on every container start
(docker-entrypoint.sh), because on Railway both the landing zone and contracts/ live on a
persistent volume that a deploy never overwrites.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
import shutil
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CATEGORIES = ("structured", "unstructured", "api", "database")
_BY_CONNECTION = {"file": "structured", "unstructured": "unstructured", "api": "api", "database": "database"}
STRUCTURED_EXT = {".csv", ".tsv", ".xlsx"}
UNSTRUCTURED_EXT = {".txt", ".md", ".html", ".htm"}
UPLOADS = "uploads"


def harness_dir() -> pathlib.Path:
    # read through profiler so a single override (tests, or a relocated harness) moves both
    from emitters import profiler
    return profiler.HARNESS_DIR


def category_for_connection(connection_type: str) -> str:
    try:
        return _BY_CONNECTION[connection_type]
    except KeyError:
        raise ValueError(f"no landing category for connection type {connection_type!r}") from None


def category_for_file(filename: str) -> str:
    ext = pathlib.Path(filename).suffix.lower()
    if ext in STRUCTURED_EXT:
        return "structured"
    if ext in UNSTRUCTURED_EXT:
        return "unstructured"
    raise ValueError(f"{ext or 'This file type'} isn't supported yet. Upload CSV, TSV or XLSX for "
                     f"tabular data, or TXT, MD or HTML for documents.")


def source_rel(domain: str, category: str, source_id: str) -> str:
    """Path of a source's landing folder, relative to harness/ -- the form connection paths use."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown landing category {category!r}")
    return f"landing/{domain}/{category}/{source_id}"


def source_dir(domain: str, category: str, source_id: str) -> pathlib.Path:
    return harness_dir() / source_rel(domain, category, source_id)


def stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------- reading

def read_tabular(path: pathlib.Path, delimiter: str = ",", header: bool = True,
                 encoding: str = "utf-8") -> tuple[list[str], list[list[str]]]:
    """(header, rows) as strings for a CSV/TSV or an Excel workbook's first sheet. Excel cells
    come back typed from openpyxl (dates, ints, floats); they are rendered to the same text a CSV
    export would carry, so bronze's type checks see one representation whichever format landed."""
    if path.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.worksheets[0]
            raw = [list(r) for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
        raw = [r for r in raw if any(v not in (None, "") for v in r)]

        def cell(v: Any) -> str:
            if v is None:
                return ""
            if isinstance(v, _dt.datetime):
                return v.date().isoformat() if v.time() == _dt.time() else v.isoformat(sep=" ")
            if isinstance(v, _dt.date):
                return v.isoformat()
            if isinstance(v, float) and v.is_integer():
                return str(int(v))
            return str(v)

        rows = [[cell(v) for v in r] for r in raw]
    else:
        import csv
        with path.open(newline="", encoding=encoding) as fh:
            rows = list(csv.reader(fh, delimiter=delimiter))
    if not rows:
        return [], []
    if header:
        return [str(h).strip() for h in rows[0]], rows[1:]
    return [f"col_{i + 1}" for i in range(len(rows[0]))], rows


# --------------------------------------------------------------------------- land-then-load

def land_api_payload(domain: str, source_id: str, payload: bytes) -> pathlib.Path:
    """The raw API response, exactly as received, before anything parses it."""
    d = source_dir(domain, "api", source_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{source_id}_{stamp()}.json"
    path.write_bytes(payload)
    return path


def land_database_extract(domain: str, source_id: str, header: list[str], rows: list[list[Any]]) -> pathlib.Path:
    import csv
    d = source_dir(domain, "database", source_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{source_id}_{stamp()}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows([["" if v is None else v for v in r] for r in rows])
    return path


# --------------------------------------------------------------------------- inventory

def inventory(domain: str) -> dict[str, Any]:
    """What is in one company's landing zone, per category and source -- files, size, newest."""
    root = harness_dir() / "landing" / domain
    out = []
    for cat in CATEGORIES:
        sources = []
        cdir = root / cat
        if cdir.exists():
            for sdir in sorted(p for p in cdir.iterdir() if p.is_dir()):
                files = [f for f in sdir.rglob("*") if f.is_file()]
                uploads = [f for f in files if UPLOADS in f.relative_to(sdir).parts[:1]]
                newest = max((f.stat().st_mtime for f in files), default=None)
                sources.append({
                    "source_id": sdir.name,
                    "files": len(files) - len(uploads), "uploads": len(uploads),
                    "bytes": sum(f.stat().st_size for f in files),
                    "latest": _dt.datetime.fromtimestamp(newest, _dt.timezone.utc).isoformat() if newest else None,
                    "path": source_rel(domain, cat, sdir.name),
                })
        out.append({"category": cat, "sources": sources})
    return {"domain": domain, "root": f"landing/{domain}", "categories": out}


# --------------------------------------------------------------------------- migration

def _known_domains(contracts_dir: pathlib.Path, discovery_dir: pathlib.Path | None) -> set[str]:
    found = set()
    for d in (contracts_dir / "sources", discovery_dir):
        if d and d.exists():
            found |= {p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")}
    return found


def _contract_owners(contracts_dir: pathlib.Path) -> dict[str, tuple[str, str]]:
    """old flat folder name -> (domain, category), from the contracts that read from it."""
    import yaml
    owners: dict[str, tuple[str, str]] = {}
    src = contracts_dir / "sources"
    if not src.exists():
        return owners
    for p in sorted(src.glob("*/*.source.yaml")):
        try:
            c = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 -- an unreadable contract owns nothing
            continue
        conn = c.get("connection") or {}
        if conn.get("type") not in _BY_CONNECTION:
            continue
        path = str(conn.get("path") or "")
        # both layouts: a contract already rewritten still names the folder it reads
        new = re.match(rf"^(?:harness/)?landing/([^/]+)/({'|'.join(CATEGORIES)})/([^/]+)/", path)
        old = re.match(r"^(?:harness/)?landing/([^/]+)/", path)
        if new:
            owners[new.group(3)] = (new.group(1), new.group(2))
        elif old:
            owners[old.group(1)] = (p.parent.name, _BY_CONNECTION[conn["type"]])
    return owners


def _move(src: pathlib.Path, dst: pathlib.Path, dry_run: bool) -> None:
    if dry_run:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        # merge: a partially migrated tree from an interrupted run -- never overwrite a file
        for f in src.rglob("*"):
            if f.is_file():
                target = dst / f.relative_to(src)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(target))
        shutil.rmtree(src, ignore_errors=True)
    else:
        shutil.move(str(src), str(dst))


def migrate(landing_dir: pathlib.Path, contracts_dir: pathlib.Path | None = None,
            discovery_dir: pathlib.Path | None = None, dry_run: bool = False,
            rewrite_paths: bool = True) -> dict[str, Any]:
    """Flat `landing/<source_id>/` and `landing/uploads/<domain>/<source>/` into
    `landing/<domain>/<category>/<source_id>/`, then the paths that pointed at the old places.

    A folder is only moved when a source contract says which company owns it -- a folder no
    contract claims is reported and left exactly where it is, because guessing a tenant for
    someone's data is the one mistake this layout exists to prevent."""
    report: dict[str, Any] = {"moved": [], "uploads_moved": [], "unowned": [], "contracts_rewritten": [],
                              "profiles_rewritten": [], "dry_run": dry_run}
    if not landing_dir.exists():
        return report
    contracts_dir = contracts_dir or (REPO_ROOT / "contracts")
    domains = _known_domains(contracts_dir, discovery_dir)
    owners = _contract_owners(contracts_dir)
    rewrites: dict[str, str] = {}   # old rel prefix -> new rel prefix

    for entry in sorted(p for p in landing_dir.iterdir() if p.is_dir()):
        name = entry.name
        if name in domains or name == UPLOADS or name.startswith((".", "_")):
            continue
        if name not in owners:
            report["unowned"].append(name)
            continue
        domain, cat = owners[name]
        _move(entry, landing_dir / domain / cat / name, dry_run)
        rewrites[f"landing/{name}/"] = f"landing/{domain}/{cat}/{name}/"
        report["moved"].append({"from": f"landing/{name}", "to": f"landing/{domain}/{cat}/{name}"})

    uploads = landing_dir / UPLOADS
    if uploads.exists():
        for ddir in sorted(p for p in uploads.iterdir() if p.is_dir()):
            for sdir in sorted(p for p in ddir.iterdir() if p.is_dir()):
                for udir in sorted(p for p in sdir.iterdir() if p.is_dir()):
                    files = [f for f in udir.iterdir() if f.is_file()]
                    try:
                        cat = category_for_file(files[0].name) if files else "structured"
                    except ValueError:
                        cat = "structured"
                    old = f"landing/{UPLOADS}/{ddir.name}/{sdir.name}/{udir.name}/"
                    new = f"landing/{ddir.name}/{cat}/{sdir.name}/{UPLOADS}/{udir.name}/"
                    _move(udir, landing_dir / ddir.name / cat / sdir.name / UPLOADS / udir.name, dry_run)
                    rewrites[old] = new
                    report["uploads_moved"].append({"from": old, "to": new})
        if not dry_run:
            shutil.rmtree(uploads, ignore_errors=True)

    if not rewrites or not rewrite_paths:
        return report

    def rewrite(text: str) -> str:
        for old, new in sorted(rewrites.items(), key=lambda kv: -len(kv[0])):
            text = re.sub(rf"(?<![\w/]){re.escape(old)}", new, text)
            text = text.replace(f"harness/{old}", f"harness/{new}")
        return text

    for p in sorted((contracts_dir / "sources").glob("*/*.source.yaml")) if (contracts_dir / "sources").exists() else []:
        before = p.read_text(encoding="utf-8")
        after = rewrite(before)
        if after != before:
            report["contracts_rewritten"].append(str(p.relative_to(contracts_dir.parent)))
            if not dry_run:
                p.write_text(after, encoding="utf-8")

    if discovery_dir and discovery_dir.exists():
        # live profiles only: version history is a record of what was read at the time and stays as it was
        for p in sorted(discovery_dir.glob("*/*.profile.json")):
            before = p.read_text(encoding="utf-8")
            after = rewrite(before)
            if after != before:
                json.loads(after)   # never write a profile the rewrite broke
                report["profiles_rewritten"].append(str(p.relative_to(discovery_dir.parent.parent)))
                if not dry_run:
                    p.write_text(after, encoding="utf-8")
    return report


def migrate_quarantine(quarantine_dir: pathlib.Path, contracts_dir: pathlib.Path | None = None,
                       dry_run: bool = False) -> dict[str, Any]:
    """Quarantined batches are company data too: `quarantine/<source_id>/` becomes
    `quarantine/<domain>/<source_id>/`, and each contract's quarantine_path follows. Same
    ownership rule as migrate() -- a folder no contract claims stays where it is."""
    import yaml
    contracts_dir = contracts_dir or (REPO_ROOT / "contracts")
    report: dict[str, Any] = {"moved": [], "unowned": [], "contracts_rewritten": []}
    owners: dict[str, str] = {}
    for c in sorted((contracts_dir / "sources").glob("*/*.source.yaml")) if (contracts_dir / "sources").exists() else []:
        domain = c.parent.name
        text = c.read_text(encoding="utf-8")
        contract = yaml.safe_load(text) or {}
        qpath = str((contract.get("file_checks") or {}).get("quarantine_path") or "")
        m = re.match(r"^harness/quarantine/([^/]+)/?$", qpath)
        if not m or m.group(1) == domain:
            continue
        owners[m.group(1)] = domain
        new_text = text.replace(f"quarantine_path: {qpath}", f"quarantine_path: harness/quarantine/{domain}/{m.group(1)}/")
        if new_text != text:
            report["contracts_rewritten"].append(str(c.relative_to(contracts_dir.parent)))
            if not dry_run:
                c.write_text(new_text, encoding="utf-8")
    domains = set(owners.values()) | ({p.name for p in (contracts_dir / "sources").iterdir() if p.is_dir()}
                                      if (contracts_dir / "sources").exists() else set())
    if quarantine_dir.exists():
        for entry in sorted(p for p in quarantine_dir.iterdir() if p.is_dir()):
            if entry.name in domains:
                continue
            if entry.name not in owners:
                report["unowned"].append(entry.name)
                continue
            _move(entry, quarantine_dir / owners[entry.name] / entry.name, dry_run)
            report["moved"].append({"from": f"quarantine/{entry.name}", "to": f"quarantine/{owners[entry.name]}/{entry.name}"})
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Move the landing zone into landing/<domain>/<category>/<source>/")
    ap.add_argument("command", choices=["migrate"])
    ap.add_argument("--landing", default=str(REPO_ROOT / "harness" / "landing"))
    ap.add_argument("--no-rewrite", action="store_true", help="move folders only (for seed_landing)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = migrate(pathlib.Path(args.landing),
                     contracts_dir=REPO_ROOT / "contracts",
                     discovery_dir=REPO_ROOT / "contracts" / "discovery",
                     dry_run=args.dry_run, rewrite_paths=not args.no_rewrite)
    if args.no_rewrite:
        result["note"] = "contracts were read to find owners but only folders were moved"
    print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in result.items()}, indent=2))
    for m in result["moved"] + result["uploads_moved"]:
        print(f"  {m['from']}  ->  {m['to']}")
    for u in result["unowned"]:
        print(f"  left in place (no contract owns it): landing/{u}")
    if not args.no_rewrite:
        q = migrate_quarantine(REPO_ROOT / "harness" / "quarantine", REPO_ROOT / "contracts", dry_run=args.dry_run)
        print(f"quarantine: {len(q['moved'])} folders moved, {len(q['contracts_rewritten'])} contracts rewritten, "
              f"{len(q['unowned'])} left in place")
