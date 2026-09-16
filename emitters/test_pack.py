"""Step 04 per-layer test generation (3A bronze / 3B silver / 3C gold) -- Agent 5's (Test
Manager) real capability, replacing what gather_validation_pack (G3's prep tool) used to lean
on: a single pytest suite hard-coded to the insurance domain (tests/test_pipeline.py's own
DOMAIN constant), run regardless of which domain was actually being validated. Confirmed by
reading it, not assumed -- a real bug: validating asset_management at G3 was checking whether
INSURANCE's regression suite passed, which says nothing about asset_management.

Every test case here is DERIVED from the domain's own contracts -- quality_rules, business_
rules, foreign_keys -- never a hardcoded assumption about what a domain "should" contain.

Why this doesn't just read control.dq_results: that table carries no entity/layer/source
column (confirmed by reading its DDL in emitters/control_plane.py), so a historical row can't
be reliably attributed to "silver, dim_policy" versus any other entity that happens to share a
column name. Rather than force an attribution the data model doesn't support, this module
RE-RUNS the exact evaluation functions bronze/silver/gold already use --
silver_transform.eval_entity_quality_rules (schema-agnostic despite the parameter name: it
takes any schema string, so it evaluates bronze's quality_rules identically to silver's) and
gold_transform.eval_business_checks -- against ONE fresh run_id generated here, then reads back
exactly what THAT run_id wrote. No mining ambiguous history; no duplicated rule logic.

The orphan-FK check (3B) is genuinely new: every fact contract already declares foreign_keys
(dimension + join column) and a conformance.reject_orphan_fks flag, but grep across
emitters/silver_transform.py and emitters/gold_transform.py confirms neither ever enforces it.
This closes that gap using exactly what the contract already declares -- never guessing at a
relationship the contract doesn't state.
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys
import uuid
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters.control_plane import ensure_control_schema  # noqa: E402
from emitters.gold_transform import eval_business_checks  # noqa: E402
from emitters.silver_transform import eval_entity_quality_rules  # noqa: E402
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402


def _load_platform(target: str) -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


def _load_model(domain: str) -> dict[str, Any] | None:
    path = REPO_ROOT / "contracts" / "models" / f"{domain}.model.yaml"
    return yaml.safe_load(path.read_text()) if path.exists() else None


def _load_gold_contract(domain: str) -> dict[str, Any] | None:
    path = REPO_ROOT / "contracts" / "semantics" / f"{domain}.gold.yaml"
    return yaml.safe_load(path.read_text()) if path.exists() else None


def _load_sources(domain: str) -> list[dict[str, Any]]:
    src_dir = REPO_ROOT / "contracts" / "sources" / domain
    if not src_dir.exists():
        return []
    return [yaml.safe_load(p.read_text()) for p in sorted(src_dir.glob("*.source.yaml"))]


def _table_exists(con, schema: str, table: str) -> bool:
    row = con.execute(
        "select count(*) from information_schema.tables where table_schema = ? and table_name = ?",
        [schema, table],
    ).fetchone()
    return bool(row and row[0])


def _row_count(con, schema: str, table: str) -> int:
    return con.execute(f"select count(*) from {schema}.{table}").fetchone()[0]


def _new_dq_rows(con, control: str, test_run_id: str, after_id: int) -> tuple[list[dict[str, Any]], int]:
    """Rows this test-pack run has written since the last checkpoint -- result_id is
    monotonically increasing within one sequential run (no concurrency inside this function),
    so tracking a cursor here gives each eval_* call's rows their correct, known layer instead
    of re-deriving it after the fact from data the table doesn't carry."""
    rows = con.execute(
        f"select result_id, rule_type, columns, severity, passed, failed_row_count "
        f"from {control}.dq_results where run_id = ? and result_id > ? order by result_id",
        [test_run_id, after_id],
    ).fetchall()
    parsed = [{"rule": r[1], "columns": r[2], "severity": r[3], "passed": bool(r[4]),
               "failed_rows": r[5]} for r in rows]
    new_after = rows[-1][0] if rows else after_id
    return parsed, new_after


