"""Step 01 source review loop and Step 02 intent-change regeneration, end to end on temp
directories: upload -> preview -> re-upload -> diff and gap impact -> reject rolls back -> accept;
intent revise -> diff, gap delta, stale architecture -> re-sign. No network, no Gemini.
"""
import json

import pytest

from emitters import intent as intent_mod
from emitters import intent_change, profiler, source_review, versions


ASKED: list = []


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(profiler, "DISCOVERY_DIR", tmp_path / "discovery")
    monkeypatch.setattr(profiler, "HARNESS_DIR", tmp_path / "harness")
    monkeypatch.setattr(intent_mod, "INTENT_DIR", tmp_path / "intent")
    monkeypatch.setattr(profiler, "_extract_document_content",
                        lambda text: {"summary": "a letter", "candidate_fields": {"policy_number": "P1"}})
    import emitters.architecture as arch
    monkeypatch.setattr(arch, "load_architecture", lambda domain: None)
    # never touch the real control plane from here; record what the lookup is asked for
    ASKED.clear()
    asked = ASKED
    monkeypatch.setattr(intent_change, "_estate_leads",
                        lambda domain, target, wide, names: asked.append((target, wide, sorted(names)))
                        or {"scan_id": "s1", "matches": {n.lower(): [{"table": "silver.x", "null_pct": 0.0}] for n in names}, "note": None})
    return tmp_path


CLAIMS_V1 = b"claim_id,policy_id,amount\nC1,P1,100\nC2,P2,\nC3,P3,300\n"
# v2: amount renamed to claim_amount, loss_date added, policy_id now has gaps
CLAIMS_V2 = b"claim_id,policy_id,claim_amount,loss_date\nC1,P1,100,2026-01-02\nC2,,250,2026-01-03\nC3,,300,2026-01-04\nC4,P4,90,2026-01-05\n"


def _intent(points):
    return {"name": "Claims dashboard", "business_outcome": "see claims",
            "reports": [{"name": "Claims by month", "required_data_points": points}]}


# ------------------------------------------------------------------ source review

def test_upload_creates_a_reviewable_first_version(workspace):
    view = source_review.upload_source("testco", "claims", "claims.csv", CLAIMS_V1, by="alice")
    assert view["meta"]["version"] == 1 and view["meta"]["reason"] == "upload"
    assert view["meta"]["created_by"] == "alice" and view["meta"]["review"]["status"] == "pending"
    assert view["meta"]["file"]["name"] == "claims.csv" and len(view["meta"]["file"]["sha256"]) == 64
    assert view["diff"] == {"first_version": True}
    # the reviewer sees the rows themselves, not only statistics
    assert view["profile"]["preview"]["header"] == ["claim_id", "policy_id", "amount"]
    assert view["profile"]["preview"]["rows"][1] == ["C2", "P2", ""]
    # stored in the domain's own upload area
    assert view["profile"]["connection"]["path"].startswith("landing/testco/structured/claims/uploads/")


def test_reupload_diffs_against_what_was_on_record_and_shows_gap_impact(workspace):
    intent_mod.capture_intent("testco", "default", _intent(["amount", "loss_date"]), "alice")
    source_review.upload_source("testco", "claims", "claims.csv", CLAIMS_V1, by="alice")
    view = source_review.upload_source("testco", "claims", "claims_fixed.csv", CLAIMS_V2, by="bob", note="new extract")

    assert view["meta"]["version"] == 2 and view["meta"]["reason"] == "re-upload"
    assert view["meta"]["file"]["note"] == "new extract"
    d = view["diff"]
    assert d["columns_added"] == ["claim_amount", "loss_date"]
    assert d["columns_removed"] == ["amount"]
    assert d["rows"] == {"before": 3, "after": 4}
    assert {"column": "policy_id", "before": 0.0, "after": 50.0} in d["null_shifts"]
    assert "policy_id" in d["keys_lost"] and not d["unchanged"]

    impact = view["impact"]
    assert [g["data_point"] for g in impact["newly_resolved"]] == ["loss_date"]
    assert [g["data_point"] for g in impact["newly_open"]] == ["amount"]

    # the new version is live for gap analysis straight away, but visibly unreviewed
    live = json.loads((workspace / "discovery" / "testco" / "claims.profile.json").read_text())
    assert live["version"] == 2
    assert source_review.review_summary("testco", "claims") == {"version": 2, "review_status": "pending",
                                                                "versions": 2, "pending": 2}


def test_reject_rolls_back_and_accept_signs_off(workspace):
    source_review.upload_source("testco", "claims", "claims.csv", CLAIMS_V1, by="alice")
    source_review.upload_source("testco", "claims", "claims.csv", CLAIMS_V2, by="bob")

    with pytest.raises(ValueError):
        source_review.decide("testco", "claims", 2, "reject", by="carol", comment="  ")   # a reason is required
    out = source_review.decide("testco", "claims", 2, "reject", by="carol", comment="amount column dropped")
    assert out == {"ok": True, "status": "rejected", "live_version": 1}
    live = json.loads((workspace / "discovery" / "testco" / "claims.profile.json").read_text())
    assert [c["name"] for c in live["columns"]] == ["claim_id", "policy_id", "amount"]

    source_review.decide("testco", "claims", 1, "accept", by="carol")
    s = source_review.review_summary("testco", "claims")
    assert s["version"] == 1 and s["review_status"] == "accepted" and s["pending"] == 0

    # a third upload is compared with v1 (on record), not with the rejected v2
    v3 = source_review.upload_source("testco", "claims", "claims.csv", CLAIMS_V2, by="bob")
    assert v3["compared_with"] == 1

    # rejecting every version removes the live profile, so gap analysis stops counting it
    source_review.decide("testco", "claims", 3, "reject", by="carol", comment="still wrong")
    source_review.decide("testco", "claims", 1, "reject", by="carol", comment="withdrawn")
    assert not (workspace / "discovery" / "testco" / "claims.profile.json").exists()


