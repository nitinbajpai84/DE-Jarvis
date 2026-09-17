"""Stage 01 estate scan: what is actually in the customer's data systems, found by looking
rather than by reading a catalogue nobody has maintained.

Tiered on purpose. You cannot send an LLM at 40,000 tables -- at roughly 2,000 tokens of names,
types and samples per table that is 80M tokens per sweep, before anyone has learned anything.
And you don't need to: most of what a human wants to know is mechanical.

  Tier 1  inventory sweep     every schema/table/column, from information_schema   exhaustive
  Tier 2  statistical profile null rate, distinctness, candidate keys, sensitivity  shortlist
  Tier 3  relationship graph  value overlap between same-named columns              shortlist
  Tier 4  semantic enrichment what this table appears to BE, and its concerns       shortlist

Tiers 1-3 are reproducible: run them twice against an unchanged estate and the output is
identical, which is what makes a scan safe to re-run nightly and safe to diff. Tier 4 is the
only place a model's judgement enters, and it enters as a suggestion attached to evidence --
never as a fact written into a contract.

Scope and tenancy: a scan for `domain` covers that domain's own schemas plus any schema that
belongs to no known domain at all (genuinely unclassified estate -- this deployment has legacy
unprefixed bronze/silver/gold schemas that predate the multi-domain migration, and finding them
is exactly the point). The platform's own `control` schema is never scanned; it is bookkeeping,
not the customer's data.

Honest limit on Tier 1: row counts here come from `count(*)` per table, which is fine at this
deployment's scale (tens of tables) and is NOT what you would do against a 40,000-table estate
-- there you would read the catalogue's own maintained statistics (DuckDB's duckdb_tables()
estimated_size, Databricks' DESCRIBE DETAIL / system tables). The structure sweep above it is
already catalogue-only and does scale; only the counts would move.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
import sys
import uuid
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters.control_plane import ensure_control_schema  # noqa: E402
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402

_DDL = [
    """create table if not exists {c}.estate_scan (
        scan_id varchar primary key, domain varchar, target varchar, status varchar,
        tier_reached integer, schemas_scanned integer, tables_scanned integer,
        started_at timestamp, ended_at timestamp, note varchar)""",
    """create table if not exists {c}.estate_table (
        scan_id varchar, schema_name varchar, table_name varchar, column_count integer,
        row_count bigint, shortlisted boolean, shortlist_reason varchar, classification varchar,
        business_meaning varchar, entity_type varchar, concerns varchar,
        mirrors varchar, mirror_state varchar, mirror_diff_rows bigint)""",
    """create table if not exists {c}.estate_column (
        scan_id varchar, schema_name varchar, table_name varchar, column_name varchar,
        data_type varchar, ordinal integer, null_pct double, distinct_count bigint,
        is_candidate_key boolean, sensitivity varchar, profiled boolean)""",
    """create table if not exists {c}.estate_relationship (
        scan_id varchar, from_schema varchar, from_table varchar, from_column varchar,
        to_schema varchar, to_table varchar, to_column varchar,
        overlap_pct double, confidence varchar)""",
]

# Column-name patterns that indicate personal or financial data. Name-based is the cheap,
# reliable first pass and it is reported as a *candidate* classification -- a column called
# "account_name" may hold a product name, so nothing here is treated as proof.
_SENSITIVITY = [
    ("national_id", r"(nric|ssn|national_id|passport|aadhaar|tax_id)"),
    ("payment", r"(card_number|cardno|iban|account_number|acct_no|sort_code|cvv)"),
    ("contact", r"(email|e_mail|phone|mobile|telephone|fax)"),
    ("name", r"(first_name|last_name|full_name|surname|given_name|display_name|maiden)"),
    ("dob", r"(date_of_birth|dob|birth_date|birthdate)"),
    ("location", r"(postal_code|postcode|zip|address_line|line1|line2|latitude|longitude)"),
    ("financial", r"(salary|income|net_worth|credit_score)"),
    ("health", r"(diagnosis|icd_?10|medical|treatment)"),
]

_LINEAGE_COLS = {"data_catalogue_id", "source_record_id", "_run_id", "_source_file",
                 "_ingested_at", "_record_hash"}


def _load_platform(target: str) -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


def _utcnow() -> _dt.datetime:
    """Naive UTC, stored as such on both engines. Passing an aware datetime was not neutral:
    DuckDB converted it to the machine's local time before dropping the zone (SGT on a dev
    laptop), Databricks kept UTC, and both read back zone-less -- so the Control Room, parsing
    them as browser-local time, showed a 20-minute-old Databricks scan as "8h ago"."""
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _iso_utc(value: Any) -> Any:
    return value.isoformat() + "Z" if isinstance(value, _dt.datetime) else value


_SCHEMA_READY: set[tuple[str, str]] = set()


def _con(target: str, domain: str):
    """Schema setup runs once per process per (target, control), not on every connection. A scan
    runs for a minute or more in the background while the Control Room polls for its status, and
    each poll opening a connection used to re-issue CREATE TABLE IF NOT EXISTS -- concurrent DDL
    on DuckDB, which is exactly the write-write conflict this codebase has already been bitten by
    (see ensure_control_schema's own retry)."""
    platform = _load_platform(target)
    control = resolve_schema(platform, domain, "control")
    con = sql_connect(target, platform)
    if (target, control) not in _SCHEMA_READY:
        ensure_control_schema(con, control)
        for stmt in _DDL:
            con.execute(stmt.format(c=control))
        _SCHEMA_READY.add((target, control))
    return con, control


def _known_domains() -> list[str]:
    d = REPO_ROOT / "contracts" / "sources"
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []


def _in_scope(schema: str, domain: str, control_schema: str, known: list[str]) -> tuple[bool, str]:
    """This domain's own schemas, plus anything belonging to no domain at all. The control
    schema is the platform's bookkeeping and is never part of a customer's estate."""
    s = schema.lower()
    if s == control_schema.lower() or s.startswith("information_schema") or s in ("pg_catalog", "main", "system"):
        return False, ""
    if s.startswith(f"{domain.lower()}_") or s == domain.lower():
        return True, domain
    for other in known:
        if s.startswith(f"{other.lower()}_") or s == other.lower():
            return False, other          # belongs to a different tenant -- not ours to look at
    return True, "unclassified"


def _classify_column(name: str) -> str | None:
    n = name.lower()
    for label, pattern in _SENSITIVITY:
        if re.search(pattern, n):
            return label
    return None


# --------------------------------------------------------------------------- tier 1

def _tier1_inventory(con, control: str, scan_id: str, domain: str) -> list[dict[str, Any]]:
    """Catalogue-only structure sweep: every schema, table and column in scope. No sampling."""
    known = _known_domains()
    rows = con.execute(
        "select table_schema, table_name, column_name, data_type, ordinal_position "
        "from information_schema.columns order by table_schema, table_name, ordinal_position"
    ).fetchall()

    tables: dict[tuple[str, str], list[tuple]] = {}
    for schema, table, column, dtype, ordinal in rows:
        ok, _origin = _in_scope(schema, domain, control, known)
        if not ok:
            continue
        tables.setdefault((schema, table), []).append((column, dtype, ordinal))

    inventory = []
    for (schema, table), cols in tables.items():
        try:
            n = con.execute(f"select count(*) from {schema}.{table}").fetchone()[0]
        except Exception:  # noqa: BLE001 -- a view or permission-denied object still belongs in the census
            n = None
        _ok, origin = _in_scope(schema, domain, control, known)
        inventory.append({"schema": schema, "table": table, "columns": cols, "row_count": n,
                          "origin": origin, "stats": {}, "shortlist_reason": None,
                          "mirrors": None, "mirror_state": None, "mirror_diff_rows": None,
                          "business_meaning": None, "entity_type": None, "concerns": None})
    return inventory


# --------------------------------------------------------------------------- writes
#
# Every tier computes into the in-memory inventory and the results are written in a handful of
# multi-row INSERTs, never one statement per row. The first version wrote as it went -- an INSERT
# per table, an INSERT per column, an UPDATE per profiled column -- which is invisible on local
# DuckDB and fatal on Databricks, where each statement is a network round trip: a real scan there
# had censused 7 tables after ~20 minutes when it was stopped. SqlConnection.executemany already
# solved exactly this for bronze loading; the scan reuses it. Column order in these rows must
# match the DDL order above, since executemany only takes the bare `insert into t values` shape.

def _table_rows(scan_id: str, inventory: list[dict[str, Any]]) -> list[list]:
    return [[scan_id, i["schema"], i["table"], len(i["columns"]), i["row_count"],
             i["shortlist_reason"] is not None, i["shortlist_reason"], i["origin"],
             i["business_meaning"], i["entity_type"], i["concerns"],
             i["mirrors"], i["mirror_state"], i["mirror_diff_rows"]] for i in inventory]


def _write_census(con, control: str, scan_id: str, inventory: list[dict[str, Any]]) -> None:
    col_rows = []
    for i in inventory:
        for column, dtype, ordinal in i["columns"]:
            s = i["stats"].get(column)
            col_rows.append([scan_id, i["schema"], i["table"], column, str(dtype), int(ordinal),
                             s["null_pct"] if s else None, s["distinct"] if s else None,
                             s["is_key"] if s else None, _classify_column(column), s is not None])
    con.executemany(f"insert into {control}.estate_table values "
                    f"(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _table_rows(scan_id, inventory))
    con.executemany(f"insert into {control}.estate_column values "
                    f"(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", col_rows)


def _rewrite_tables(con, control: str, scan_id: str, inventory: list[dict[str, Any]]) -> None:
    """Enrichment lands after the census is written; two statements replace the scan's table rows
    rather than one UPDATE per enriched table."""
    con.execute(f"delete from {control}.estate_table where scan_id = ?", [scan_id])
    con.executemany(f"insert into {control}.estate_table values "
                    f"(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _table_rows(scan_id, inventory))


# --------------------------------------------------------------------------- shortlist

def _shortlist(inventory: list[dict[str, Any]], domain: str, limit: int) -> list[dict[str, Any]]:
    """Which tables are worth the expensive tiers. Ranked by the intent's own vocabulary first
    (a table whose columns the business actually asked for outranks a big one nobody named),
    then by size. Reproducible: same estate and same intent give the same shortlist."""
    wanted: set[str] = set()
    try:
        from emitters.intent import list_intents
        for it in list_intents(domain):
            for rep in it.get("reports", []) or []:
                for dp in rep.get("required_data_points", []) or []:
                    wanted.add(str(dp).strip().lower())
    except Exception:  # noqa: BLE001 -- no intent captured yet is a normal first-scan state
        pass

    scored = []
    for item in inventory:
        names = {c[0].lower() for c in item["columns"]}
        hits = sorted(wanted & names)
        rows = item["row_count"] or 0
        reason = f"matches intent: {', '.join(hits[:4])}" if hits else ("largest tables" if rows else "structure only")
        scored.append((len(hits), rows, reason, item))

    scored.sort(key=lambda s: (-s[0], -s[1]))
    out = []
    for _hits, _rows, reason, item in scored[:limit]:
        item["shortlist_reason"] = reason      # same dict as the inventory's, so the census row sees it
        out.append(item)
    return out


# --------------------------------------------------------------------------- tier 2

def _tier2_profile(con, shortlist: list[dict[str, Any]]) -> None:
    """One aggregate query per shortlisted table gives null rate and distinctness for every
    column at once -- approx_count_distinct so this stays cheap on a wide table, and it exists
    on both DuckDB and Databricks so there is no dialect branch."""
    for item in shortlist:
        schema, table = item["schema"], item["table"]
        cols = [c[0] for c in item["columns"] if c[0] not in _LINEAGE_COLS]
        if not cols:
            continue
        parts = ["count(*) as n"]
        for i, c in enumerate(cols):
            parts.append(f'count("{c}") as nn_{i}')
            parts.append(f'approx_count_distinct("{c}") as dc_{i}')
        try:
            row = con.execute(f"select {', '.join(parts)} from {schema}.{table}").fetchone()
        except Exception:  # noqa: BLE001 -- unprofilable object (view, nested type); census row stands
            continue
        total = row[0] or 0
        for i, c in enumerate(cols):
            non_null = row[1 + i * 2] or 0
            distinct = row[2 + i * 2] or 0
            null_pct = round(100.0 * (total - non_null) / total, 2) if total else 0.0
            is_key = bool(total) and non_null == total and distinct >= total * 0.98
            item["stats"][c] = {"null_pct": null_pct, "distinct": int(distinct), "is_key": is_key}


# --------------------------------------------------------------------------- tier 3

_KEY_SUFFIXES = ("_id", "_code", "_key", "_number", "_no", "_sk")
_TABLE_PREFIXES = ("dim_", "fact_", "stg_", "ref_", "mart_", "raw_")
_NON_KEY_TYPES = ("bool", "date", "time", "float", "double", "decimal", "numeric", "real")


def _key_stem(column: str) -> str | None:
    """`policy_id` -> `policy`. Only id-like columns are foreign-key candidates at all -- a
    boolean or a date whose values happen to be contained in another table's is not a join."""
    c = column.lower()
    for suffix in _KEY_SUFFIXES:
        if c.endswith(suffix) and len(c) > len(suffix):
            return c[: -len(suffix)]
    return None


def _entity_of(table: str) -> str:
    """`dim_policy` / `policies` -> `policy`; `fact_policy_coverage` -> `policy_coverage`."""
    t = table.lower()
    for prefix in _TABLE_PREFIXES:
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    if t.endswith("ies"):
        t = t[:-3] + "y"
    elif t.endswith("sses") or t.endswith("xes"):
        t = t[:-2]
    elif t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    return t


def _owns(table: str, stem: str) -> bool:
    """Whether `table` is the entity `stem` names. Exact entity match, or a domain-prefixed one
    (`awm_portfolio` owns `portfolio`). Deliberately not a substring test: `policy` must not be
    owned by `policy_coverage`, or every fact sharing a policy_id would claim the key."""
    entity = _entity_of(table)
    return entity == stem or entity.endswith("_" + stem)


def _tier3_relationships(con, control: str, scan_id: str, max_pairs: int = 200) -> tuple[int, str | None]:
    """Foreign keys, and how intact they are.

    Two separate questions, answered by two separate things -- conflating them is what the
    earlier versions of this got wrong on real data:

      WHICH way a relationship points is decided by the model: `policy_id` is the identity of
      the table whose entity is `policy`, so dim_policy is the parent and anything else carrying
      policy_id is a child. This is how every data modeller reads a schema, and it's applied
      only to relationships the data has already proven.

      WHETHER it holds is decided by the data: the share of the child's distinct values that
      actually resolve in the parent. 100% is intact. Below that is orphaned references -- a
      real quality finding, reported as one.

    Why not let containment decide direction too: it can't when the data is dirty. On this
    deployment every one of dim_agent's agent_ids appears in dim_policy, but dim_policy also
    carries agent_ids that exist in no agent row -- so "A contained in B" pointed agents at
    policies. The orphans are the finding; they must not silently reverse the model.

    Uniqueness is not used at all: bronze is append-only, so a key that is unique upstream
    repeats once per re-delivered file (the first version keyed on uniqueness and reported
    parties -> addresses).

    Limits, all deliberate: within one schema (cross-schema matches are lineage or
    duplication, reported separately); identical column names only; id-like columns only; and
    a pair where neither side owns the key is two siblings referencing the same parent, which is
    not a relationship between them (fact_claim.policy_id and fact_premium.policy_id both point
    at dim_policy -- not at each other)."""
    rows = con.execute(
        f"select schema_name, table_name, column_name, distinct_count, data_type "
        f"from {control}.estate_column "
        f"where scan_id = ? and profiled = true and distinct_count >= 3", [scan_id]
    ).fetchall()

    groups: dict[tuple[str, str], list[tuple]] = {}
    for schema, table, column, distinct, dtype in rows:
        if column.lower() in _LINEAGE_COLS:
            continue
        if any(t in str(dtype).lower() for t in _NON_KEY_TYPES):
            continue
        stem = _key_stem(column)
        if stem is None:
            continue
        groups.setdefault((schema, column.lower(), stem), []).append((table, column))

    candidates = []
    for (schema, _col, stem), entries in groups.items():
        owners = [e for e in entries if _owns(e[0], stem)]
        if len(owners) != 1:
            continue          # no owner = siblings only; several owners = ambiguous, don't guess
        parent = owners[0]
        for child in entries:
            if child is not parent:
                candidates.append((schema, child, parent))
    candidates = candidates[:max_pairs]

    rel_rows, failed, last_error = [], 0, None
    for schema, (ct, cc), (pt, pc) in candidates:
        try:
            child_n, resolved = con.execute(
                f'with c as (select distinct "{cc}" as v from {schema}.{ct} where "{cc}" is not null) '
                # both columns aliased: unnamed they come back as two identically-named fields,
                # which the Railway image's Arrow rejects outright ("duplicate field names")
                f'select (select count(*) from c) as child_n, '
                f'(select count(*) from c where v in (select "{pc}" from {schema}.{pt})) as resolved'
            ).fetchone()
        except Exception as exc:  # noqa: BLE001 -- no relationship claimed, but the failure is counted
            failed += 1
            last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            continue
        if not child_n:
            continue
        pct = 100.0 * resolved / child_n
        if pct < 50:
            continue          # most values don't resolve: not evidence of a reference at all
        integrity = "intact" if resolved == child_n else ("orphans" if pct >= 90 else "weak")
        rel_rows.append([scan_id, schema, ct, cc, schema, pt, pc, round(pct, 2), integrity])
    con.executemany(f"insert into {control}.estate_relationship values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rel_rows)
    # Not swallowed. Seen live: the same Databricks estate gave 56 references from a laptop and
    # 0 from the Railway container, and the report said "no references found" -- a tier that
    # failed, presented as a clean result.
    why = (f"{failed} of {len(candidates)} reference checks failed; last: {last_error}"
           if failed else None)
    return len(rel_rows), why


# --------------------------------------------------------------------------- mirrors

# Columns a pipeline writes about itself rather than about the business: load lineage, SCD
# validity bookkeeping, and generated surrogate keys (any *_sk). Two tables holding the same
# business facts will still differ in every one of these, because they were loaded or built at
# different times -- so a copy check that includes them reports "different" for tables that are
# not. Verified on this deployment: legacy bronze.* and insurance_bronze.* differ in 100% of raw
# rows and 0% of business rows.
_BUILD_META = _LINEAGE_COLS | {"row_start_date", "row_end_date", "row_is_current", "row_version",
                               "valid_from", "valid_to", "is_current", "etl_loaded_at"}


def _is_build_meta(column: str) -> bool:
    """Pattern-based, because every pipeline names its own bookkeeping differently. This
    deployment's silver layer stamps `silver_loaded_at`; the first version of this list missed
    it, and that single column made three business-identical dimensions report as "diverged" --
    a false "two sources of truth" alarm, caught by checking which column actually differed.

    Deliberately NOT excluded: `created_at` / `updated_at`. In a source system those are usually
    real business audit fields (when did this customer record change), not pipeline stamps, and
    silently ignoring them would hide a genuine divergence."""
    c = column.lower()
    return (c in _BUILD_META or c.endswith("_sk") or c.startswith("_")
            or c.endswith("_loaded_at") or c.endswith("_ingested_at")
            or c.startswith("etl_") or c.startswith("dw_"))


def _detect_mirrors(con, inventory: list[dict[str, Any]]) -> int:
    """Tables that exist twice in the estate -- same name, same columns, same row count, in two
    different schemas -- and whether their business content actually matches.

    Shape alone is only a candidate. The verdict comes from comparing the business columns
    (build metadata excluded) with EXCEPT ALL in both directions, and there are two outcomes
    worth distinguishing, because they carry very different risk:
      identical  a redundant copy -- wasted storage, a cleanup question
      diverged   same shape, different facts -- two sources of truth that disagree, so anyone
                 reading the stale one gets a different answer. On this deployment,
                 silver.dim_party vs insurance_silver.dim_party.

    Each pair is recorded against the table in the unclassified (non-domain) schema, pointing at
    the domain-owned table it mirrors, since the domain copy is the one the platform maintains."""
    by_shape: dict[tuple, list[dict[str, Any]]] = {}
    for item in inventory:
        colset = tuple(sorted(c[0].lower() for c in item["columns"]))
        by_shape.setdefault((item["table"].lower(), colset, item["row_count"]), []).append(item)

    found = 0
    for (_t, _cols, _rows), items in by_shape.items():
        if len(items) != 2:
            continue
        a, b = items
        # record against the copy that isn't domain-owned, pointing at the one that is
        a_owned = a["schema"].lower().startswith(tuple(f"{d.lower()}_" for d in _known_domains()))
        copy, owned = (b, a) if a_owned else (a, b)
        biz = [c[0] for c in copy["columns"] if not _is_build_meta(c[0])]
        if not biz:
            continue
        cols = ", ".join(f'"{c}"' for c in biz)
        try:
            diff_a = con.execute(
                f"select count(*) from (select {cols} from {copy['schema']}.{copy['table']} "
                f"except all select {cols} from {owned['schema']}.{owned['table']})").fetchone()[0]
            diff_b = con.execute(
                f"select count(*) from (select {cols} from {owned['schema']}.{owned['table']} "
                f"except all select {cols} from {copy['schema']}.{copy['table']})").fetchone()[0]
        except Exception:  # noqa: BLE001 -- incomparable column types: no verdict claimed
            continue
        diff = (diff_a or 0) + (diff_b or 0)
        copy["mirrors"] = f"{owned['schema']}.{owned['table']}"
        copy["mirror_state"] = "identical" if diff == 0 else "diverged"
        copy["mirror_diff_rows"] = diff
        found += 1
    return found


# --------------------------------------------------------------------------- tier 4

def _load_dotenv() -> None:
    import os
    env = REPO_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _tier4_enrich(con, control: str, scan_id: str, shortlist: list[dict[str, Any]],
                  batch: int = 6) -> tuple[int, str | None]:
    """A real Gemini call reads the shortlist's structure and says what each table appears to be.
    Batched to keep round trips down.

    Returns (tables_enriched, why_not). A failure leaves the census and statistics intact, but
    it is NOT swallowed: the first version returned a bare 0 on any error, and the scan then
    recorded tier_reached=4 having enriched nothing -- the report claiming a tier that never
    ran. Caught because 7 model batches "finished" in under a second. The reason is now handed
    back so the scan can say what actually happened."""
    if not shortlist:
        return 0, None
    _load_dotenv()
    try:
        from langchain.chat_models import init_chat_model
        model = init_chat_model("google_genai:gemini-2.5-flash")
    except Exception as exc:  # noqa: BLE001 -- reported, not hidden
        return 0, f"enrichment unavailable: {type(exc).__name__}: {str(exc)[:160]}"

    # What the mechanical tiers already measured, handed to the model as fact. The first version
    # gave it only names and types, and on this deployment it then spent its answers guessing at
    # exactly what the scan had already proven: it called raw landing tables "dimension" in one
    # row and "staging" in the next, re-derived "exact duplicate, same row count" (the naive
    # reasoning the mirror check exists to replace -- same shape is not same content), and
    # speculated that key uniqueness "is not guaranteed" when Tier 2 had measured it.
    keys_by_table: dict[tuple[str, str], list[str]] = {}
    for schema, table, column in con.execute(
        f"select schema_name, table_name, column_name from {control}.estate_column "
        f"where scan_id = ? and is_candidate_key = true", [scan_id]
    ).fetchall():
        keys_by_table.setdefault((schema, table), []).append(column)
    mirror_by_table = {
        (s, t): (m, st) for s, t, m, st in con.execute(
            f"select schema_name, table_name, mirrors, mirror_state from {control}.estate_table "
            f"where scan_id = ? and mirrors is not null", [scan_id]).fetchall()
    }
    # The model only sees one batch of ~6 tables at a time. Without this, on this deployment it
    # told us dim_address's "party dimension is not provided" and fact_payment had "no
    # corresponding customer dimension" -- both exist, just in a different batch. A report
    # claiming the estate lacks something it has is worse than no semantic layer. Tier 3 has
    # already proven these links, so each table is told what it resolves to.
    refs_by_table: dict[tuple[str, str], list[str]] = {}
    for fs, ft, fc, tt, integ in con.execute(
        f"select from_schema, from_table, from_column, to_table, confidence "
        f"from {control}.estate_relationship where scan_id = ?", [scan_id]
    ).fetchall():
        refs_by_table.setdefault((fs, ft), []).append(f"{fc} -> {tt} ({integ})")

    def _layer(schema: str) -> str:
        s = schema.lower()
        for layer in ("bronze", "silver", "gold"):
            if s == layer or s.endswith("_" + layer):
                return layer
        return "unknown"

    enriched, failed_batches, last_error = 0, 0, None
    for start in range(0, len(shortlist), batch):
        chunk = shortlist[start:start + batch]
        described = []
        for item in chunk:
            key = (item["schema"], item["table"])
            cols = ", ".join(f"{c[0]} {c[1]}" for c in item["columns"][:40] if c[0] not in _LINEAGE_COLS)
            facts = [f"layer={_layer(item['schema'])}"]
            keys = keys_by_table.get(key)
            facts.append(f"measured unique columns: {', '.join(keys)}" if keys else "no column measured as unique")
            if key in mirror_by_table:
                facts.append(f"verified mirror of {mirror_by_table[key][0]} ({mirror_by_table[key][1]})")
            refs = refs_by_table.get(key)
            if refs:
                facts.append(f"verified references: {', '.join(refs)}")
            described.append(f'- {item["schema"]}.{item["table"]} [{"; ".join(facts)}]: {cols}')
        prompt = (
            "You are describing tables in a data estate. Some facts have ALREADY BEEN MEASURED "
            "from the data and are given in brackets -- treat them as true, and do NOT comment on "
            "uniqueness, duplication, mirroring, row counts or referential integrity at all; those "
            "are reported separately next to your answer.\n\n"
            "IMPORTANT: you are only shown a few tables at a time, not the whole estate. Never "
            "say a related table, dimension or entity is missing, absent or not provided -- you "
            "cannot see it, and it very likely exists.\n\n"
            "Classify by layer first: every table in the bronze layer is raw landing data, so its "
            "entity_type is \"staging\". Only silver/gold tables are dimension/fact/reference.\n\n"
            "Respond with ONLY a JSON array, one object per table, same order, each with keys: "
            "\"table\" (schema.table as given), \"entity_type\" (one of: dimension, fact, "
            "reference, staging, audit, unknown), \"business_meaning\" (one sentence), "
            "\"concerns\" (one sentence on modelling problems the numbers CANNOT show -- wrong "
            "data types, ambiguous naming, unclear grain, sensitive fields -- or an empty "
            "string). Do not invent business rules.\n\n"
            + "\n".join(described)
        )
        try:
            raw = model.invoke(prompt).content.strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                raw = raw[raw.index("\n") + 1:] if "\n" in raw else raw
            parsed = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 -- one bad batch doesn't sink the rest, but it's counted
            failed_batches += 1
            last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            continue
        by_ref = {f'{i["schema"]}.{i["table"]}': i for i in chunk}
        for entry in parsed if isinstance(parsed, list) else []:
            item = by_ref.get(str(entry.get("table", "")))
            if item is None:
                continue          # a table name the model invented or mangled: not written anywhere
            item["business_meaning"] = entry.get("business_meaning", "")
            item["entity_type"] = entry.get("entity_type", "")
            item["concerns"] = entry.get("concerns", "")
            enriched += 1
    if failed_batches:
        return enriched, f"{failed_batches} enrichment batch(es) failed; last: {last_error}"
    return enriched, None


# --------------------------------------------------------------------------- entry point

def run_scan(domain: str, target: str = "duckdb", tiers: int = 4,
             shortlist_size: int = 40, scan_id: str | None = None) -> dict[str, Any]:
    """Runs the scan and returns its summary. Each tier builds on the one before it, so `tiers`
    stops it early -- tiers=1 is the exhaustive census alone, which is the cheap thing you can
    afford to run against everything. `scan_id` lets a caller that runs this in the background
    hand the id back to a client before the scan has written anything."""
    scan_id = scan_id or uuid.uuid4().hex[:12]
    started = _utcnow()
    con, control = _con(target, domain)
    try:
        con.execute(
            f"insert into {control}.estate_scan values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [scan_id, domain, target, "running", 0, 0, 0, started, None, None],
        )
        inventory = _tier1_inventory(con, control, scan_id, domain)
        schemas = len({i["schema"] for i in inventory})
        reached = 1
        if not inventory:
            # The census ran and found nothing; the later tiers have nothing to act on, so they
            # are not claimed. Seen live: the Railway DuckDB holds no loaded tables, and the scan
            # reported "tier 3, 0 tables" with no note -- which reads as "your estate is clean".
            con.execute(
                f"update {control}.estate_scan set status = ?, tier_reached = ?, ended_at = ?, "
                f"note = ? where scan_id = ?",
                ["completed", 1, _utcnow(),
                 f"no tables found on {target} in {domain}'s schemas or in any unclassified schema "
                 f"-- nothing has been loaded here yet", scan_id])
            return {"scan_id": scan_id, "domain": domain, "target": target, "status": "completed",
                    "tier_reached": 1, "schemas_scanned": 0, "tables_scanned": 0, "shortlisted": 0,
                    "enriched": 0, "note": "no tables found",
                    "seconds": round((_utcnow() - started).total_seconds(), 1)}

        shortlist: list[dict[str, Any]] = []
        if tiers >= 2:
            shortlist = _shortlist(inventory, domain, shortlist_size)
            _tier2_profile(con, shortlist)
            reached = 2
        if tiers >= 3:
            _detect_mirrors(con, inventory)       # reads the source tables only; lands in the census rows
        _write_census(con, control, scan_id, inventory)
        con.execute(f"update {control}.estate_scan set schemas_scanned = ?, tables_scanned = ? "
                    f"where scan_id = ?", [schemas, len(inventory), scan_id])
        if tiers >= 3:
            found, t3_note = _tier3_relationships(con, control, scan_id)   # reads the profiled columns just written
            # every check failing is a tier that didn't run, not an estate without references
            reached = 2 if (t3_note and not found) else 3
            notes = [t3_note] if t3_note else []
        else:
            notes = []
        enriched = 0
        if tiers >= 4:
            enriched, t4_note = _tier4_enrich(con, control, scan_id, shortlist)
            if t4_note:
                notes.append(t4_note)
            # Only claim tier 4 if it produced something. A tier that errored out is reported as
            # not reached, with the reason in the note -- never as done.
            if enriched:
                _rewrite_tables(con, control, scan_id, inventory)
                reached = 4
        note = " · ".join(notes) or None

        ended = _utcnow()
        con.execute(
            f"update {control}.estate_scan set status = ?, tier_reached = ?, schemas_scanned = ?, "
            f"tables_scanned = ?, ended_at = ?, note = ? where scan_id = ?",
            ["completed", reached, schemas, len(inventory), ended, note, scan_id],
        )
        return {"scan_id": scan_id, "domain": domain, "target": target, "status": "completed",
                "tier_reached": reached, "schemas_scanned": schemas,
                "tables_scanned": len(inventory), "shortlisted": len(shortlist),
                "enriched": enriched, "note": note,
                "seconds": round((ended - started).total_seconds(), 1)}
    except Exception as exc:  # noqa: BLE001 -- record the failure rather than leaving it "running"
        con.execute(
            f"update {control}.estate_scan set status = ?, note = ?, ended_at = ? where scan_id = ?",
            ["failed", f"{type(exc).__name__}: {exc}", _utcnow(), scan_id],
        )
        raise
    finally:
        con.close()


# --------------------------------------------------------------------------- the report

# Columns that are empty by design, not by neglect. An SCD2 dimension's row_end_date is null on
# every current row -- 93% null on this deployment's dim_policy, and correct. Reporting that as
# a quality hotspot is exactly the kind of alarm a human learns to ignore, which then buries the
# real ones. These are counted and shown as "expected", never silently dropped.
_EXPECTED_NULL = [re.compile(p) for p in (
    r"(^|_)end_date$", r"^row_end", r"valid_to$", r"expir",
    r"(deleted|cancelled|canceled|closed|terminated|archived)_(at|date|on)$",
)]


def _expected_null(column: str) -> bool:
    c = column.lower()
    return any(p.search(c) for p in _EXPECTED_NULL)


def build_report(domain: str, target: str = "duckdb", scan_id: str | None = None,
                 null_threshold: float = 20.0) -> dict[str, Any]:
    """The estate report for one scan (the latest completed one if none is given).

    Everything here is read back from what the scan stored -- nothing is re-derived with a
    fresh guess -- so the report for a given scan_id is reproducible and two reports can be
    diffed. The one live read is the domain's current intents, because readiness is the
    question "can this estate serve what the business wants NOW".

    Open questions are generated mechanically from findings and phrased as questions to a human,
    never as conclusions: an orphaned reference might be a source bug, or might be legitimate
    data the report should exclude, and this has no basis to decide which."""
    con, control = _con(target, domain)
    try:
        if scan_id is None:
            row = con.execute(
                f"select scan_id from {control}.estate_scan where domain = ? and target = ? "
                f"and status = 'completed' order by started_at desc limit 1", [domain, target]
            ).fetchone()
            if row is None:
                return {"domain": domain, "target": target, "scan": None}
            scan_id = row[0]

        s = con.execute(
            f"select scan_id, status, tier_reached, schemas_scanned, tables_scanned, started_at, "
            f"ended_at, note from {control}.estate_scan where scan_id = ?", [scan_id]).fetchone()
        scan = dict(zip(["scan_id", "status", "tier_reached", "schemas_scanned", "tables_scanned",
                         "started_at", "ended_at", "note"], s))
        scan["started_at"], scan["ended_at"] = _iso_utc(scan["started_at"]), _iso_utc(scan["ended_at"])

        tcols = ["schema_name", "table_name", "column_count", "row_count", "shortlisted",
                 "shortlist_reason", "classification", "business_meaning", "entity_type",
                 "concerns", "mirrors", "mirror_state", "mirror_diff_rows"]
        tables = [dict(zip(tcols, r)) for r in con.execute(
            f"select {', '.join(tcols)} from {control}.estate_table where scan_id = ? "
            f"order by schema_name, table_name", [scan_id]).fetchall()]

        ccols = ["schema_name", "table_name", "column_name", "data_type", "null_pct",
                 "distinct_count", "is_candidate_key", "sensitivity", "profiled"]
        columns = [dict(zip(ccols, r)) for r in con.execute(
            f"select {', '.join(ccols)} from {control}.estate_column where scan_id = ?",
            [scan_id]).fetchall()]

        rcols = ["from_schema", "from_table", "from_column", "to_schema", "to_table",
                 "to_column", "overlap_pct", "confidence"]
        rels = [dict(zip(rcols, r)) for r in con.execute(
            f"select {', '.join(rcols)} from {control}.estate_relationship where scan_id = ? "
            f"order by overlap_pct", [scan_id]).fetchall()]
    finally:
        con.close()

    domain_owned = {t["schema_name"] for t in tables if t["classification"] == domain}
    classification = {(t["schema_name"], t["table_name"]): t["classification"] for t in tables}

    # ---- coverage
    by_schema: dict[str, dict[str, Any]] = {}
    for t in tables:
        b = by_schema.setdefault(t["schema_name"], {"schema": t["schema_name"],
                                                    "classification": t["classification"],
                                                    "tables": 0, "rows": 0})
        b["tables"] += 1
        b["rows"] += t["row_count"] or 0
    coverage = {
        "schemas": len(by_schema),
        "tables": len(tables),
        "columns": len(columns),
        "rows": sum(t["row_count"] or 0 for t in tables),
        "profiled_tables": sum(1 for t in tables if t["shortlisted"]),
        "enriched_tables": sum(1 for t in tables if t["entity_type"]),
        "unclassified_schemas": sorted(s for s, b in by_schema.items() if b["classification"] == "unclassified"),
        "by_schema": sorted(by_schema.values(), key=lambda b: (b["classification"] != domain, b["schema"])),
    }

    # ---- entity map (tier 4, domain-owned only -- mirrors would just repeat it)
    entities = [
        {"table": f"{t['schema_name']}.{t['table_name']}", "entity_type": t["entity_type"],
         "business_meaning": t["business_meaning"], "concerns": t["concerns"], "rows": t["row_count"]}
        for t in tables if t["entity_type"] and t["schema_name"] in domain_owned
    ]

    # ---- relationships (domain-owned; the legacy mirrors produce an identical graph)
    rel_view = [r for r in rels if r["from_schema"] in domain_owned]
    relationships = {
        "intact": [r for r in rel_view if r["confidence"] == "intact"],
        "orphans": [r for r in rel_view if r["confidence"] == "orphans"],
        "weak": [r for r in rel_view if r["confidence"] == "weak"],
    }

    # ---- sensitivity register
    sensitive = [c for c in columns if c["sensitivity"] and c["schema_name"] in domain_owned]
    register: dict[str, list[str]] = {}
    for c in sensitive:
        register.setdefault(c["sensitivity"], []).append(f"{c['schema_name']}.{c['table_name']}.{c['column_name']}")
    sensitivity = {"total": len(sensitive),
                   "by_class": [{"class": k, "count": len(v), "columns": sorted(v)}
                                for k, v in sorted(register.items(), key=lambda kv: -len(kv[1]))]}

    # ---- quality hotspots
    hot, expected = [], []
    for c in columns:
        if c["schema_name"] not in domain_owned or c["null_pct"] is None or _is_build_meta(c["column_name"]):
            continue
        if c["null_pct"] < null_threshold:
            continue
        entry = {"column": f"{c['schema_name']}.{c['table_name']}.{c['column_name']}", "null_pct": c["null_pct"]}
        (expected if _expected_null(c["column_name"]) else hot).append(entry)
    quality = {"threshold_pct": null_threshold,
               "hotspots": sorted(hot, key=lambda h: -h["null_pct"]),
               "expected_nulls": sorted(expected, key=lambda h: -h["null_pct"])}

    # ---- mirrors
    mirrored = [t for t in tables if t["mirrors"]]
    mirrors = {
        "identical": [{"table": f"{t['schema_name']}.{t['table_name']}", "mirrors": t["mirrors"],
                       "rows": t["row_count"]} for t in mirrored if t["mirror_state"] == "identical"],
        "diverged": [{"table": f"{t['schema_name']}.{t['table_name']}", "mirrors": t["mirrors"],
                      "differing_rows": t["mirror_diff_rows"]} for t in mirrored if t["mirror_state"] == "diverged"],
    }

    # ---- readiness per intent
    readiness = []
    try:
        from emitters.intent import list_intents
        intents = list_intents(domain)
    except Exception:  # noqa: BLE001
        intents = []
    by_colname: dict[str, list[dict[str, Any]]] = {}
    for c in columns:
        if c["schema_name"] in domain_owned:
            by_colname.setdefault(c["column_name"].lower(), []).append(c)
    for it in intents:
        points = []
        for rep in it.get("reports", []) or []:
            for dp in rep.get("required_data_points", []) or []:
                hits = by_colname.get(str(dp).strip().lower(), [])
                worst = max((h["null_pct"] or 0 for h in hits), default=0)
                status = "missing" if not hits else ("quality" if worst >= null_threshold else "ready")
                points.append({"report": rep.get("name"), "data_point": dp, "status": status,
                               "found_in": sorted({f"{h['schema_name']}.{h['table_name']}" for h in hits}),
                               "worst_null_pct": worst if hits else None})
        ready = sum(1 for p in points if p["status"] == "ready")
        readiness.append({"intent": it.get("name") or it.get("intent_id"), "data_points": points,
                          "ready": ready, "total": len(points),
                          "score_pct": round(100.0 * ready / len(points), 1) if points else None})

    # ---- open questions
    questions: list[dict[str, str]] = []
    for s_name in coverage["unclassified_schemas"]:
        b = by_schema[s_name]
        questions.append({"kind": "ownership", "severity": "medium",
                          "question": f"Schema `{s_name}` belongs to no domain ({b['tables']} tables, "
                                      f"{b['rows']:,} rows). Who owns it, and does anything still read it?"})
    ident_by_schema: dict[str, list[str]] = {}
    for m in mirrors["identical"]:
        ident_by_schema.setdefault(m["table"].split(".")[0], []).append(m["mirrors"].split(".")[0])
    for s_name, targets in sorted(ident_by_schema.items()):
        questions.append({"kind": "duplication", "severity": "low",
                          "question": f"All {len(targets)} mirrored tables in `{s_name}` are business-identical "
                                      f"copies of `{sorted(set(targets))[0]}`. Safe to retire?"})
    for m in mirrors["diverged"]:
        questions.append({"kind": "duplication", "severity": "high",
                          "question": f"`{m['table']}` and `{m['mirrors']}` have the same shape but differ in "
                                      f"{m['differing_rows']:,} rows of business data. Which is the source of truth?"})

    orphan_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in relationships["orphans"] + relationships["weak"]:
        key = (_entity_of(r["from_table"]), r["from_column"].lower(), _entity_of(r["to_table"]))
        g = orphan_groups.setdefault(key, {"pct": r["overlap_pct"], "layers": set(), "example": r})
        g["layers"].add(r["from_schema"])
        g["pct"] = min(g["pct"], r["overlap_pct"])
    for (child, col, parent), g in sorted(orphan_groups.items(), key=lambda kv: kv[1]["pct"]):
        where = ", ".join(sorted(g["layers"]))
        origin = " — present in every layer, so it originates at source" if len(g["layers"]) > 1 else ""
        questions.append({"kind": "integrity", "severity": "high" if g["pct"] < 95 else "medium",
                          "question": f"{round(100 - g['pct'], 2)}% of {child}.{col} values reference no "
                                      f"{parent} ({where}){origin}. Fix at source, or exclude from reporting?"})
    for rd in readiness:
        for p in rd["data_points"]:
            if p["status"] == "missing":
                questions.append({"kind": "gap", "severity": "high",
                                  "question": f"Intent “{rd['intent']}” needs `{p['data_point']}`, but no column of "
                                              f"that name exists anywhere in the estate. Where does it come from?"})
            elif p["status"] == "quality":
                questions.append({"kind": "gap", "severity": "medium",
                                  "question": f"Intent “{rd['intent']}” needs `{p['data_point']}` — found in "
                                              f"{', '.join(p['found_in'])}, but up to {p['worst_null_pct']}% null."})
    for h in quality["hotspots"][:10]:
        questions.append({"kind": "quality", "severity": "medium",
                          "question": f"`{h['column']}` is {h['null_pct']}% null. Expected, or a capture problem upstream?"})

    order = {"high": 0, "medium": 1, "low": 2}
    questions.sort(key=lambda q: order[q["severity"]])

    return {"domain": domain, "target": target, "scan": scan, "coverage": coverage,
            "entities": entities, "relationships": relationships, "sensitivity": sensitivity,
            "quality": quality, "mirrors": mirrors, "readiness": readiness,
            "open_questions": questions}


def close_abandoned(domain: str, target: str, live_scan_id: str | None) -> int:
    """A scan whose process died (a restart, a deploy, a killed worker) leaves its row saying
    "running" forever, and the Control Room would spin on it indefinitely. The caller -- the one
    process that runs scans for this deployment -- knows which scan is genuinely live; every other
    running row is closed as failed, with the reason stated rather than a silent delete."""
    con, control = _con(target, domain)
    try:
        stale = [r[0] for r in con.execute(
            f"select scan_id from {control}.estate_scan "
            f"where domain = ? and target = ? and status = 'running'", [domain, target]).fetchall()
            if r[0] != live_scan_id]
        for sid in stale:
            con.execute(
                f"update {control}.estate_scan set status = ?, note = ?, ended_at = ? where scan_id = ?",
                ["failed", "interrupted: the process running this scan stopped before it finished",
                 _utcnow(), sid])
        return len(stale)
    finally:
        con.close()


def list_scans(domain: str, target: str = "duckdb", limit: int = 10) -> list[dict[str, Any]]:
    con, control = _con(target, domain)
    try:
        rows = con.execute(
            f"select scan_id, status, tier_reached, schemas_scanned, tables_scanned, "
            f"started_at, ended_at, note from {control}.estate_scan "
            f"where domain = ? and target = ? order by started_at desc", [domain, target]
        ).fetchall()
        keys = ["scan_id", "status", "tier_reached", "schemas_scanned", "tables_scanned",
                "started_at", "ended_at", "note"]
        scans = [dict(zip(keys, r)) for r in rows][:limit]
        for sc in scans:
            sc["started_at"], sc["ended_at"] = _iso_utc(sc["started_at"]), _iso_utc(sc["ended_at"])
        return scans
    finally:
        con.close()