def _case(case_id: str, layer: str, description: str, status: str, detail: str) -> dict[str, Any]:
    return {"id": case_id, "layer": layer, "description": description, "status": status, "detail": detail}


def _check_orphan_fks(con, silver: str, fact_name: str, foreign_keys: list[dict],
                       entities_by_name: dict[str, dict]) -> list[dict[str, Any]]:
    """One case per declared foreign key -- every row in the fact whose join column is non-null
    must resolve to at least one row in the referenced dimension. Uses the dimension's OWN
    declared business_key column, not the fact's `on` column name, in case a project ever names
    them differently -- read from the contract, never assumed to match by convention."""
    cases = []
    for fk in foreign_keys:
        dim_name, on_col = fk.get("dimension"), fk.get("on")
        dim = entities_by_name.get(dim_name)
        if dim is None:
            cases.append(_case(f"fk:{fact_name}:{dim_name}", "silver",
                               f"{fact_name}.{on_col} -> {dim_name}", "fail",
                               f"declared FK target {dim_name!r} is not a known dimension in this model"))
            continue
        dim_key = (dim.get("business_key") or [on_col])[0]
        orphans = con.execute(
            f'select count(*) from {silver}.{fact_name} f where f."{on_col}" is not null '
            f'and not exists (select 1 from {silver}.{dim_name} d where d."{dim_key}" = f."{on_col}")'
        ).fetchone()[0]
        cases.append(_case(
            f"fk:{fact_name}:{dim_name}", "silver", f"{fact_name}.{on_col} -> {dim_name}.{dim_key}",
            "pass" if orphans == 0 else "fail",
            f"0 orphan rows" if orphans == 0 else f"{orphans} row(s) in {fact_name} reference a "
                                                  f"{on_col} value not present in {dim_name}",
        ))
    return cases


