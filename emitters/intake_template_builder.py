"""Builds docs/templates/jarvis_intake_template.xlsx -- one unified client-intake workbook
replacing the three separate ones (jarvis_data_catalogue_template.xlsx,
jarvis_silver_model_template.xlsx, jarvis_gold_model_template.xlsx). Ported from the
"Data Platform Factory" reference kit (D:/Projects/Data Platform - Jarvis/Design Docs),
adapted to Jarvis's paths and its bronze/silver/gold engine.

Every sheet maps 1:1 to a section of contracts/schema/intake_spec.schema.json. Yellow cells
are client input; row 3 in every table sheet is a worked example (kept as insurance, matching
Jarvis's existing domain, rather than the reference kit's asset-management example) that a
client overwrites for their own domain.

Usage: python -m emitters.intake_template_builder
"""
from __future__ import annotations

import pathlib

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "docs" / "templates" / "jarvis_intake_template.xlsx"

FONT = "Arial"
HEADER_FILL = PatternFill("solid", start_color="1F3864")
INPUT_FILL = PatternFill("solid", start_color="FFF2CC")
EXAMPLE_FONT = Font(name=FONT, size=10, color="0000FF")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

LISTS = {
    "platform": ["databricks", "duckdb"],  # Jarvis only executes these two today, even though
                                            # the canonical spec/schema models more (kept broad
                                            # for forward-compat -- see contracts/schema)
    "env": ["dev", "sit", "uat", "prod"],
    "yesno": ["Y", "N"],
    "file_format": ["csv", "txt", "json", "xml", "parquet", "excel", "avro", "api_json", "table"],
    "load_type": ["full", "incremental", "cdc", "snapshot"],
    "frequency": ["intraday", "daily", "weekly", "monthly", "adhoc"],
    "data_type": ["string", "integer", "bigint", "decimal", "double", "boolean", "date", "timestamp", "binary", "json"],
    "dq_level": ["file", "attribute", "business"],
    "layer": ["landing", "bronze", "silver", "gold"],
    "dimension": ["completeness", "validity", "uniqueness", "timeliness", "consistency", "accuracy", "conformity"],
    "dq_check": ["file_name_regex", "file_extension", "file_not_empty", "row_count_min", "sla_arrival",
                 "header_matches", "not_null", "unique", "regex", "in_list", "range", "is_date",
                 "referential", "sql_expression", "natural_language"],
    "severity": ["error", "warning"],
    "dq_action": ["reject_file", "quarantine_row", "flag_row", "notify_only"],
    "entity_type": ["dimension", "fact", "bridge", "reference"],
    "scd_type": ["0", "1", "2"],
    "materialization": ["table", "view", "incremental"],
    "br_check": ["variance_vs_history", "dimension_mapping_inconsistency", "calculated_column_accuracy",
                 "trend_anomaly", "new_or_missing_dimension", "reconciliation", "custom_sql"],
    "channel": ["email", "teams", "slack", "webhook"],
}

