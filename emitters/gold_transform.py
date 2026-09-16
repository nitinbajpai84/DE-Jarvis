"""Silver -> Gold: materialises the marts in contracts/semantics/<domain>.gold.yaml (compiled
from docs/templates/jarvis_gold_model_template.xlsx).

Each mart aggregates from its source_fact, joined to the dimensions its grain needs (an SCD2
dimension joins only its row_is_current=true version -- gold reports current state, not full
history). Metrics with no depends_on are additive: computed directly in that aggregation. A
metric WITH depends_on (a ratio, an average) is computed from already-aggregated metric values,
never by re-deriving from row-level data -- and when its dependencies live on a DIFFERENT fact
than the mart's own source_fact (loss_ratio needs claims_paid from fact_claim AND
earned_premium from fact_premium -- a real cross-fact metric, not a hand-hacked one-off), that
other fact is aggregated to the same grain separately and joined in before the ratio is computed.

Same portability discipline as bronze_loader.py/silver_transform.py: one SQL string per query,
authored in duckdb dialect, run through SqlConnection so it works on either target unchanged.
"""
from __future__ import annotations

import argparse
import pathlib
import uuid
from datetime import datetime, timezone
from typing import Any

import yaml

from emitters.control_plane import ensure_control_schema, log_run
from emitters.sql_dialect import SqlConnection, connect as sql_connect, resolve_schema

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_gold(domain: str) -> dict[str, Any]:
    path = REPO_ROOT / "contracts" / "semantics" / f"{domain}.gold.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No gold contract at {path} -- run emitters/gold_model_compiler.py first.")
    return yaml.safe_load(path.read_text())


def _load_platform(target: str) -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


def _table_columns(con: SqlConnection, schema: str, table: str) -> list[str]:
    cur = con.execute(f"select * from {schema}.{table} limit 0")
    return [d[0] for d in cur.description]


_DATE_LIKE_TYPE_CODES = {"date", "timestamp", "datetime"}


def _date_like_columns(con: SqlConnection, schema: str, table: str) -> set[str]:
    """information_schema.columns is ANSI-standard -- works unchanged on duckdb and Databricks
    (same pattern already relied on in webapp/backend/pipeline.py for table discovery)."""
    rows = con.execute(
        "select column_name, data_type from information_schema.columns "
        "where table_schema = ? and table_name = ?", [schema, table],
    ).fetchall()
    return {name for name, dtype in rows if any(t in dtype.lower() for t in _DATE_LIKE_TYPE_CODES)}


def _metric_source_facts(gold: dict) -> dict[str, str]:
    """metric name -> fact table it's additively computed from, inferred from whichever mart
    computes it directly (no depends_on) via that mart's own source_fact."""
    metrics_by_name = {m["name"]: m for m in gold["metrics"]}
    mapping: dict[str, str] = {}
    for mart in gold["marts"]:
        for mname in mart["metrics"]:
            if not metrics_by_name[mname].get("depends_on"):
                mapping.setdefault(mname, mart["source_fact"])
    return mapping


def _resolve_grain_ref(
    con: SqlConnection, silver: str, fact: str, join_dims: list[dict], grain_col: str,
) -> tuple[str, str]:
    """Returns (qualified_column_ref, join_sql_fragment_key) -- finds which table (the fact
    itself, or one of its joined dimensions) actually has this grain column."""
    fact_cols = _table_columns(con, silver, fact)
    if grain_col in fact_cols:
        return f"f.{grain_col}", ""
    for i, jd in enumerate(join_dims):
        dim_cols = _table_columns(con, silver, jd["dimension"])
        if grain_col in dim_cols:
            return f"d{i}.{grain_col}", ""
    raise ValueError(f"grain column {grain_col!r} not found on {fact!r} or its joined dimensions")