def test_document_upload_and_field_diff(workspace):
    v1 = source_review.upload_source("testco", "letters", "letter1.txt", b"Policy P1 approved.", by="alice")
    assert v1["profile"]["connection"]["type"] == "unstructured"
    assert v1["profile"]["content_extraction"][0]["candidate_fields"] == {"policy_number": "P1"}


def test_unsupported_and_bad_uploads_are_refused(workspace):
    with pytest.raises(ValueError, match="isn.t supported"):
        source_review.upload_source("testco", "claims", "claims.pdf", b"%PDF-1.4", by="alice")
    with pytest.raises(ValueError, match="empty"):
        source_review.upload_source("testco", "claims", "claims.csv", b"", by="alice")
    with pytest.raises(ValueError, match="invalid id"):
        source_review.upload_source("testco", "../escape", "claims.csv", CLAIMS_V1, by="alice")


def test_profiles_from_before_versioning_become_a_baseline(workspace):
    d = workspace / "discovery" / "testco"
    d.mkdir(parents=True)
    old = {"source_id": "legacy", "columns": [{"name": "a", "inferred_type": "string", "null_pct": 0.0}],
           "candidate_keys": [], "total_rows": 1, "connection": {}, "profiled_at": "2026-01-01T00:00:00+00:00"}
    (d / "legacy.profile.json").write_text(json.dumps(old))
    view = source_review.upload_source("testco", "legacy", "legacy.csv", b"a,b\n1,2\n", by="alice")
    assert [m["reason"] for m in view["history"]] == ["baseline", "re-upload"]
    assert view["diff"]["columns_added"] == ["b"]


def test_company_paths_are_confined_to_their_own_files(workspace):
    allowed = source_review.allowed_path
    assert allowed({"type": "file", "path": "landing/testco/structured/claims/uploads/x/claims.csv"}, "testco", False)[0]
    assert not allowed({"type": "file", "path": "landing/claims/*.csv"}, "testco", False)[0]
    assert not allowed({"type": "file", "path": "landing/otherco/structured/claims/*.csv"}, "testco", False)[0]
    assert not allowed({"type": "file", "path": "landing/testco/../otherco/claims/*.csv"}, "testco", False)[0]
    assert allowed({"type": "file", "path": "landing/claims/*.csv"}, "testco", True)[0]          # admin
    assert allowed({"type": "api", "endpoint": "https://x"}, "testco", False)[0]


# ------------------------------------------------------------------ intent change

def test_intent_revision_reports_diff_gap_delta_and_needs_resign(workspace, monkeypatch):
    source_review.upload_source("testco", "claims", "claims.csv", CLAIMS_V1, by="alice")
    intent_mod.capture_intent("testco", "default", _intent(["claim_id", "amount"]), "alice")
    first = intent_change.change_view("testco", "claims-dashboard")
    assert first["diff"] == {"first_version": True} and first["meta"]["reason"] == "captured"

    revised = _intent(["claim_id", "loss_date", "broker_code"])
    revised["definitions"] = [{"term": "open claim", "definition": "not settled"}]
    intent_mod.capture_intent("testco", "default", revised, "bob")

    import emitters.architecture as arch
    monkeypatch.setattr(arch, "load_architecture", lambda domain: {"captured_at": "2020-01-01T00:00:00+00:00"})
    view = intent_change.change_view("testco", "claims-dashboard")

    assert view["meta"]["version"] == 2 and view["meta"]["created_by"] == "bob"
    assert view["compared_with"] == 1
    d = view["diff"]
    assert d["reports_changed"] == [{"report": "Claims by month",
                                     "data_points_added": ["broker_code", "loss_date"],
                                     "data_points_removed": ["amount"]}]
    assert d["definitions_added"] == ["open claim"]
    g = view["gaps"]
    assert sorted(x["data_point"] for x in g["newly_open"]) == ["broker_code", "loss_date"]
    assert [x["data_point"] for x in g["no_longer_needed"]] == ["amount"]
    assert g["open_before"] == 0 and g["open_now"] == 2
    assert view["stale"] and view["stale"][0]["artifact"] == "architecture"
    # the estate is searched for exactly the data points still unresolved
    assert ASKED[-1] == ("duckdb", False, ["broker_code", "loss_date"])
    assert set(view["estate"]["matches"]) == {"broker_code", "loss_date"}

    # only the version in force can be signed
    with pytest.raises(ValueError):
        intent_change.sign("testco", "claims-dashboard", 1, by="carol")
    signed = intent_change.sign("testco", "claims-dashboard", 2, by="carol", comment="ok")
    assert signed["review"]["status"] == "signed" and signed["review"]["by"] == "carol"
    # prior version still readable
    assert versions.load(intent_mod.INTENT_DIR / "testco", "claims-dashboard", 1)["artifact"]["reports"][0]["required_data_points"] == ["claim_id", "amount"]