# sheet -> (columns, list-validations {col_name: list_key}, example rows, notes {col: comment})
# Worked example kept as a single source (parties) from the live insurance domain, cut down to
# a minimal illustrative shape -- unlike the reference kit's two-source (Aladdin+BNP) example,
# since Jarvis's silver_transform.py does not yet support conforming multiple bronze sources
# into one silver entity via per-column expressions (see intake_compiler.py's docstring).
TABLES = {
    "02_Data_Systems": (
        ["data_system_code", "name", "system_type", "country", "description", "is_active"],
        {"is_active": "yesno"},
        [["policy_admin", "Policy Admin System", "Core", "SG", "System of record for policies", "Y"]],
        {"data_system_code": "lower_snake_case, unique"},
    ),
    "03_Data_Sources": (
        ["data_source_id", "data_source_code", "data_system_code", "entity_name", "entity_type", "category",
         "file_name_pattern", "file_name_regex", "file_format", "extension", "delimiter", "text_qualifier",
         "has_header", "header_row_number", "row_tag", "sheet_name", "effective_date_in_filename",
         "effective_date_format", "load_type", "frequency", "sla_cutoff_time", "is_mandatory",
         "watermark_column", "bronze_table", "owner", "description", "is_active"],
        {"data_system_code": None, "file_format": "file_format", "has_header": "yesno",
         "effective_date_in_filename": "yesno", "load_type": "load_type", "frequency": "frequency",
         "is_mandatory": "yesno", "is_active": "yesno"},
        [[1, "parties", "policy_admin", "Party", "Core", "Master data", "parties_*.csv",
          r"^parties_.*\.csv$", "csv", "csv", ",", "", "Y", 0, "", "", "N", "",
          "full", "daily", "06:00", "Y", "", "parties", "data-eng@example.com",
          "Insurance party (person/org) master", "Y"]],
        {"data_source_id": "Integer, unique. Stamped on every downstream row for lineage.",
         "sla_cutoff_time": "HH:MM, 24h",
         "file_name_regex": "Used by the file_name_regex DQ check"},
    ),
    "04_Attributes": (
        ["data_source_code", "ordinal", "name", "data_type", "precision", "scale", "nullable",
         "is_business_key", "pii", "format", "description"],
        {"data_type": "data_type", "nullable": "yesno", "is_business_key": "yesno", "pii": "yesno"},
        [["parties", 1, "party_id", "string", "", "", "N", "Y", "Y", "", "Natural key"],
         ["parties", 2, "display_name", "string", "", "", "N", "N", "Y", "", "Full name"],
         ["parties", 3, "email", "string", "", "", "Y", "N", "Y", "", "Contact email"]],
        {},
    ),
    "05_DQ_Rules": (
        ["rule_id", "rule_name", "level", "layer", "dimension", "target", "column", "check", "params",
         "severity", "action", "description", "owner", "is_active"],
        {"level": "dq_level", "layer": "layer", "dimension": "dimension", "check": "dq_check",
         "severity": "severity", "action": "dq_action", "is_active": "yesno"},
        [["DQ001", "Parties row count", "file", "landing", "completeness", "parties", "",
          "row_count_min", "min_rows=100", "error", "reject_file", "", "ops", "Y"],
         ["DQ002", "party_id not null", "attribute", "bronze", "completeness", "parties", "party_id",
          "not_null", "", "error", "quarantine_row", "", "ops", "Y"],
         ["DQ003", "party_id unique", "attribute", "bronze", "uniqueness", "parties", "party_id",
          "unique", "", "error", "quarantine_row", "", "ops", "Y"]],
        {"params": "key=value; key=value (e.g. min=0; max=100 or values=A,B,C)",
         "description": "For check=natural_language, write the rule in plain English. An agent drafts the check; a human approves it at the Freeze gate."},
    ),
    "06_Silver_Entities": (
        ["name", "entity_type", "scd_type", "business_key", "merge_hash_columns", "surrogate_key", "description"],
        {"entity_type": "entity_type", "scd_type": "scd_type"},
        [["dim_party", "dimension", "1", "party_id", "", "party_sk", "Party master (SCD1)"]],
        {"business_key": "Comma-separated column list",
         "merge_hash_columns": "SCD2 only: columns whose change creates a new version"},
    ),
    "07_Silver_Columns": (
        ["entity", "name", "data_type", "precision", "scale", "nullable", "references", "description"],
        {"data_type": "data_type", "nullable": "yesno"},
        [["dim_party", "party_id", "string", "", "", "N", "", "Natural key"],
         ["dim_party", "display_name", "string", "", "", "N", "", "Full name"],
         ["dim_party", "email", "string", "", "", "Y", "", "Contact email"]],
        {},
    ),
    "08_Silver_Mappings": (
        ["target_entity", "target_column", "source", "expression", "filter", "rule_description"],
        {},
        [["dim_party", "party_id", "parties", "party_id", "", ""],
         ["dim_party", "display_name", "parties", "display_name", "", ""],
         ["dim_party", "email", "parties", "email", "", ""]],
        {"expression": "SQL expression over BRONZE column names. Jarvis's silver engine today "
                        "only supports ONE source per entity with a pass-through (identity) "
                        "expression per column -- see intake_compiler.py's known-gaps note for "
                        "what a non-identity expression or a second source per entity means."},
    ),
    "09_Gold_Datasets": (
        ["name", "business_area", "grain", "materialization", "sql", "refresh", "owner", "description"],
        {"materialization": "materialization"},
        [["mart_party_count_by_type", "Ops", "classification", "table",
          "SELECT classification, COUNT(*) AS party_count FROM silver.dim_party GROUP BY classification",
          "daily 06:30", "data-eng@example.com", "Party counts by classification"]],
        {"sql": "SELECT referencing silver.<entity>. Written once in duckdb dialect; transpiled per platform."},
    ),
    "10_Gold_Metrics": (
        ["name", "dataset", "expression", "dimensions", "time_dimension", "owner", "description"],
        {},
        [["party_count", "mart_party_count_by_type", "SUM(party_count)", "classification",
          "", "data-eng", "Total parties"]],
        {},
    ),
    "11_Business_Checks": (
        ["rule_id", "check_type", "dataset", "column", "params", "severity", "owner", "description"],
        {"check_type": "br_check", "severity": "severity"},
        [["BR001", "new_or_missing_dimension", "mart_party_count_by_type", "classification",
          "reference=silver.dim_party", "warning", "data.steward",
          "Every classification in gold should exist in the current party dimension"]],
        {"params": "key=value; key=value"},
    ),
    "12_Notifications": (
        ["channel", "recipients", "on", "scope"],
        {"channel": "channel"},
        [["slack", "#all-jarvis-ops-testing", "failure, sla_breach, dq_error", "*"]],
        {"on": "Comma list: failure, sla_breach, dq_error, dq_warning, success"},
    ),
}