def _build_joins(silver: str, join_dims: list[dict]) -> str:
    sql = ""
    alias_by_dim = {}
    for i, jd in enumerate(join_dims):
        alias = f"d{i}"
        alias_by_dim[jd["dimension"]] = alias
        current_filter = f" and {alias}.row_is_current" if jd.get("current_only") else ""
        via = jd.get("via")
        if via:
            via_table, via_col = via.split(".")
            via_alias = alias_by_dim[via_table]
            left_ref = f"{via_alias}.{via_col}"
        else:
            left_ref = f"f.{jd['on']}"
        sql += f" left join {silver}.{jd['dimension']} {alias} on {alias}.{jd['on']} = {left_ref}{current_filter}"
    return sql


def _aggregate_fact(
    con: SqlConnection, gold: str, silver: str, fact: str, join_dims: list[dict],
    grain: list[str], metric_names: list[str], metrics_by_name: dict, temp_name: str,
) -> str:
    grain_refs = [_resolve_grain_ref(con, silver, fact, join_dims, g)[0] for g in grain]
    select_metrics = [f"{metrics_by_name[m]['expression']} as {m}" for m in metric_names]
    joins = _build_joins(silver, join_dims)
    select_list = ", ".join([f"{r} as {g}" for r, g in zip(grain_refs, grain)] + select_metrics)
    group_by = ", ".join(str(i + 1) for i in range(len(grain)))

    con.execute(f"drop table if exists {gold}.{temp_name}")
    con.execute(
        f"create table {gold}.{temp_name} as "
        f"select {select_list} from {silver}.{fact} f{joins} "
        f"group by {group_by}"
    )
    return temp_name


def build_mart(con: SqlConnection, gold: str, silver: str, mart: dict, gold_contract: dict) -> int:
    metrics_by_name = {m["name"]: m for m in gold_contract["metrics"]}
    metric_source_fact = _metric_source_facts(gold_contract)

    direct = [m for m in mart["metrics"] if not metrics_by_name[m].get("depends_on")]
    derived = [m for m in mart["metrics"] if metrics_by_name[m].get("depends_on")]
    own_direct = [m for m in direct if metric_source_fact[m] == mart["source_fact"]]

    base = _aggregate_fact(con, gold, silver, mart["source_fact"], mart["join_dimensions"],
                            mart["grain"], own_direct, metrics_by_name, f"_base_{mart['name']}")

    # any metric (direct, or a derived metric's dependency) that lives on a DIFFERENT fact
    needed_elsewhere: dict[str, list[str]] = {}
    for m in direct:
        if metric_source_fact[m] != mart["source_fact"]:
            needed_elsewhere.setdefault(metric_source_fact[m], []).append(m)
    for m in derived:
        for dep in metrics_by_name[m]["depends_on"]:
            if metric_source_fact.get(dep, mart["source_fact"]) != mart["source_fact"]:
                needed_elsewhere.setdefault(metric_source_fact[dep], []).append(dep)

    result = base
    for other_fact, other_metrics in needed_elsewhere.items():
        other_mart = next(mt for mt in gold_contract["marts"] if mt["source_fact"] == other_fact)
        other_table = _aggregate_fact(con, gold, silver, other_fact, other_mart["join_dimensions"],
                                       mart["grain"], sorted(set(other_metrics)), metrics_by_name,
                                       f"_other_{mart['name']}_{other_fact}")
        join_cond = " and ".join(f"a.{g} = b.{g}" for g in mart["grain"])
        joined_name = f"_joined_{mart['name']}_{other_fact}"
        select_cols = ", ".join([f"coalesce(a.{g}, b.{g}) as {g}" for g in mart["grain"]]
                                 + [f"a.{m}" for m in own_direct]
                                 + [f"b.{m}" for m in sorted(set(other_metrics))])
        con.execute(f"drop table if exists {gold}.{joined_name}")
        con.execute(
            f"create table {gold}.{joined_name} as "
            f"select {select_cols} from {gold}.{result} a full outer join {gold}.{other_table} b "
            f"on {join_cond}"
        )
        result = joined_name

    derived_select = [f"{metrics_by_name[m]['expression']} as {m}" for m in derived]
    extra = (", " + ", ".join(derived_select)) if derived_select else ""
    con.execute(f"drop table if exists {gold}.{mart['name']}")
    con.execute(f"create table {gold}.{mart['name']} as select *{extra} from {gold}.{result}")

    # clean up scratch tables
    for scratch in {base, result} | ({f"_joined_{mart['name']}_{f}" for f in needed_elsewhere}
                                       | {f"_other_{mart['name']}_{f}" for f in needed_elsewhere}):
        if scratch != mart["name"]:
            con.execute(f"drop table if exists {gold}.{scratch}")

    return con.execute(f"select count(*) from {gold}.{mart['name']}").fetchone()[0]


