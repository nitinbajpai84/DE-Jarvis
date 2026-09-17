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
    assert estate._in_scope("silver", "insurance", "control", known, include_unclassified=False)[0] is False
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
    def run(wide: int = 0, include_unclassified: bool = True):
        db = tmp_path / f"estate_{wide}.duckdb"
        if not db.exists():
            _plant(db, wide)
        monkeypatch.setattr(estate, "sql_connect",
                            lambda target, platform: _CountingConnection(duckdb.connect(str(db)), "duckdb"))
        monkeypatch.setattr(estate, "_known_domains", lambda: ["asset_management", "insurance"])
        estate._SCHEMA_READY.clear()
        _CountingConnection.statements = 0
        summary = estate.run_scan("insurance", "duckdb", tiers=3, include_unclassified=include_unclassified)
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

    # timestamps leave as explicit UTC, whatever the machine's own zone is
    import datetime as dt
    assert scan["started_at"].endswith("Z")
    started = dt.datetime.fromisoformat(scan["started_at"][:-1]).replace(tzinfo=dt.timezone.utc)
    assert abs((dt.datetime.now(dt.timezone.utc) - started).total_seconds()) < 120


def test_abandoned_running_scan_is_closed(synthetic_estate):
    summary, _ = synthetic_estate()
    con, control = estate._con("duckdb", "insurance")
    con.execute(f"insert into {control}.estate_scan values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["deadbeef0000", "insurance", "duckdb", "running", 0, 0, 0, None, None, None, "domain"])
    con.execute(f"insert into {control}.estate_scan values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["a1ive0000000", "insurance", "duckdb", "running", 0, 0, 0, None, None, None, "domain"])
    con.close()
    assert estate.close_abandoned("insurance", "duckdb", live_scan_ids={"a1ive0000000"}) == 1
    rows = {s["scan_id"]: s for s in estate.list_scans("insurance", "duckdb")}
    assert rows["deadbeef0000"]["status"] == "failed"
    assert "interrupted" in rows["deadbeef0000"]["note"]
    assert rows[summary["scan_id"]]["status"] == "completed"


def test_company_scan_never_includes_unclassified_schemas(synthetic_estate):
    """Unclassified estate is admin-only: a company's scan must not census it, and a company
    must not be able to read an admin scan -- neither as "latest" nor by pasting its id."""
    admin, _ = synthetic_estate(include_unclassified=True)
    company, _ = synthetic_estate(include_unclassified=False)

    rep = estate.build_report("insurance", "duckdb", include_unclassified=False)
    assert rep["scan"]["scan_id"] == company["scan_id"] and rep["scan"]["scope"] == "domain"
    assert rep["coverage"]["tables"] == 3                         # own schemas only
    assert rep["coverage"]["unclassified_schemas"] == []
    assert rep["mirrors"] == {"identical": [], "diverged": []}   # the legacy copies are not visible
    assert not any("silver`" in q["question"] and "no domain" in q["question"] for q in rep["open_questions"])

    assert estate.build_report("insurance", "duckdb", admin["scan_id"], include_unclassified=False)["scan"] is None
    assert {s["scan_id"] for s in estate.list_scans("insurance", "duckdb", include_unclassified=False)} == {company["scan_id"]}

    # the admin sees both scans, and the latest one is the company's (narrower) scan
    assert len(estate.list_scans("insurance", "duckdb", include_unclassified=True)) == 2
    assert estate.build_report("insurance", "duckdb", admin["scan_id"])["coverage"]["tables"] == 5


def test_scan_id_from_another_domain_is_not_readable(synthetic_estate):
    summary, _ = synthetic_estate()
    other = estate.build_report("asset_management", "duckdb", summary["scan_id"])
    assert other["scan"] is None


def test_scope_column_migrates_onto_an_existing_scan_table(tmp_path, monkeypatch):
    db = tmp_path / "old.duckdb"
    c = duckdb.connect(str(db))
    c.execute("create schema control")
    c.execute("""create table control.estate_scan (
        scan_id varchar primary key, domain varchar, target varchar, status varchar,
        tier_reached integer, schemas_scanned integer, tables_scanned integer,
        started_at timestamp, ended_at timestamp, note varchar)""")
    c.execute("insert into control.estate_scan values ('old000000000', 'insurance', 'duckdb', "
              "'completed', 3, 6, 48, now(), now(), null)")
    c.close()
    monkeypatch.setattr(estate, "sql_connect",
                        lambda target, platform: SqlConnection(duckdb.connect(str(db)), "duckdb"))
    estate._SCHEMA_READY.clear()
    scans = estate.list_scans("insurance", "duckdb")
    assert scans[0]["scope"] == "domain+unclassified"          # pre-scope scans did include legacy schemas
    assert estate.list_scans("insurance", "duckdb", include_unclassified=False) == []


def test_first_use_schema_setup_is_safe_under_concurrent_requests(tmp_path, monkeypatch):
    """The Control Room opens Discovery with /api/estate/scans and /api/estate/report in
    parallel. On a fresh process both ran the schema setup and `scope` migration at once, and
    DuckDB raised a write-write conflict on the ALTER -- a 502 on the first page load. Without
    the lock this failed in 9 of 15 trials."""
    import threading
    for trial in range(5):
        db = tmp_path / f"race{trial}.duckdb"
        c = duckdb.connect(str(db))
        c.execute("create schema control")
        c.execute("""create table control.estate_scan (
            scan_id varchar primary key, domain varchar, target varchar, status varchar,
            tier_reached integer, schemas_scanned integer, tables_scanned integer,
            started_at timestamp, ended_at timestamp, note varchar)""")
        c.close()
        monkeypatch.setattr(estate, "sql_connect",
                            lambda target, platform, db=db: SqlConnection(duckdb.connect(str(db)), "duckdb"))
        estate._SCHEMA_READY.clear()
        errors = []

        def call():
            try:
                estate.list_scans("insurance", "duckdb")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