PROJECT_FIELDS = [
    ("spec_version", "0.1.0", "Bumped at every Freeze gate"),
    ("project.code", "your_project_code", "lower_snake_case"),
    ("project.name", "Your Project Name", ""),
    ("project.client", "default", "Which client/tenant this run is for -- see contracts/clients/"),
    ("project.domain", "example_domain", "REQUIRED: change this before approving -- 'insurance' is "
     "already a live domain in this repo, and approving with this left unchanged would overwrite "
     "its real contracts with this sheet's toy example. e.g. asset_management, retail, insurance_v2"),
    ("project.owner", "data-eng@example.com", ""),
    ("environment.platform", "duckdb", "duckdb | databricks (the two Jarvis actually runs)"),
    ("environment.environment_name", "dev", "dev | sit | uat | prod"),
    ("environment.canonical_sql_dialect", "duckdb", "Dialect used in mapping and gold SQL -- matches emitters/sql_dialect.py"),
    ("environment.catalog", "local", "'local' for duckdb (informational only); 'jarvis' for databricks"),
    ("environment.schemas.landing", "landing", "Informational only -- Jarvis derives real schema names from domain+layer, see resolve_schema()"),
    ("environment.schemas.bronze", "bronze", ""),
    ("environment.schemas.silver", "silver", ""),
    ("environment.schemas.gold", "gold", ""),
    ("environment.schemas.utility", "control", ""),
    ("environment.landing_path", "harness/landing", ""),
    ("environment.auth_profile", "DEFAULT", "Name of a .env profile ONLY. Never paste tokens here."),
    ("environment.compute", "serverless", ""),
    ("environment.orchestrator", "none", "platform_native | dagster | airflow | none"),
    ("governance.catalog_tool", "none", "openmetadata | datahub | unity_catalog | purview | collibra | none"),
]


def style_header(ws, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=2, column=c)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF", size=10)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.border = BORDER