def build_monthly_performance(con: SqlConnection, gold: str, silver: str) -> int:
    """Monthly written/earned premium + claims paid + loss ratio, overall (not split by line --
    45 months x 5 lines would thin each series out for a trend chart). NOT driven by
    gold_model_template.xlsx: it needs a computed grain (date_trunc on a date-typed column
    that lands as a string in bronze/silver per R3 -- specs/P1/requirements.md -- so needs an
    explicit cast too), which emitters/gold_model_compiler.py's contract shape doesn't support
    yet (grain there is always a literal physical column). A one-off, not a pattern to copy for
    the next mart without extending the contract shape first -- documented here, not hidden."""
    # Joined to dim_policy (current) the same way mart_premium_by_product/mart_claims_by_status
    # are, and filtered to a resolved policy the same way -- otherwise this mart and the
    # headline KPIs (which do apply that filter) describe two different populations. Caught by
    # comparing this mart's pooled ratio against the headline ratio before shipping: they
    # should be the same number computed two ways, and weren't, until this join was added.
    con.execute(f"drop table if exists {gold}._monthly_premium")
    con.execute(
        f"create table {gold}._monthly_premium as "
        f"select date_trunc('month', cast(f.premium_period_start as date)) as month, "
        f"sum(f.written_premium_amount) as written_premium, sum(f.earned_premium_amount) as earned_premium "
        f"from {silver}.fact_premium f "
        f"join {silver}.dim_policy dp on dp.policy_id = f.policy_id and dp.row_is_current "
        f"group by 1"
    )
    con.execute(f"drop table if exists {gold}._monthly_claims")
    con.execute(
        f"create table {gold}._monthly_claims as "
        f"select date_trunc('month', cast(f.loss_date as date)) as month, "
        f"sum(f.paid_amount) as claims_paid, count(distinct f.claim_id) as claim_count "
        f"from {silver}.fact_claim f "
        f"join {silver}.dim_policy dp on dp.policy_id = f.policy_id and dp.row_is_current "
        f"group by 1"
    )
    con.execute(f"drop table if exists {gold}.mart_monthly_performance")
    con.execute(
        f"create table {gold}.mart_monthly_performance as "
        f"select coalesce(p.month, c.month) as month, "
        f"coalesce(p.written_premium, 0) as written_premium, coalesce(p.earned_premium, 0) as earned_premium, "
        f"coalesce(c.claims_paid, 0) as claims_paid, coalesce(c.claim_count, 0) as claim_count, "
        f"coalesce(c.claims_paid, 0) / nullif(p.earned_premium, 0) as loss_ratio "
        f"from {gold}._monthly_premium p full outer join {gold}._monthly_claims c on p.month = c.month "
        f"order by 1"
    )
    con.execute(f"drop table if exists {gold}._monthly_premium")
    con.execute(f"drop table if exists {gold}._monthly_claims")
    return con.execute(f"select count(*) from {gold}.mart_monthly_performance").fetchone()[0]


_IMPLEMENTED_BUSINESS_CHECKS = {"variance_vs_history", "dimension_mapping_inconsistency", "new_or_missing_dimension"}


