"""Unifies duckdb.DuckDBPyConnection and databricks.sql.Connection behind one
.execute(sql, params).fetchall()/.fetchone() interface (matching duckdb's own chained-call
shape), transpiling SQL authored once in duckdb dialect to the connection's target dialect via
sqlglot (ADR-001's resolution: CLAUDE.md rule 2 held that "never write platform-specific SQL by
hand" -- this is the one place per platform that knows about SQL differences; every other module
writes exactly one SQL string per query, in duckdb dialect, and never branches on target.

Empirically verified against a live Databricks SQL warehouse before relying on it here (not
assumed from sqlglot's dialect docs alone):
  - '?' positional placeholders work against databricks-sql-connector despite it reporting
    paramstyle='named' -- no query rewriting needed for parameter binding.
  - DATEDIFF(HOUR, start, end) -- sqlglot's transpile target -- is valid Databricks SQL.
  - ON CONFLICT is NOT: sqlglot transpiles it to itself rather than a valid MERGE INTO, which
    would silently produce a syntax error at execute time, not a working upsert. The two call
    sites that needed this (emitters/control_plane.py) were rewritten as DELETE+INSERT instead,
    which needs no dialect-specific SQL at all -- not worked around with a hand-written MERGE
    branch, which would just reintroduce the rule this module exists to uphold.
"""
from __future__ import annotations

import pathlib
from typing import Any

import duckdb
import sqlglot
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class SqlConnection:
    def __init__(self, raw: Any, dialect: str) -> None:
        self._raw = raw
        self.dialect = dialect

    def executemany(self, sql: str, param_rows: list[list], chunk_size: int = 500) -> None:
        """Batched insert -- one round trip for many rows instead of one per row.

        Row-by-row execute() is fine for local DuckDB but was measured as impractically slow
        against a remote Databricks warehouse (network round-trip per statement) -- discovered
        loading orders' 3,359 rows there, ~540 rows staged before the run was abandoned.

        The DBAPI cursor's own .executemany() looked like the fix, but databricks-sql-connector's
        implementation is a loop over .execute() with NO batching -- its own docstring says so
        ("This will issue N sequential request... No optimizations of the query (like batching)
        will be performed"), confirmed by measurement: same ~3 rows/sec after switching to it.
        This builds one real multi-row `INSERT ... VALUES (...), (...), ...` statement per chunk
        instead, which both drivers execute as a single round trip.

        Only handles a bare `insert into t values (?, ?, ...)` shape (this codebase's only use)
        -- raises rather than silently mis-batching anything else.
        """
        if not param_rows:
            return
        prefix, _, value_tuple = sql.partition(" values ")
        if not value_tuple or not prefix.strip().lower().startswith("insert into"):
            raise ValueError(
                f"executemany() only supports 'insert into t values (?, ?, ...)' -- got: {sql!r}"
            )
        n_params = value_tuple.count("?")

        for start in range(0, len(param_rows), chunk_size):
            chunk = param_rows[start:start + chunk_size]
            batched_sql = f"{prefix} values " + ", ".join([value_tuple.strip()] * len(chunk))
            flat_params = [v for row in chunk for v in row]
            assert len(flat_params) == n_params * len(chunk)
            self.execute(batched_sql, flat_params)

    def execute(self, sql: str, params: list | None = None):
        """One statement per call, by contract -- a multi-statement string is a caller bug,
        not something to paper over: sqlglot splits multi-statement input into several
        transpiled statements, and Databricks' connector rejects more than one per execute()
        outright, so silently running only the first (or concatenating) would hide exactly the
        kind of mistake this wrapper exists to catch."""
        if ";" in sql.strip().rstrip(";"):
            raise ValueError(
                "SqlConnection.execute() takes one statement -- split multi-statement DDL into "
                "separate execute() calls (see ensure_control_schema for the pattern)."
            )
        if self.dialect != "duckdb":
            sql = sqlglot.transpile(sql, read="duckdb", write=self.dialect)[0]
        if self.dialect == "duckdb":
            return self._raw.execute(sql, params or [])
        cur = self._raw.cursor()
        cur.execute(sql, params or [])
        return cur

    def close(self) -> None:
        self._raw.close()


def connect(target: str, platform: dict[str, Any]) -> SqlConnection:
    """target is also the sqlglot dialect name -- both duckdb and databricks are valid values
    for each. If a future platform's storage engine and SQL dialect ever diverge, this is the
    one place that would need to know the difference."""
    if target == "duckdb":
        duckdb_path = REPO_ROOT / "harness" / "jarvis.duckdb"
        duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        return SqlConnection(duckdb.connect(str(duckdb_path)), "duckdb")

    if target == "databricks":
        import os

        from databricks import sql as databricks_sql

        env_path = REPO_ROOT / ".env"
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

        raw = databricks_sql.connect(
            server_hostname=os.environ["DATABRICKS_HOST"].replace("https://", ""),
            http_path=os.environ["DATABRICKS_HTTP_PATH"],
            access_token=os.environ["DATABRICKS_TOKEN"],
            catalog=platform["storage"]["catalog"],
        )
        return SqlConnection(raw, "databricks")

    raise NotImplementedError(f"target={target!r} not implemented -- only duckdb and databricks are.")
