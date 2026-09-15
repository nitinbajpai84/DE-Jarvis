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
from typing import Any

import yaml

from emitters.sql_dialect import SqlConnection, connect as sql_connect

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


def run(domain: str, target: str = "duckdb") -> dict[str, int]:
    gold_contract = _load_gold(domain)
    platform = _load_platform(target)
    gold, silver = platform["storage"]["gold"], platform["storage"]["silver"]

    con = sql_connect(target, platform)
    try:
        con.execute(f"create schema if not exists {gold}")
        results = {}
        for mart in gold_contract["marts"]:
            results[mart["name"]] = build_mart(con, gold, silver, mart, gold_contract)
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