def _log_dq_result(con, control, run_id, rule_type, column, severity, passed, observed, failed_count, domain, client):
    result_id = con.execute(f"select coalesce(max(result_id), 0) + 1 from {control}.dq_results").fetchone()[0]
    con.execute(
        f"insert into {control}.dq_results values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [result_id, run_id, None, rule_type, column or "", severity, passed, observed,
         failed_count, datetime.now(timezone.utc), domain, client],
    )


def eval_business_checks(
    con: SqlConnection, control: str, gold: str, silver: str, mart_name: str, business_rules: list[dict],
    run_id: str, domain: str, client: str,
) -> None:
    """The three business-rule check types named on the reference AWM slides (Variance Check,
    Dimension Mapping Inconsistency Check, New/Missing Dimension Check) -- run against the
    already-built gold mart, logged to control.dq_results the same as bronze/silver DQ results
    so one table answers "what quality checks ran, where, and did they pass" for the whole
    platform. calculated_column_accuracy/trend_anomaly/reconciliation/custom_sql are in the
    schema's enum but not implemented here -- intake_compiler.py already refuses to silently
    claim they're enforced (see its _IMPLEMENTED_BUSINESS_CHECKS)."""
    table = f"{gold}.{mart_name}"
    for br in business_rules:
        check_type = br.get("check_type")
        if check_type not in _IMPLEMENTED_BUSINESS_CHECKS or mart_name not in br.get("applies_to", []):
            continue
        column = br.get("column")
        params = br.get("params") or {}
        severity = br.get("severity", "warning")

        if check_type == "new_or_missing_dimension":
            reference = params.get("reference", "")
            ref_table = reference.split(".", 1)[-1]
            if not ref_table or not column:
                _log_dq_result(con, control, run_id, check_type, column, severity, None,
                               "rule has no reference/column params -- not evaluable", 0, domain, client)
                continue
            ref_cols = _table_columns(con, silver, ref_table)
            if column not in ref_cols:
                _log_dq_result(con, control, run_id, check_type, column, severity, None,
                               f"reference table {ref_table!r} has no column {column!r}", 0, domain, client)
                continue
            current_filter = ' and d."row_is_current"' if "row_is_current" in ref_cols else ""
            failed = con.execute(
                f'select count(*) from {table} m where not exists ('
                f'select 1 from {silver}.{ref_table} d where d."{column}" = m."{column}"{current_filter})'
            ).fetchone()[0]
            _log_dq_result(con, control, run_id, check_type, column, severity, failed == 0, str(failed), failed, domain, client)

        elif check_type == "dimension_mapping_inconsistency":
            allowed = params.get("allowed", [])
            allowed = allowed if isinstance(allowed, list) else [allowed]
            if not column or not allowed:
                _log_dq_result(con, control, run_id, check_type, column, severity, None,
                               "rule has no column/allowed params -- not evaluable", 0, domain, client)
                continue
            values = ", ".join(f"'{v}'" for v in allowed)
            failed = con.execute(f'select count(*) from {table} where "{column}" not in ({values})').fetchone()[0]
            _log_dq_result(con, control, run_id, check_type, column, severity, failed == 0, str(failed), failed, domain, client)

        elif check_type == "variance_vs_history":
            if not column:
                _log_dq_result(con, control, run_id, check_type, column, severity, None,
                               "rule has no column param -- not evaluable", 0, domain, client)
                continue
            max_pct = params.get("max_pct_change")
            date_cols = _date_like_columns(con, gold, mart_name)
            mart_cols = _table_columns(con, gold, mart_name)
            date_col = next((c for c in mart_cols if c in date_cols), None)
            if not date_col or max_pct is None:
                _log_dq_result(con, control, run_id, check_type, column, severity, None,
                               f"no date-typed grain column found on {mart_name!r} (or no max_pct_change param) "
                               "-- variance needs a time dimension to compare periods against, skipped", 0, domain, client)
                continue
            partition_cols = [c for c in mart_cols if c != date_col and c != column]
            partition_by = ("partition by " + ", ".join(f'"{c}"' for c in partition_cols)) if partition_cols else ""
            # Period-over-period (previous row by date), not a rolling lookback_days average --
            # a deliberate simplification of the params.lookback_days concept, not a silent
            # reinterpretation: logged in the observed_value text below so it's visible, not hidden.
            failed = con.execute(
                f'with ranked as ('
                f'  select "{column}" as v, lag("{column}") over ({partition_by} order by "{date_col}") as prev_v '
                f'  from {table}'
                f') select count(*) from ranked where prev_v is not null and prev_v != 0 '
                f'and abs(v - prev_v) / abs(prev_v) * 100 > {float(max_pct)}'
            ).fetchone()[0]
            _log_dq_result(con, control, run_id, check_type, column, severity, failed == 0,
                           f"{failed} period-over-period changes (by {date_col!r}) exceed {max_pct}% "
                           "(period-over-period, not a lookback_days rolling average)", failed, domain, client)


