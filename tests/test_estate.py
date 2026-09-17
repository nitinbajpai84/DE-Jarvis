"""Stage 01 estate scan, against a small synthetic estate whose answers are known in advance.

Runs on its own throwaway DuckDB file (not harness/jarvis.duckdb), so it needs no seeded data,
doesn't contend with a running Control Room, and every expected number below is planted.
"""
import duckdb
import pytest

from emitters import estate
from emitters.sql_dialect import SqlConnection


# ------------------------------------------------------------------ heuristics

@pytest.mark.parametrize("col,stem", [("policy_id", "policy"), ("agent_code", "agent"),
                                      ("party_sk", "party"), ("id", None), ("status", None)])
def test_key_stem(col, stem):
    assert estate._key_stem(col) == stem


@pytest.mark.parametrize("table,entity", [("dim_policy", "policy"), ("policies", "policy"),
                                          ("fact_policy_coverage", "policy_coverage"),
                                          ("addresses", "address"), ("class", "class")])
def test_entity_of(table, entity):
    assert estate._entity_of(table) == entity


def test_owns_is_not_a_substring_match():
    assert estate._owns("dim_policy", "policy")
    assert estate._owns("awm_portfolio", "portfolio")
    assert not estate._owns("fact_policy_coverage", "policy")


@pytest.mark.parametrize("col,meta", [("silver_loaded_at", True), ("party_sk", True),
                                      ("_run_id", True), ("row_end_date", True),
                                      ("etl_batch", True), ("created_at", False),
                                      ("updated_at", False), ("premium", False)])
def test_build_meta(col, meta):
    assert estate._is_build_meta(col) is meta


def test_expected_null_and_sensitivity():
    assert estate._expected_null("row_end_date") and estate._expected_null("deleted_at")
    assert not estate._expected_null("email")
    assert estate._classify_column("customer_email") == "contact"
    assert estate._classify_column("nric") == "national_id"
    assert estate._classify_column("premium") is None


def test_scope_excludes_control_and_other_tenants():
    known = ["asset_management", "insurance"]
    assert estate._in_scope("insurance_silver", "insurance", "control", known) == (True, "insurance")
    assert estate._in_scope("silver", "insurance", "control", known) == (True, "unclassified")
    assert estate._in_scope("asset_management_gold", "insurance", "control", known)[0] is False
    assert estate._in_scope("control", "insurance", "control", known)[0] is False


# ------------------------------------------------------------------ end to end

class _CountingConnection(SqlConnection):
    statements = 0

    def execute(self, sql, params=None):
        type(self).statements += 1
        return super().execute(sql, params)


def _plant(path, wide: int) -> None:
    c = duckdb.connect(str(path))
    for s in ("insurance_silver", "silver", "asset_management_silver"):
        c.execute(f"create schema {s}")
    extra = ", ".join(f"attr_{i} varchar" for i in range(wide))
    c.execute(f"create table insurance_silver.dim_agent (agent_id varchar, agent_name varchar, {extra})")
    c.execute("insert into insurance_silver.dim_agent (agent_id, agent_name) "
              "select 'A' || i, 'agent ' || i from range(20) t(i)")
    c.execute("create table insurance_silver.dim_policy (policy_id varchar, agent_id varchar, "
              "customer_email varchar, deleted_at timestamp, silver_loaded_at timestamp)")
    # 30 policies over 20 distinct agent ids, one of which (A99) is in no agent row: 95% resolve
    c.execute("insert into insurance_silver.dim_policy select 'P' || i, "
              "case when i = 29 then 'A99' else 'A' || (i % 19) end, 'p' || i || '@x.com', null, now() "
              "from range(30) t(i)")
    c.execute("create table insurance_silver.fact_claim (claim_id varchar, policy_id varchar, amount double)")
    c.execute("insert into insurance_silver.fact_claim select 'C' || i, 'P' || (i % 30), i * 1.5 from range(60) t(i)")
    # legacy copies: dim_agent identical in business content; dim_policy differs in one email,
    # and in silver_loaded_at everywhere (build metadata, which must not count as divergence)
    c.execute("create table silver.dim_agent as select * from insurance_silver.dim_agent")
    c.execute("create table silver.dim_policy as select policy_id, agent_id, "
              "case when policy_id = 'P3' then 'changed@x.com' else customer_email end as customer_email, "
              "deleted_at, now() - interval 1 day as silver_loaded_at from insurance_silver.dim_policy")
    # another tenant's data: must never appear in an insurance scan
    c.execute("create table asset_management_silver.dim_portfolio (portfolio_id varchar)")
    c.execute("insert into asset_management_silver.dim_portfolio values ('X')")
    c.close()


