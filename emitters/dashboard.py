"""Step 04 visualisation -- Agent 6's real capability.

The gold contract schema ($schema: jarvis/gold/v1) has ALWAYS had a `dashboards:` field --
insurance's compiled contract already carries one real, hand-authored dashboard (4 tiles: two
bars, a KPI, a table), left over from the older gold_model_compiler.py this project replaced in
its Phase A migration. Confirmed by reading it, not assumed: nothing anywhere in this codebase
ever reads `dashboards:` to render anything (grepped emitters/, webapp/, agents/ -- zero hits
besides the one place `intake_compiler.py` writes an empty list). asset_management's contract
carries `dashboards: []` for the same reason -- the current compiler never populates the field,
because no intake-workbook sheet feeds it.

So Agent 6's job here is two things, not one:
  1. RENDER whatever dashboard spec a domain already has, for real, against live gold data --
     completing wiring this project already half-built.
  2. PROPOSE a dashboard, in the exact same tile schema, for a domain that has none yet --
     mechanically derived from the domain's own declared metrics and marts, never invented.

Tile types (unchanged from the existing schema): bar (metric x one grain dim, optional series),
kpi (a single headline number), table (a mart's full grain + metrics). Proposal rule, applied
per metric: 0 grain dims -> kpi, 1 grain dim -> bar (this also covers non-additive ratio
metrics like loss_ratio, which naturally read as "compare this ratio across categories" rather
than one fake blended number), 2+ grain dims -> table. Plus one full-mart table tile per mart,
mirroring the shape of insurance's own hand-authored table tile.

KPI rollup for a non-additive metric (a ratio/formula with `depends_on`) is the one place this
needs real care: summing per-group ratios is mathematically wrong (a loss ratio isn't additive
across lines of business). Rather than invent a different formula, this substitutes SUM(dep)
for each dependency NAME inside the metric's own declared `expression` string -- the same
formula the contract already asserts is correct, just evaluated at zero grain instead of the
mart's grain. mart_loss_ratio_by_line's live columns (claims_paid, earned_premium) confirm this
works: gold_transform.py's own mart-builder already includes every dependency of a derived
metric as a real column, whether or not the contract's mart.metrics list names it explicitly.
"""
from __future__ import annotations

import pathlib
import re
import sys
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402


def _load_platform(target: str) -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


def _load_gold_contract(domain: str) -> dict[str, Any] | None:
    path = REPO_ROOT / "contracts" / "semantics" / f"{domain}.gold.yaml"
    return yaml.safe_load(path.read_text()) if path.exists() else None


def _table_columns(con, schema: str, table: str) -> set[str]:
    rows = con.execute(
        "select column_name from information_schema.columns "
        "where table_schema = ? and table_name = ?", [schema, table],
    ).fetchall()
    return {r[0] for r in rows}


def _mart_declaring(gold_contract: dict, metric_name: str) -> dict[str, Any] | None:
    for m in gold_contract.get("marts", []):
        if metric_name in (m.get("metrics") or []):
            return m
    return None


# --------------------------------------------------------------------------- proposal

def propose_dashboard(domain: str) -> dict[str, Any]:
    """Mechanically derives a dashboard spec from the domain's own declared metrics/marts --
    zero invention, every tile traces to a contract fact. Returns the SAME tile shape as an
    already-authored dashboard (see insurance's), so a proposal is immediately renderable and,
    if accepted, savable back into the contract with no format translation."""
    gold_contract = _load_gold_contract(domain)
    if gold_contract is None:
        return {"domain": domain, "ok": False, "reason": "no gold contract for this domain"}

    metrics_by_name = {m["name"]: m for m in gold_contract.get("metrics", [])}
    tiles: list[dict[str, Any]] = []
    skipped: list[str] = []

    for metric in gold_contract.get("metrics", []):
        mart = _mart_declaring(gold_contract, metric["name"])
        if mart is None:
            skipped.append(f"{metric['name']}: not declared in any mart, nothing to chart")
            continue
        grain = metric.get("grain", [])
        if len(grain) == 0:
            tiles.append({"type": "kpi", "metric": metric["name"]})
        elif len(grain) == 1:
            tiles.append({"type": "bar", "metric": metric["name"], "x": grain[0]})
        else:
            tiles.append({"type": "table", "by": grain, "metric": metric["name"]})

    for mart in gold_contract.get("marts", []):
        tiles.append({"type": "table", "mart": mart["name"], "by": mart["grain"]})

    return {
        "domain": domain, "ok": True, "name": f"{domain.replace('_', ' ').title()} Overview",
        "tiles": tiles, "skipped": skipped, "proposed": True,
    }


