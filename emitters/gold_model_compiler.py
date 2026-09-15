"""Compiles docs/templates/jarvis_gold_model_template.xlsx into
contracts/semantics/<domain>.gold.yaml -- same shape as the pre-existing hand-authored
sales.gold.yaml. Same discipline as the bronze/silver compilers: only emits what's in the
workbook, stops on a broken reference.
"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

import yaml
from openpyxl import load_workbook

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEMANTICS_DIR = REPO_ROOT / "contracts" / "semantics"


def _sheet_rows(ws) -> list[dict[str, Any]]:
    header = [c.value for c in ws[1]]
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is None:
            continue
        rows.append(dict(zip(header, row)))
    return rows


def _split_list(v: Any) -> list[str]:
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def compile_gold(xlsx_path: pathlib.Path, domain: str) -> dict[str, Any]:
    wb = load_workbook(xlsx_path, data_only=True)
    metric_rows = _sheet_rows(wb["Metrics"])
    rule_rows = _sheet_rows(wb["Business Rules"])
    mart_rows = _sheet_rows(wb["Marts"])
    tile_rows = _sheet_rows(wb["Dashboards"])

    metric_names = {m["name"] for m in metric_rows}
    errors = []
    for m in metric_rows:
        for dep in _split_list(m.get("depends_on")):
            if dep not in metric_names:
                errors.append(f"metric {m['name']!r} depends_on unknown metric {dep!r}")
    for r in rule_rows:
        for applies in _split_list(r["applies_to"]):
            if applies not in metric_names:
                errors.append(f"business rule {r['id']!r} applies_to unknown metric {applies!r}")
    for mart in mart_rows:
        for metric in _split_list(mart["metrics"]):
            if metric not in metric_names:
                errors.append(f"mart {mart['name']!r} references unknown metric {metric!r}")
    if errors:
        raise ValueError("Gold model has broken references:\n  " + "\n  ".join(errors))

    gold: dict[str, Any] = {
        "$schema": "jarvis/gold/v1", "domain": domain,
        "consumers": ["bi_dashboard", "finance_export"],
        "metrics": [], "business_rules": [], "marts": [], "dashboards": [],
    }

    for m in metric_rows:
        entry: dict[str, Any] = {"name": m["name"], "expression": m["expression"],
                                  "grain": _split_list(m["grain"])}
        deps = _split_list(m.get("depends_on"))
        if deps:
            entry["depends_on"] = deps
        if m.get("non_additive"):
            entry["non_additive"] = True
        if m.get("format"):
            entry["format"] = m["format"]
        if m.get("owner"):
            entry["owner"] = m["owner"]
        gold["metrics"].append(entry)

    for r in rule_rows:
        gold["business_rules"].append({"id": r["id"], "statement": r["statement"],
                                        "applies_to": _split_list(r["applies_to"])})

    for mart in mart_rows:
        gold["marts"].append({
            "name": mart["name"], "source_fact": mart["source_fact"],
            "join_dimensions": json.loads(mart["join_dimensions_json"] or "[]"),
            "grain": _split_list(mart["grain"]), "metrics": _split_list(mart["metrics"]),
            "materialisation": mart["materialisation"],
        })

    dashboards: dict[str, list] = {}
    for t in tile_rows:
        tile = {"type": t["tile_type"]}
        if t.get("metric"):
            tile["metric"] = t["metric"]
        if t.get("x"):
            tile["x"] = t["x"]
        if t.get("series"):
            tile["series"] = t["series"]
        if t.get("by"):
            tile["by"] = _split_list(t["by"])
        dashboards.setdefault(t["dashboard_name"], []).append(tile)
    gold["dashboards"] = [{"name": name, "tiles": tiles} for name, tiles in dashboards.items()]

    return gold


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", default=str(REPO_ROOT / "docs" / "templates" / "jarvis_gold_model_template.xlsx"))
    parser.add_argument("--domain", default="insurance")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    gold = compile_gold(pathlib.Path(args.workbook), args.domain)
    yaml_text = yaml.safe_dump(gold, sort_keys=False, default_flow_style=False)
    header = ("# Contract schema: GOLD / SEMANTIC. Compiled from "
              "docs/templates/jarvis_gold_model_template.xlsx -- do not hand-edit.\n")
    print(header + yaml_text)

    out_path = SEMANTICS_DIR / f"{args.domain}.gold.yaml"
    if args.write:
        out_path.write_text(header + yaml_text)
        print(f"\nWROTE {out_path}")


if __name__ == "__main__":
    main()
