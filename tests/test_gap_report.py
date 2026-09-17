"""Estate-backed gap report: each of the eight gap types planted once, with a known answer."""
import duckdb
import pytest

from emitters import estate, gap_report
from emitters import intent as intent_mod
from emitters import profiler, source_review
from emitters.sql_dialect import SqlConnection


def _plant(db):
    c = duckdb.connect(str(db))
    for s in ("insurance_silver", "silver"):
        c.execute(f"create schema {s}")
    c.execute("create table insurance_silver.dim_agent as select 'A' || i as agent_id from range(20) t(i)")
    # policies: agent A99 resolves to nothing (95%), broker_note 100% empty, customer_email personal data
    c.execute("create table insurance_silver.dim_policy as select 'P' || i as policy_id, "
              "case when i = 29 then 'A99' else 'A' || (i % 19) end as agent_id, "
              "'p' || i || '@x.com' as customer_email, cast(null as varchar) as broker_note from range(30) t(i)")
    c.execute("create table insurance_silver.fact_claim as select 'C' || i as claim_id, 'P' || (i % 30) as policy_id from range(60) t(i)")
    # a legacy copy of dim_policy that disagrees in one email
    c.execute("create table silver.dim_policy as select policy_id, agent_id, "
              "case when policy_id = 'P3' then 'x@x.com' else customer_email end as customer_email, broker_note "
              "from insurance_silver.dim_policy")
    c.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = tmp_path / "estate.duckdb"
    _plant(db)
    monkeypatch.setattr(estate, "sql_connect", lambda target, platform: SqlConnection(duckdb.connect(str(db)), "duckdb"))
    monkeypatch.setattr(estate, "_known_domains", lambda: ["insurance"])
    estate._SCHEMA_READY.clear()
    monkeypatch.setattr(intent_mod, "INTENT_DIR", tmp_path / "intent")
    monkeypatch.setattr(profiler, "DISCOVERY_DIR", tmp_path / "discovery")
    monkeypatch.setattr(profiler, "HARNESS_DIR", tmp_path / "harness")
    monkeypatch.setattr(gap_report, "CONTRACTS_DIR", tmp_path / "contracts")
    src = tmp_path / "contracts" / "sources" / "insurance"
    src.mkdir(parents=True)
    (src / "claims.source.yaml").write_text(
        "source_id: claims\nschema:\n- name: claim_id\n  type: string\n- name: policy_id\n  type: string\n")
    return tmp_path


def _points(report):
    return {r["data_point"]: r for r in report["data_points"]}


def test_every_gap_type_is_found_where_it_was_planted(world):
    estate.run_scan("insurance", "duckdb", tiers=3, include_unclassified=True)
    source_review.upload_source("insurance", "amounts", "amounts.csv", b"claim_amount\n10\n20\n", by="alice")
    intent_mod.capture_intent("insurance", "default", {"name": "Claims", "reports": [
        {"name": "Claims by agent", "required_data_points":
            ["claim_id", "agent_id", "customer_email", "broker_note", "broker_code", "claim_amount"]}],
        "definitions": [{"term": "open claim", "definition": "not settled"}]}, "alice")
    intent_mod.capture_intent("insurance", "default", {"name": "Reserving", "reports": [],
        "definitions": [{"term": "open claim", "definition": "reserve above zero"}]}, "bob")

    r = gap_report.build_gap_report("insurance", "duckdb", include_unclassified=True)
    pts = _points(r)
    types = lambda dp: [g["type"] for g in pts[dp]["gaps"]]  # noqa: E731

    assert pts["claim_id"]["ready"] and pts["claim_id"]["found_in"]["contracts"] == ["claims"]
    assert types("broker_code") == ["missing"]
    assert set(types("agent_id")) == {"integrity", "not_onboarded"}
    # gaps come most severe first, for every data point
    order = {"high": 0, "medium": 1, "low": 2}
    for row in r["data_points"]:
        ranks = [order[g["severity"]] for g in row["gaps"]]
        assert ranks == sorted(ranks)
    # only the column that actually differs between the copies is a conflict -- agent_id and
    # broker_note live in the same diverged table but hold the same values in both copies
    assert "conflicting_copies" not in types("agent_id") + types("broker_note")
    assert set(types("customer_email")) == {"conflicting_copies", "not_onboarded", "sensitive_uncontrolled"}
    assert set(types("broker_note")) == {"not_onboarded", "quality"}
    assert types("claim_amount") == ["unreviewed"]
    assert r["definition_conflicts"][0]["term"] == "open claim"

    s = r["summary"]
    assert s["data_points"] == 6 and s["ready"] == 1
    assert all(s["by_type"][k] >= 1 for k in gap_report.GAP_TYPES)
    assert r["estate_scan"]["scope"] == "domain+unclassified"
    # evidence travels with the gap
    integ = next(g for g in pts["agent_id"]["gaps"] if g["type"] == "integrity")
    assert integ["evidence"]["references"][0]["references"] == "insurance_silver.dim_agent"
    assert integ["owner"] == "The Superstar Data Engineer"

    # accepting the profile clears "unreviewed"
    source_review.decide("insurance", "amounts", 1, "accept", by="carol")
    assert _points(gap_report.build_gap_report("insurance", "duckdb", include_unclassified=True))["claim_amount"]["ready"]


def test_company_view_does_not_use_admin_only_legacy_evidence(world):
    estate.run_scan("insurance", "duckdb", tiers=3, include_unclassified=False)
    intent_mod.capture_intent("insurance", "default", {"name": "Claims", "reports": [
        {"name": "R", "required_data_points": ["customer_email"]}]}, "alice")
    r = gap_report.build_gap_report("insurance", "duckdb", include_unclassified=False)
    # the diverged legacy copy is admin-only, so a company's report can't cite it
    assert "conflicting_copies" not in [g["type"] for g in _points(r)["customer_email"]["gaps"]]
    assert r["estate_scan"]["scope"] == "domain"


def test_without_a_scan_it_says_so_instead_of_calling_everything_missing(world):
    intent_mod.capture_intent("insurance", "default", {"name": "Claims", "reports": [
        {"name": "R", "required_data_points": ["claim_id", "loss_date"]}]}, "alice")
    r = gap_report.build_gap_report("insurance", "duckdb")
    assert r["estate_scan"] is None and "No estate scan" in r["note"]
    miss = _points(r)["loss_date"]["gaps"][0]
    assert miss["type"] == "missing" and "no estate scan yet" in miss["action"]