def get_dashboard(domain: str) -> dict[str, Any]:
    """The dashboard to show: whatever's already in the gold contract, or a fresh proposal if
    none exists yet. Never silently prefers a proposal over a saved, human-reviewed one."""
    gold_contract = _load_gold_contract(domain)
    if gold_contract and gold_contract.get("dashboards"):
        d = gold_contract["dashboards"][0]
        return {"domain": domain, "ok": True, "name": d.get("name", f"{domain} dashboard"),
                "tiles": d.get("tiles", []), "skipped": [], "proposed": False}
    return propose_dashboard(domain)


def save_dashboard(domain: str, name: str, tiles: list[dict]) -> dict[str, Any]:
    """Writes an accepted (or edited) dashboard into contracts/semantics/<domain>.gold.yaml's
    dashboards: field -- the field the schema has always had, now actually used. Replaces any
    existing dashboards list for this domain (there's only ever one dashboard here; a domain
    with a richer set of named dashboards is future work, not pretended at)."""
    path = REPO_ROOT / "contracts" / "semantics" / f"{domain}.gold.yaml"
    if not path.exists():
        return {"ok": False, "reason": f"no gold contract for domain {domain!r}"}
    contract = yaml.safe_load(path.read_text())
    contract["dashboards"] = [{"name": name, "tiles": tiles}]
    path.write_text(yaml.safe_dump(contract, sort_keys=False, allow_unicode=True))
    return {"ok": True, "path": str(path)}


# --------------------------------------------------------------------------- rendering

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _rollup_expression(metric: dict) -> str:
    """The metric's own declared expression, with each dependency NAME replaced by SUM(name) --
    reusing the contract's own formula rather than asserting a new one. Only touches whole-word
    identifier matches, so it can't accidentally rewrite part of a longer name."""
    expr = metric["expression"]
    for dep in metric.get("depends_on", []):
        expr = re.sub(rf"\b{re.escape(dep)}\b", f"SUM({dep})", expr)
    return expr


def _render_bar(con, gold: str, gold_contract: dict, tile: dict) -> dict[str, Any]:
    """GROUPs BY the tile's chosen dimensions rather than trusting the mart is already at
    exactly that grain -- found a real case where it isn't: insurance's own hand-authored bar
    tile charts written_premium by line_of_business alone, but the mart backing it
    (mart_premium_by_product) is grained by line_of_business AND transaction_type. Selecting
    raw rows without aggregating would have shown one bar per (line_of_business,
    transaction_type) pair, not one per line_of_business -- caught by checking the actual
    output against the mart's real declared grain, not assumed correct."""
    metric_name = tile["metric"]
    metric = next((m for m in gold_contract["metrics"] if m["name"] == metric_name), None)
    mart = _mart_declaring(gold_contract, metric_name)
    if metric is None or mart is None:
        return {**tile, "error": f"metric {metric_name!r} is not in any mart"}

    x, series = tile["x"], tile.get("series")
    group_cols = [x] + ([series] if series else [])
    if metric.get("depends_on"):
        live_cols = _table_columns(con, gold, mart["name"])
        missing = [d for d in metric["depends_on"] if d not in live_cols]
        if missing:
            return {**tile, "mart": mart["name"], "error":
                    f"cannot chart {metric_name!r} by {group_cols} -- dependency column(s) "
                    f"{missing} aren't in {mart['name']}"}
        value_expr = _rollup_expression(metric)
    else:
        value_expr = f'SUM("{metric_name}")'

    group_list = ", ".join(f'"{c}"' for c in group_cols)
    order = ", ".join(str(i + 1) for i in range(len(group_cols)))
    rows = con.execute(
        f"select {group_list}, {value_expr} as v from {gold}.{mart['name']} "
        f"group by {group_list} order by {order}"
    ).fetchall()
    data = [{"x": r[0], "series": r[1] if series else None, "value": r[-1]} for r in rows]
    return {**tile, "mart": mart["name"], "data": data}