def build(path: pathlib.Path) -> None:
    wb = Workbook()
    readme = wb.active
    readme.title = "README"
    lines = [
        ("Jarvis Intake Workbook", True),
        ("", False),
        ("How to use", True),
        ("1. Fill the yellow cells on each numbered sheet. Blue text in row 3+ is a worked example (insurance); overwrite or delete it for your own domain.", False),
        ("2. Dropdowns enforce allowed values. Y/N fields accept Y or N only.", False),
        ("3. Never enter passwords, tokens or keys. environment.auth_profile is the NAME of a .env profile, never a token.", False),
        ("4. Upload this file in the Jarvis Control Room, or run: python -m emitters.intake_compiler <this file> --write", False),
        ("5. The compiler validates against contracts/schema/intake_spec.schema.json and reports every error with sheet/row before anything is written.", False),
        ("", False),
        ("Sheet map (sheet -> what it becomes in Jarvis)", True),
        ("01_Project -> contracts/sources/<domain>/*.source.yaml's domain/client + project metadata", False),
        ("02_Data_Systems, 03_Data_Sources, 04_Attributes -> one contracts/sources/<domain>/<source>.source.yaml per row", False),
        ("05_DQ_Rules -> file_checks + quality_rules on each source contract", False),
        ("06/07/08 Silver -> contracts/models/<domain>.model.yaml (dimensions/facts)", False),
        ("09/10/11 Gold -> contracts/semantics/<domain>.gold.yaml (marts/metrics/business_rules)", False),
        ("12_Notifications -> which Slack channel gets [domain]-tagged alerts", False),
        ("", False),
        ("Legend", True),
        ("Yellow fill = client input cell", False),
        ("Blue text = example value (replace)", False),
        ("Dark blue header = field name used in the spec (do not rename)", False),
        ("Cell comments on headers explain formats", False),
    ]
    for i, (t, bold) in enumerate(lines, start=1):
        c = readme.cell(row=i, column=1, value=t)
        c.font = Font(name=FONT, bold=bold, size=14 if i == 1 else 10)
    readme.column_dimensions["A"].width = 120

    lists_ws = wb.create_sheet("Lists")
    list_ranges = {}
    for col_idx, (key, values) in enumerate(LISTS.items(), start=1):
        lists_ws.cell(row=1, column=col_idx, value=key).font = Font(name=FONT, bold=True)
        for r, v in enumerate(values, start=2):
            lists_ws.cell(row=r, column=col_idx, value=v).font = Font(name=FONT)
        letter = lists_ws.cell(row=1, column=col_idx).column_letter
        list_ranges[key] = f"Lists!${letter}$2:${letter}${len(values) + 1}"
    lists_ws.sheet_state = "hidden"

    pj = wb.create_sheet("01_Project", 1)
    pj["A1"] = "Project, environment and governance settings"
    pj["A1"].font = Font(name=FONT, bold=True, size=12)
    for i, h in enumerate(["key", "value", "guidance"], start=1):
        pj.cell(row=2, column=i, value=h)
    style_header(pj, 3)
    for r, (k, v, g) in enumerate(PROJECT_FIELDS, start=3):
        pj.cell(row=r, column=1, value=k).font = Font(name=FONT, size=10)
        vc = pj.cell(row=r, column=2, value=v)
        vc.font = EXAMPLE_FONT
        vc.fill = INPUT_FILL
        pj.cell(row=r, column=3, value=g).font = Font(name=FONT, size=9, italic=True)
        for c in range(1, 4):
            pj.cell(row=r, column=c).border = BORDER
    for key, lk in [("environment.platform", "platform"), ("environment.environment_name", "env")]:
        row = [i for i, f in enumerate(PROJECT_FIELDS, start=3) if f[0] == key][0]
        dv = DataValidation(type="list", formula1=list_ranges[lk], allow_blank=False)
        pj.add_data_validation(dv)
        dv.add(f"B{row}")
    pj.column_dimensions["A"].width = 36
    pj.column_dimensions["B"].width = 38
    pj.column_dimensions["C"].width = 60
    pj.freeze_panes = "A3"

    for pos, (name, (cols, validations, examples, notes)) in enumerate(TABLES.items(), start=2):
        ws = wb.create_sheet(name, pos)
        ws["A1"] = name.split("_", 1)[1].replace("_", " ")
        ws["A1"].font = Font(name=FONT, bold=True, size=12)
        for i, col in enumerate(cols, start=1):
            cell = ws.cell(row=2, column=i, value=col)
            if col in notes:
                cell.comment = Comment(notes[col], "jarvis")
            ws.column_dimensions[cell.column_letter].width = max(14, min(48, len(col) + 6))
        style_header(ws, len(cols))
        for r, row in enumerate(examples, start=3):
            for c, val in enumerate(row, start=1):
                cell = ws.cell(row=r, column=c, value=val)
                cell.font = EXAMPLE_FONT
                cell.border = BORDER
                cell.alignment = Alignment(vertical="top", wrap_text=cols[c - 1] in ("sql", "expression", "description", "rule_description"))
        last_input_row = 500
        for c in range(1, len(cols) + 1):
            for r in range(3 + len(examples), 3 + len(examples) + 20):
                ws.cell(row=r, column=c).fill = INPUT_FILL
                ws.cell(row=r, column=c).border = BORDER
        for col, lk in validations.items():
            if not lk:
                continue
            letter = ws.cell(row=2, column=cols.index(col) + 1).column_letter
            dv = DataValidation(type="list", formula1=list_ranges[lk], allow_blank=True,
                                showErrorMessage=True, errorTitle="Invalid value",
                                error=f"Choose a value from the list for {col}")
            ws.add_data_validation(dv)
            dv.add(f"{letter}3:{letter}{last_input_row}")
        if "sql" in cols:
            ws.column_dimensions[ws.cell(row=2, column=cols.index("sql") + 1).column_letter].width = 80
        ws.freeze_panes = "B3"

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


if __name__ == "__main__":
    build(OUT_PATH)
    print(f"wrote {OUT_PATH}")