def generate_test_pack(domain: str, target: str) -> dict[str, Any]:
    """The real per-layer test pack for THIS domain. Every case traces to something the
    domain's own contracts declared -- a quality_rules entry, a foreign_keys relationship, a
    business_rules check -- never an assumption about what data should look like."""
    platform = _load_platform(target)
    control = resolve_schema(platform, domain, "control")
    bronze = resolve_schema(platform, domain, "bronze")
    silver = resolve_schema(platform, domain, "silver")
    gold = resolve_schema(platform, domain, "gold")
    con = sql_connect(target, platform)
    ensure_control_schema(con, control)
    test_run_id = f"testpack-{uuid.uuid4().hex[:12]}"
    client = "default"

    try:
        cases: list[dict[str, Any]] = []
        cursor = 0  # dq_results.result_id checkpoint -- see _new_dq_rows

        def _drain_dq(layer: str, entity: str) -> None:
            nonlocal cursor
            new_rows, cursor = _new_dq_rows(con, control, test_run_id, cursor)
            for d in new_rows:
                cases.append(_case(
                    f"dq:{entity}:{d['rule']}:{d['columns']}", layer,
                    f"{entity}: {d['rule']}({d['columns']})", "pass" if d["passed"] else "fail",
                    "0 failing rows" if d["passed"] else f"{d['failed_rows']} failing row(s)",
                ))

        # ---- 3A bronze: row landed + re-run each source's own quality_rules ----
        for src in _load_sources(domain):
            source_id = src["source_id"]
            client = src.get("client", client)
            if not _table_exists(con, bronze, source_id):
                cases.append(_case(f"bronze:{source_id}:exists", "bronze",
                                   f"{source_id} landed in bronze", "fail", "table does not exist"))
                continue
            rows = _row_count(con, bronze, source_id)
            cases.append(_case(f"bronze:{source_id}:rows", "bronze", f"{source_id} has landed rows",
                               "pass" if rows > 0 else "fail", f"{rows} row(s)"))
            # `unique` is deliberately excluded here: bronze_loader.py's own _eval_quality_rules
            # runs it against a per-BATCH staging table (`{bronze}._staging_{source_id}`), not
            # the accumulated bronze table -- confirmed by reading that function, not assumed.
            # Bronze is append-only across multiple loads by design (see docs/agentic-sdlc.md
            # / the P1 evidence doc), so the same natural key legitimately repeats across days.
            # Re-running `unique` against the FULL table here would report that expected,
            # by-design repetition as a failure -- found exactly that happening during testing,
            # not reasoned about in advance, and fixed before this shipped. not_null/
            # accepted_values/range/regex stay in: those are per-VALUE checks, unaffected by
            # how many days of history have accumulated.
            rules = [r for r in (src.get("quality_rules") or []) if r.get("rule") != "unique"]
            if rules:
                eval_entity_quality_rules(con, control, bronze, source_id, rules, test_run_id, domain, client)
                _drain_dq("bronze", source_id)

        # ---- 3B silver: row count + declared quality_rules + real orphan-FK check ----
        model = _load_model(domain)
        entities_by_name: dict[str, dict] = {}
        if model:
            entities_by_name = {d["name"]: d for d in model.get("dimensions", [])}
            entities_by_name.update({f["name"]: f for f in model.get("facts", [])})

            for dim in model.get("dimensions", []):
                name = dim["name"]
                if not _table_exists(con, silver, name):
                    cases.append(_case(f"silver:{name}:exists", "silver", f"{name} built in silver",
                                       "fail", "table does not exist"))
                    continue
                rows = _row_count(con, silver, name)
                cases.append(_case(f"silver:{name}:rows", "silver", f"{name} has conformed rows",
                                   "pass" if rows > 0 else "fail", f"{rows} row(s)"))
                rules = dim.get("quality_rules") or []
                if rules:
                    eval_entity_quality_rules(con, control, silver, name, rules, test_run_id, domain, client)
                    _drain_dq("silver", name)

            for fact in model.get("facts", []):
                name = fact["name"]
                if not _table_exists(con, silver, name):
                    cases.append(_case(f"silver:{name}:exists", "silver", f"{name} built in silver",
                                       "fail", "table does not exist"))
                    continue
                rows = _row_count(con, silver, name)
                cases.append(_case(f"silver:{name}:rows", "silver", f"{name} has conformed rows",
                                   "pass" if rows > 0 else "fail", f"{rows} row(s)"))
                rules = fact.get("quality_rules") or []
                if rules:
                    eval_entity_quality_rules(con, control, silver, name, rules, test_run_id, domain, client)
                    _drain_dq("silver", name)
                fks = fact.get("foreign_keys") or []
                if fks:
                    cases += _check_orphan_fks(con, silver, name, fks, entities_by_name)

        # ---- 3C gold: mart row count + declared business_rules ----
        gold_contract = _load_gold_contract(domain)
        if gold_contract:
            business_rules = gold_contract.get("business_rules", [])
            for mart in gold_contract.get("marts", []):
                name = mart["name"]
                if not _table_exists(con, gold, name):
                    cases.append(_case(f"gold:{name}:exists", "gold", f"{name} built in gold",
                                       "fail", "table does not exist"))
                    continue
                rows = _row_count(con, gold, name)
                cases.append(_case(f"gold:{name}:rows", "gold", f"{name} has rows",
                                   "pass" if rows > 0 else "fail", f"{rows} row(s)"))
                if business_rules:
                    eval_business_checks(con, control, gold, silver, name, business_rules,
                                         test_run_id, domain, client)
                    _drain_dq("gold", name)

        by_layer: dict[str, list[dict]] = {"bronze": [], "silver": [], "gold": []}
        for c in cases:
            by_layer.setdefault(c["layer"], []).append(c)

        summary = {
            layer: {"total": len(items), "passed": sum(1 for i in items if i["status"] == "pass"),
                    "failed": sum(1 for i in items if i["status"] == "fail")}
            for layer, items in by_layer.items()
        }
        total_failed = sum(s["failed"] for s in summary.values())

        return {
            "domain": domain, "target": target, "test_run_id": test_run_id,
            "by_layer": by_layer, "summary": summary, "total_failed": total_failed,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
    finally:
        con.close()