def _render_kpi(con, gold: str, gold_contract: dict, tile: dict) -> dict[str, Any]:
    metric_name = tile["metric"]
    metric = next((m for m in gold_contract["metrics"] if m["name"] == metric_name), None)
    mart = _mart_declaring(gold_contract, metric_name)
    if metric is None or mart is None:
        return {**tile, "error": f"metric {metric_name!r} is not in any mart"}

    if not metric.get("depends_on"):
        value = con.execute(f'select sum("{metric_name}") from {gold}.{mart["name"]}').fetchone()[0]
        return {**tile, "mart": mart["name"], "value": value, "format": metric.get("format")}

    # Non-additive: only safe if this mart's live table actually carries every dependency
    # column -- checked against the real table, not assumed from the contract's mart.metrics
    # list (which, per this module's docstring, doesn't always enumerate them).
    live_cols = _table_columns(con, gold, mart["name"])
    missing = [d for d in metric["depends_on"] if d not in live_cols]
    if missing:
        return {**tile, "mart": mart["name"], "error":
                f"cannot roll up {metric_name!r} to one number -- dependency column(s) "
                f"{missing} aren't in {mart['name']} -- no single total for a ratio computed "
                f"across marts"}
    value = con.execute(f"select {_rollup_expression(metric)} from {gold}.{mart['name']}").fetchone()[0]
    return {**tile, "mart": mart["name"], "value": value, "format": metric.get("format")}


def _render_table(con, gold: str, gold_contract: dict, tile: dict) -> dict[str, Any]:
    if "mart" in tile:
        mart = next((m for m in gold_contract["marts"] if m["name"] == tile["mart"]), None)
        if mart is None:
            return {**tile, "error": f"mart {tile['mart']!r} not declared"}
        cols = mart["grain"] + mart["metrics"]
    elif "metric" in tile:
        mart = _mart_declaring(gold_contract, tile["metric"])
        if mart is None:
            return {**tile, "error": f"metric {tile['metric']!r} is not in any mart"}
        cols = tile["by"] + [tile["metric"]]
    else:
        # A table tile can name neither a mart nor a metric -- just a `by`, e.g. insurance's own
        # hand-authored {"type": "table", "by": ["line_of_business"]}. Resolved by finding the
        # one mart whose own declared grain matches `by` exactly, rather than guessing which of
        # several candidate marts was meant.
        by = tile.get("by", [])
        candidates = [m for m in gold_contract["marts"] if m["grain"] == by]
        if len(candidates) != 1:
            return {**tile, "error": f"table tile names no mart/metric and `by`={by} matches "
                                     f"{len(candidates)} marts (need exactly 1) -- ambiguous"}
        mart = candidates[0]
        cols = mart["grain"] + mart["metrics"]
    select_list = ", ".join(f'"{c}"' for c in cols)
    order = ", ".join(str(i + 1) for i in range(len(cols)))
    rows = con.execute(
        f"select {select_list} from {gold}.{mart['name']} order by {order}"
    ).fetchall()
    # Per-column display format, resolved from each column's own metric declaration where one
    # exists (a grain/dimension column has none, and correctly shows as plain text) -- without
    # this every numeric column defaulted to plain 2-decimal formatting, showing loss_ratio as
    # "0.81" instead of "81.0%" and claims_paid with no currency marker. Caught by looking at
    # the actual rendered table, not assumed correct from the bar/KPI tiles working.
    metrics_by_name = {m["name"]: m for m in gold_contract.get("metrics", [])}
    formats = [metrics_by_name[c]["format"] if c in metrics_by_name and "format" in metrics_by_name[c]
               else None for c in cols]
    return {**tile, "mart": mart["name"], "columns": cols, "formats": formats,
            "rows": [list(r) for r in rows]}


def render_dashboard(domain: str, target: str) -> dict[str, Any]:
    """Resolves every tile in get_dashboard(domain) against LIVE gold-layer data. Tiles that
    can't be resolved (metric not in any mart, a KPI that genuinely has no safe single total)
    carry an "error" field instead of a fabricated number -- the caller renders that honestly,
    not silently."""
    dashboard = get_dashboard(domain)
    if not dashboard.get("ok", True):
        return dashboard
    gold_contract = _load_gold_contract(domain)
    platform = _load_platform(target)
    gold = resolve_schema(platform, domain, "gold")
    con = sql_connect(target, platform)
    try:
        rendered = []
        for tile in dashboard["tiles"]:
            if tile["type"] == "bar":
                rendered.append(_render_bar(con, gold, gold_contract, tile))
            elif tile["type"] == "kpi":
                rendered.append(_render_kpi(con, gold, gold_contract, tile))
            elif tile["type"] == "table":
                rendered.append(_render_table(con, gold, gold_contract, tile))
            else:
                rendered.append({**tile, "error": f"unknown tile type {tile['type']!r}"})
        return {**dashboard, "tiles": rendered, "target": target}
    finally:
        con.close()
