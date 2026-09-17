"""Landing zone layout: landing/<domain>/<category>/<source_id>/ -- migration from the flat
layout, Excel reading, uploads by kind, and land-then-load for API and database sources."""
import json

import pytest
import yaml

from emitters import landing, profiler, source_review


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(profiler, "HARNESS_DIR", tmp_path / "harness")
    monkeypatch.setattr(profiler, "DISCOVERY_DIR", tmp_path / "contracts" / "discovery")
    return tmp_path


def _contract(root, domain, source_id, ctype="file", path=None):
    d = root / "contracts" / "sources" / domain
    d.mkdir(parents=True, exist_ok=True)
    path = path or f"harness/landing/{source_id}/{source_id}_*.csv"
    (d / f"{source_id}.source.yaml").write_text(
        "# a comment the rewrite must keep\n"
        f"source_id: {source_id}\ndomain: {domain}\nconnection:\n  type: {ctype}\n  path: {path}\n"
        f"file_checks:\n  quarantine_path: harness/quarantine/{source_id}/\n", encoding="utf-8")


def test_migration_separates_companies_and_kinds_and_is_idempotent(harness):
    lz = harness / "harness" / "landing"
    for src, fname in [("claims", "claims_1.csv"), ("awm_positions", "awm_positions_1.csv"),
                       ("letters", "a.txt"), ("orphan", "x.csv")]:
        (lz / src).mkdir(parents=True)
        (lz / src / fname).write_text("a,b\n1,2\n")
    (lz / "uploads" / "insurance" / "claims" / "u1").mkdir(parents=True)
    (lz / "uploads" / "insurance" / "claims" / "u1" / "fix.csv").write_text("a\n1\n")
    _contract(harness, "insurance", "claims")
    _contract(harness, "asset_management", "awm_positions")
    _contract(harness, "insurance", "letters", ctype="unstructured", path="harness/landing/letters/*.txt")
    disc = harness / "contracts" / "discovery" / "insurance"
    disc.mkdir(parents=True)
    (disc / "claims.profile.json").write_text(json.dumps({"connection": {"type": "file", "path": "landing/uploads/insurance/claims/u1/fix.csv"}}))

    r = landing.migrate(lz, harness / "contracts", harness / "contracts" / "discovery")

    assert (lz / "insurance" / "structured" / "claims" / "claims_1.csv").exists()
    assert (lz / "asset_management" / "structured" / "awm_positions" / "awm_positions_1.csv").exists()
    assert (lz / "insurance" / "unstructured" / "letters" / "a.txt").exists()
    assert (lz / "insurance" / "structured" / "claims" / "uploads" / "u1" / "fix.csv").exists()
    assert not (lz / "uploads").exists() and not (lz / "claims").exists()
    # a folder no contract claims is never assigned to a company by guesswork
    assert r["unowned"] == ["orphan"] and (lz / "orphan" / "x.csv").exists()

    c = (harness / "contracts" / "sources" / "insurance" / "claims.source.yaml").read_text()
    assert c.startswith("# a comment the rewrite must keep")
    assert yaml.safe_load(c)["connection"]["path"] == "harness/landing/insurance/structured/claims/claims_*.csv"
    prof = json.loads((disc / "claims.profile.json").read_text())
    assert prof["connection"]["path"] == "landing/insurance/structured/claims/uploads/u1/fix.csv"

    again = landing.migrate(lz, harness / "contracts", harness / "contracts" / "discovery")
    assert again["moved"] == [] and again["contracts_rewritten"] == [] and again["unowned"] == ["orphan"]

    q = harness / "harness" / "quarantine"
    (q / "claims").mkdir(parents=True)
    (q / "claims" / "bad.csv").write_text("x")
    qr = landing.migrate_quarantine(q, harness / "contracts")
    assert (q / "insurance" / "claims" / "bad.csv").exists()
    assert "quarantine_path: harness/quarantine/insurance/claims/" in (harness / "contracts" / "sources" / "insurance" / "claims.source.yaml").read_text()
    assert landing.migrate_quarantine(q, harness / "contracts")["moved"] == []


def test_excel_reads_like_its_csv_export(tmp_path):
    import datetime as dt
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["policy_id", "premium", "start_date", "note"])
    ws.append(["P1", 1200.0, dt.datetime(2026, 1, 2), None])
    ws.append(["P2", 99.5, dt.date(2026, 2, 3), "renewal"])
    ws.append([None, None, None, None])      # trailing empty row is not data
    path = tmp_path / "policies.xlsx"
    wb.save(path)
    header, rows = landing.read_tabular(path)
    assert header == ["policy_id", "premium", "start_date", "note"]
    assert rows == [["P1", "1200", "2026-01-02", ""], ["P2", "99.5", "2026-02-03", "renewal"]]


def test_uploads_land_by_company_then_kind(harness, monkeypatch):
    monkeypatch.setattr(profiler, "_extract_document_content", lambda text: {"summary": "", "candidate_fields": {}})
    from openpyxl import Workbook
    import io
    wb = Workbook()
    wb.active.append(["a", "b"])
    wb.active.append([1, 2])
    buf = io.BytesIO()
    wb.save(buf)

    xl = source_review.upload_source("testco", "sheet", "sheet.xlsx", buf.getvalue(), by="alice")
    assert xl["profile"]["connection"]["path"].startswith("landing/testco/structured/sheet/uploads/")
    assert xl["profile"]["connection"]["format"] == "xlsx"
    assert [c["name"] for c in xl["profile"]["columns"]] == ["a", "b"]

    doc = source_review.upload_source("testco", "memo", "memo.html", b"<p>hello</p>", by="alice")
    assert doc["profile"]["connection"]["path"].startswith("landing/testco/unstructured/memo/uploads/")

    inv = landing.inventory("testco")
    by_cat = {c["category"]: c["sources"] for c in inv["categories"]}
    assert [s["source_id"] for s in by_cat["structured"]] == ["sheet"]
    assert by_cat["structured"][0]["uploads"] == 1 and by_cat["structured"][0]["files"] == 0
    assert [s["source_id"] for s in by_cat["unstructured"]] == ["memo"]
    assert by_cat["api"] == [] and by_cat["database"] == []


def test_api_and_database_pulls_land_a_raw_copy_first(harness, monkeypatch):
    from emitters import bronze_loader
    monkeypatch.setattr(bronze_loader, "REPO_ROOT", harness)
    payload = json.dumps({"data": {"rows": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]}}).encode()

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return payload

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=30: Resp())
    contract = {"source_id": "rain", "domain": "testco", "connection": {"type": "api", "endpoint": "https://x"},
                "schema": [{"name": "id"}, {"name": "v"}]}
    [batch] = bronze_loader._extract_api_batches(contract)
    landed = sorted((harness / "harness" / "landing" / "testco" / "api" / "rain").glob("rain_*.json"))
    assert len(landed) == 1 and landed[0].read_bytes() == payload
    assert batch.location == str(landed[0]) and batch.rows == [[1, "a"], [2, "b"]]

    path = landing.land_database_extract("testco", "trips", ["id", "fare"], [[1, 9.5], [2, None]])
    assert path.parent == harness / "harness" / "landing" / "testco" / "database" / "trips"
    assert path.read_text().splitlines() == ["id,fare", "1,9.5", "2,"]
