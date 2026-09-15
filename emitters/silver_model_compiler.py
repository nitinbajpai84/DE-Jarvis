"""Compiles docs/templates/jarvis_silver_model_template.xlsx into
contracts/models/<domain>.model.yaml -- the bronze-to-silver transformation rules (SCD type,
tracked attributes, fact grain/measures/foreign keys), same shape as the hand-authored
contracts/models/sales.model.yaml. Same discipline as emitters/catalogue_compiler.py: only
emits what's in the workbook, stops on a broken reference rather than guessing.
"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

import yaml
from openpyxl import load_workbook

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "contracts" / "models"


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


def compile_model(xlsx_path: pathlib.Path) -> dict[str, Any]:
    wb = load_workbook(xlsx_path, data_only=True)
    std_rows = _sheet_rows(wb["Standardisation"])
    if len(std_rows) != 1:
        raise ValueError(f"Standardisation tab must have exactly one row, found {len(std_rows)}")
    std = std_rows[0]
    dims = _sheet_rows(wb["Dimensions"])
    facts = _sheet_rows(wb["Facts"])

    dim_names = {d["name"] for d in dims}
    errors = []
    for f in facts:
        for fk in json.loads(f["foreign_keys_json"] or "[]"):
            if fk["dimension"] not in dim_names:
                errors.append(f"fact {f['name']!r} references unknown dimension {fk['dimension']!r}")
    if errors:
        raise ValueError("Silver model has broken references:\n  " + "\n  ".join(errors))

    model: dict[str, Any] = {
        "$schema": "jarvis/model/v1",
        "domain": std["domain"],
        "grain_statement": std["grain_statement"],
        "standardisation": {
            "date_format": std["date_format"],
            "timezone": std["timezone"],
            "decimal_precision": [int(x) for x in str(std["decimal_precision"]).split(",")],
            "string_case": std["string_case"],
            "null_tokens": _split_list(std["null_tokens"]),
        },
        "dimensions": [],
        "facts": [],
    }

    for d in dims:
        entry: dict[str, Any] = {
            "name": d["name"], "type": d["scd_type"],
            "business_key": _split_list(d["business_key"]),
            "surrogate_key": d["surrogate_key"],
        }
        if d["scd_type"] == "scd2":
            entry["effective_from"] = "row_start_date"
            entry["effective_to"] = "row_end_date"
            entry["current_flag"] = "row_is_current"
        if _split_list(d.get("tracked_attributes")):
            entry["tracked_attributes"] = _split_list(d["tracked_attributes"])
        entry["source"] = [d["source_bronze_table"]]
        model["dimensions"].append(entry)

    for f in facts:
        entry = {
            "name": f["name"], "grain": _split_list(f["grain"]), "type": "transaction",
            "source": [f["source_bronze_table"]],
            "foreign_keys": json.loads(f["foreign_keys_json"] or "[]"),
            "measures": json.loads(f["measures_json"] or "[]"),
        }
        model["facts"].append(entry)

    model["conformance"] = {
        "shared_dimensions": [d["name"] for d in model["dimensions"]],
        "reject_orphan_fks": False,
    }
    return model


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", default=str(REPO_ROOT / "docs" / "templates" / "jarvis_silver_model_template.xlsx"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    model = compile_model(pathlib.Path(args.workbook))
    yaml_text = yaml.safe_dump(model, sort_keys=False, default_flow_style=False)
    header = (f"# Contract schema: MODEL (silver). Compiled from "
              f"docs/templates/jarvis_silver_model_template.xlsx -- do not hand-edit, "
              f"re-run the compiler instead.\n")
    print(header + yaml_text)

    out_path = MODELS_DIR / f"{model['domain']}.model.yaml"
    if args.write:
        out_path.write_text(header + yaml_text)
        print(f"\nWROTE {out_path}")


if __name__ == "__main__":
    main()