def _build_and_log(con, gold, silver, control, name, domain, client, business_rules, builder, *builder_args) -> int:
    started_at = datetime.now(timezone.utc)
    try:
        n = builder(*builder_args)
    except Exception as exc:  # noqa: BLE001 -- log the failure, then re-raise
        log_run(con, control, run_id=str(uuid.uuid4()), source_id=name, phase="gold",
                started_at=started_at, ended_at=datetime.now(timezone.utc), status="failed",
                error_message=str(exc), files_seen=1, files_accepted=0, files_quarantined=0,
                rows_loaded=0, columns_processed=0, domain=domain, client=client)
        raise
    n_cols = len(_table_columns(con, gold, name))
    run_id = str(uuid.uuid4())
    log_run(con, control, run_id=run_id, source_id=name, phase="gold",
            started_at=started_at, ended_at=datetime.now(timezone.utc), status="completed",
            error_message=None, files_seen=1, files_accepted=1, files_quarantined=0,
            rows_loaded=n, columns_processed=n_cols, domain=domain, client=client)
    if business_rules:
        eval_business_checks(con, control, gold, silver, name, business_rules, run_id, domain, client)
    return n


def run(domain: str, target: str = "duckdb") -> dict[str, int]:
    gold_contract = _load_gold(domain)
    platform = _load_platform(target)
    client = gold_contract.get("client", "default")
    gold = resolve_schema(platform, domain, "gold")
    silver = resolve_schema(platform, domain, "silver")
    control = resolve_schema(platform, domain, "control")

    con = sql_connect(target, platform)
    ensure_control_schema(con, control)
    business_rules = gold_contract.get("business_rules", [])
    try:
        con.execute(f"create schema if not exists {gold}")
        results = {}
        for mart in gold_contract["marts"]:
            results[mart["name"]] = _build_and_log(
                con, gold, silver, control, mart["name"], domain, client, business_rules,
                build_mart, con, gold, silver, mart, gold_contract)
        # build_monthly_performance is hand-coded against insurance's specific fact_premium/
        # fact_claim/dim_policy tables (see its own docstring: "NOT driven by
        # gold_model_template.xlsx"), not something every domain has -- gating it here rather
        # than at the function itself, since the function's own logic is genuinely insurance-
        # specific and shouldn't pretend otherwise. Found by running a second domain through
        # this engine for the first time (verify_intake, via the new intake_compiler path).
        if domain == "insurance":
            results["mart_monthly_performance"] = _build_and_log(
                con, gold, silver, control, "mart_monthly_performance", domain, client, business_rules,
                build_monthly_performance, con, gold, silver)
        return results
    finally:
        con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="insurance")
    parser.add_argument("--target", default="duckdb")
    args = parser.parse_args()
    out = run(args.domain, args.target)
    for name, n in out.items():
        print(f"{name:<28} rows={n}")