@pytest.fixture
def synthetic_estate(tmp_path, monkeypatch):
    def run(wide: int = 0):
        db = tmp_path / f"estate_{wide}.duckdb"
        _plant(db, wide)
        monkeypatch.setattr(estate, "sql_connect",
                            lambda target, platform: _CountingConnection(duckdb.connect(str(db)), "duckdb"))
        monkeypatch.setattr(estate, "_known_domains", lambda: ["asset_management", "insurance"])
        estate._SCHEMA_READY.clear()
        _CountingConnection.statements = 0
        summary = estate.run_scan("insurance", "duckdb", tiers=3)
        return summary, _CountingConnection.statements
    return run


def test_scan_finds_what_was_planted(synthetic_estate):
    summary, _ = synthetic_estate()
    assert summary["status"] == "completed" and summary["tier_reached"] == 3
    report = estate.build_report("insurance", "duckdb", summary["scan_id"])

    cov = report["coverage"]
    assert cov["tables"] == 5                      # 3 domain + 2 legacy; the other tenant excluded
    assert cov["unclassified_schemas"] == ["silver"]

    rels = report["relationships"]
    pairs = {(r["from_table"], r["from_column"], r["to_table"]): kind
             for kind, rows in rels.items() for r in rows}
    assert pairs[("fact_claim", "policy_id", "dim_policy")] == "intact"
    assert pairs[("dim_policy", "agent_id", "dim_agent")] == "orphans"
    assert not any(t == "dim_agent" and to == "dim_policy" for (t, _c, to) in pairs)   # never reversed

    mirrors = report["mirrors"]
    identical = {m["table"] for m in mirrors["identical"]}
    diverged = {m["table"]: m for m in mirrors["diverged"]}
    assert "silver.dim_agent" in identical
    assert "silver.dim_policy" in diverged         # one changed email; loaded_at alone would not do it

    contact = next(b for b in report["sensitivity"]["by_class"] if b["class"] == "contact")
    assert contact["columns"] == ["insurance_silver.dim_policy.customer_email"]   # legacy copy not double-counted

    # deleted_at is 100% null by design: shown as expected, never as a hotspot
    exp = {q["column"] for q in report["quality"]["expected_nulls"]}
    hot = {q["column"] for q in report["quality"]["hotspots"]}
    assert "insurance_silver.dim_policy.deleted_at" in exp
    assert "insurance_silver.dim_policy.deleted_at" not in hot


def test_scan_writes_are_batched(synthetic_estate):
    """Regression guard for the Databricks round-trip bug: statements issued must not grow with
    the number of columns. A 100-column-wider estate should cost the same number of statements."""
    _, narrow = synthetic_estate(wide=0)
    _, wide = synthetic_estate(wide=100)
    assert wide == narrow


def test_empty_estate_claims_no_tiers_it_did_not_run(tmp_path, monkeypatch):
    db = tmp_path / "empty.duckdb"
    monkeypatch.setattr(estate, "sql_connect",
                        lambda target, platform: SqlConnection(duckdb.connect(str(db)), "duckdb"))
    monkeypatch.setattr(estate, "_known_domains", lambda: ["insurance"])
    estate._SCHEMA_READY.clear()
    summary = estate.run_scan("insurance", "duckdb", tiers=4)
    assert summary["tier_reached"] == 1
    scan = estate.list_scans("insurance", "duckdb")[0]
    assert scan["tier_reached"] == 1 and "no tables found" in scan["note"]


def test_abandoned_running_scan_is_closed(synthetic_estate):
    summary, _ = synthetic_estate()
    con, control = estate._con("duckdb", "insurance")
    con.execute(f"insert into {control}.estate_scan values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["deadbeef0000", "insurance", "duckdb", "running", 0, 0, 0, None, None, None])
    con.close()
    assert estate.close_abandoned("insurance", "duckdb", live_scan_id=None) == 1
    rows = {s["scan_id"]: s for s in estate.list_scans("insurance", "duckdb")}
    assert rows["deadbeef0000"]["status"] == "failed"
    assert "interrupted" in rows["deadbeef0000"]["note"]
    assert rows[summary["scan_id"]]["status"] == "completed"
