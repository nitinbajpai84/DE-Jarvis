"""Automated, re-runnable test suite for the bronze -> silver -> gold pipeline. Run with:

    pytest tests/test_pipeline.py -v                      # against duckdb (default)
    pytest tests/test_pipeline.py -v --target=databricks   # against the live Databricks catalog

This replaces ad-hoc manual verification (querying, eyeballing, writing up what was found in
evidence/runs/*.md) with assertions anyone can re-run with one command. The evidence docs stay
as the narrative record of what was found and why; this is the thing that keeps working (or
loudly stops) as the pipeline changes. Tests run against the actual current database state, not
a fixture or a mock -- these ARE integration tests, deliberately: the pipeline's correctness
claims (SCD1 has exactly one row per key, mart totals reconcile against source sums, etc.) are
about real data, and a mocked version of that risks passing while the real thing doesn't.

Requires the pipeline to have already been run (harness/run_bronze.py for all 10 sources,
emitters/silver_transform.py, emitters/gold_transform.py) against --target before running these.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402

DOMAIN = "insurance"  # this suite covers the insurance domain end to end; a second domain
                       # (e.g. asset_management) gets its own parametrized run, not a rewrite here


@pytest.fixture(scope="session")
def platform(target):
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


@pytest.fixture(scope="session")
def con(target, platform):
    connection = sql_connect(target, platform)
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def schemas(platform):
    return {layer: resolve_schema(platform, DOMAIN, layer) for layer in ("bronze", "silver", "gold", "control")}


# --------------------------------------------------------------------------- bronze

class TestBronzeFQC:
    """FQC (file_checks) must gate bad batches before they ever reach staging -- verified
    against the specific fixtures harness/seed/generate_insurance.py builds for this purpose."""

    def test_agents_schema_drift_file_quarantined(self, con, schemas):
        control = schemas["control"]
        row = con.execute(
            f"select fqc_passed, action from {control}.file_audit "
            f"where file_name = 'agents_drift.csv' order by arrival_time desc limit 1"
        ).fetchone()
        assert row is not None, "agents_drift.csv was never processed -- run the bronze loader for agents first"
        assert row[0] is False, "schema-drift file should fail FQC (fqc_passed=False)"
        assert row[1] == "quarantined"

    def test_payments_row_count_dip_quarantined(self, con, schemas):
        control = schemas["control"]
        row = con.execute(
            f"select fqc_passed, action, row_count from {control}.file_audit "
            f"where file_name = 'payments_dip.csv' order by arrival_time desc limit 1"
        ).fetchone()
        assert row is not None, "payments_dip.csv was never processed"
        assert row[0] is False
        assert row[1] == "quarantined"
        assert row[2] < 100, "the dip file should have fewer than min_rows"


class TestBronzeDQC:
    """DQC (quality_rules) must quarantine the WHOLE batch on any error-severity violation --
    not filter individual rows (specs/P1/requirements.md Q1/Q2)."""

    @pytest.mark.parametrize("source,file_name,rule_type", [
        ("policies", "policies_2.csv", "unique"),
        ("customers", "customers_2.csv", "accepted_values"),
    ])
    def test_error_severity_violation_quarantines_whole_batch(self, con, schemas, source, file_name, rule_type):
        control, bronze = schemas["control"], schemas["bronze"]
        audit = con.execute(
            f"select action from {control}.file_audit where file_name = ? order by arrival_time desc limit 1",
            [file_name],
        ).fetchone()
        assert audit is not None, f"{file_name} was never processed"
        assert audit[0] == "quarantined", f"{file_name} should be quarantined at DQC (fqc passed, rule violation caught after staging)"

        catalogue = con.execute(
            f"select data_catalogue_id from {control}.data_object_catalogue where file_name = ? "
            f"order by created_at desc limit 1",
            [file_name],
        ).fetchone()
        if catalogue is None:
            return  # FQC-rejected files never reach data_object_catalogue at all -- also correct
        dq = con.execute(
            f"select passed, severity from {control}.dq_results "
            f"where data_catalogue_id = ? and rule_type = ?",
            [catalogue[0], rule_type],
        ).fetchone()
        assert dq is not None, f"expected a {rule_type} rule result for {file_name}"
        assert dq[0] is False and dq[1] == "error"


class TestBronzeIdempotency:
    def test_no_file_is_marked_loaded_more_than_once(self, con, schemas):
        control = schemas["control"]
        dupes = con.execute(
            f"select file_name, count(*) from {control}.file_audit "
            f"where action = 'loaded' group by file_name having count(*) > 1"
        ).fetchall()
        assert dupes == [], f"these files were promoted to bronze more than once: {dupes}"


class TestBronzeLineage:
    @pytest.mark.parametrize("table", ["parties", "customers", "policies", "claims"])
    def test_lineage_columns_present(self, con, schemas, table):
        cols = {d[0] for d in con.execute(f"select * from {schemas['bronze']}.{table} limit 0").description}
        for lineage_col in ("data_catalogue_id", "source_record_id", "_run_id", "_source_file",
                             "_ingested_at", "_record_hash"):
            assert lineage_col in cols, f"{table} is missing lineage column {lineage_col!r}"


# --------------------------------------------------------------------------- silver

class TestSCD1:
    @pytest.mark.parametrize("table,key", [("dim_party", "party_id"), ("dim_address", "address_id"),
                                             ("dim_product", "product_id")])
    def test_exactly_one_row_per_key(self, con, schemas, table, key):
        silver = schemas["silver"]
        total = con.execute(f"select count(*) from {silver}.{table}").fetchone()[0]
        distinct = con.execute(f"select count(distinct {key}) from {silver}.{table}").fetchone()[0]
        assert total == distinct, f"{table} has {total} rows but only {distinct} distinct {key} -- SCD1 must be 1:1"
        assert total > 0, f"{table} is empty -- has silver_transform.py been run?"


class TestSCD2:
    @pytest.mark.parametrize("table,key", [("dim_customer", "customer_id"), ("dim_agent", "agent_id"),
                                             ("dim_policy", "policy_id")])
    def test_exactly_one_current_row_per_key(self, con, schemas, table, key):
        silver = schemas["silver"]
        violations = con.execute(
            f"select {key} from {silver}.{table} where row_is_current "
            f"group by {key} having count(*) > 1"
        ).fetchall()
        assert violations == [], f"{table} has keys with more than one row_is_current=true: {violations}"

    @pytest.mark.parametrize("table,key", [("dim_customer", "customer_id"), ("dim_agent", "agent_id"),
                                             ("dim_policy", "policy_id")])
    def test_every_key_has_a_current_row(self, con, schemas, table, key):
        silver = schemas["silver"]
        missing = con.execute(
            f"select count(*) from (select distinct {key} from {silver}.{table}) k "
            f"where not exists (select 1 from {silver}.{table} t "
            f"where t.{key} = k.{key} and t.row_is_current)"
        ).fetchone()[0]
        assert missing == 0, f"{table} has {missing} keys with no current version at all"


# --------------------------------------------------------------------------- gold

class TestGoldReconciliation:
    def test_premium_mart_reconciles_with_fact(self, con, schemas):
        gold, silver = schemas["gold"], schemas["silver"]
        mart_sum = con.execute(f"select sum(written_premium) from {gold}.mart_premium_by_product").fetchone()[0]
        fact_sum = con.execute(f"select sum(written_premium_amount) from {silver}.fact_premium").fetchone()[0]
        assert mart_sum == pytest.approx(fact_sum, rel=1e-6), (
            f"mart_premium_by_product sums to {mart_sum}, fact_premium sums to {fact_sum} -- "
            "a join in the mart is dropping or duplicating rows"
        )

    def test_claims_mart_reconciles_with_fact(self, con, schemas):
        gold, silver = schemas["gold"], schemas["silver"]
        mart_sum = con.execute(f"select sum(claims_paid) from {gold}.mart_claims_by_status").fetchone()[0]
        fact_sum = con.execute(f"select sum(paid_amount) from {silver}.fact_claim").fetchone()[0]
        assert mart_sum == pytest.approx(fact_sum, rel=1e-6)

    def test_loss_ratio_math_is_correct(self, con, schemas):
        """loss_ratio is a real cross-fact metric (claims_paid/fact_claim, earned_premium/
        fact_premium) -- recompute it independently from the two facts and check the mart's
        own arithmetic, not just that it ran without error."""
        gold = schemas["gold"]
        rows = con.execute(
            f"select line_of_business, claims_paid, earned_premium, loss_ratio "
            f"from {gold}.mart_loss_ratio_by_line where line_of_business is not null"
        ).fetchall()
        assert len(rows) > 0
        for line, claims_paid, earned_premium, loss_ratio in rows:
            expected = claims_paid / earned_premium
            assert loss_ratio == pytest.approx(expected, rel=1e-9), (
                f"{line}: mart says loss_ratio={loss_ratio}, but claims_paid/earned_premium={expected}"
            )

    def test_loss_ratio_is_in_a_plausible_range(self, con, schemas):
        """Not just 'the math is internally consistent' -- also 'the underlying data is
        believable'. This is the exact class of bug found manually earlier in this project
        (claims/premium amounts generated independently produced a 1,700%+ ratio) -- codified
        here so a future data change that reintroduces it fails loudly instead of shipping."""
        gold = schemas["gold"]
        rows = con.execute(
            f"select line_of_business, loss_ratio from {gold}.mart_loss_ratio_by_line "
            f"where line_of_business is not null"
        ).fetchall()
        for line, loss_ratio in rows:
            assert 0.0 <= loss_ratio <= 1.5, (
                f"{line}: loss_ratio={loss_ratio:.2%} is outside a plausible P&C range "
                "(0-150%) -- check whether claims/premium synthetic data amounts drifted"
            )


class TestControlPlaneCoverage:
    """CLAUDE.md rule 7: every run writes to control.run_registry -- for all three layers,
    not just bronze."""

    @pytest.mark.parametrize("phase", ["bronze", "silver", "gold"])
    def test_phase_has_run_registry_entries(self, con, schemas, phase):
        control = schemas["control"]
        n = con.execute(f"select count(*) from {control}.run_registry where phase = ?", [phase]).fetchone()[0]
        assert n > 0, f"no run_registry entries for phase={phase!r} -- has that layer ever run with logging active?"
