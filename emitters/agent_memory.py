"""Phase 1 of the agent memory layer: durable, retrievable facts an agent has learned across
conversations, so a new chat with Mr. Program Manager or The Master Architect doesn't start from
zero every time. Deliberately built on the platform's own control plane (DuckDB/Databricks)
rather than a new graph database -- the actual need (store a fact, retrieve what's relevant to a
new question) is well served by a table plus a semantic-similarity read, and every other
subsystem in this project already lives in the control schema next to run_registry/incident_
ticket/etc. Adding a dedicated graph DB would be new infrastructure this project doesn't need
yet, not a better fit for what "the agent remembers" actually requires here.

A memory row is written at a concrete, high-confidence moment -- when a catalogue_chat
conversation actually produces a structured suggestion, not from every casual turn -- and read
back via cosine similarity over Gemini embeddings, computed in Python rather than a vector index
(memory volume per domain is small; a full scan is the honest, simple choice at this scale, not
a shortcut). A human can see and delete what's been remembered -- see list_memory/forget -- so
this is never a black box.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters.control_plane import ensure_control_schema  # noqa: E402
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402

_DDL = """
create table if not exists {control}.agent_memory (
    memory_id    bigint primary key,
    domain       varchar,
    agent        varchar,
    subject      varchar,
    content      varchar,
    source       varchar,
    embedding    varchar,
    created_at   timestamp
)
"""

_EMBED_MODEL = "models/gemini-embedding-001"


def _load_platform(target: str) -> dict:
    import yaml
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


def _con(target: str, domain: str):
    platform = _load_platform(target)
    control = resolve_schema(platform, domain, "control")
    con = sql_connect(target, platform)
    ensure_control_schema(con, control)
    con.execute(_DDL.format(control=control))
    return con, control


def _embed(text: str) -> list[float]:
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    emb = GoogleGenerativeAIEmbeddings(model=_EMBED_MODEL)
    return emb.embed_query(text)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def remember(domain: str, agent: str, subject: str, content: str, source: str, target: str = "duckdb") -> int:
    """Writes one durable fact. Retry-safe id assignment -- same jittered-backoff pattern as
    raise_ticket/log_sdlc_stage, proven necessary by a real Phase C bug, not theoretical."""
    import random
    import time
    con, control = _con(target, domain)
    try:
        embedding = json.dumps(_embed(f"{subject}\n{content}"))
        now = _dt.datetime.now(_dt.timezone.utc)
        for attempt in range(5):
            try:
                memory_id = con.execute(f"select coalesce(max(memory_id), 0) + 1 from {control}.agent_memory").fetchone()[0]
                con.execute(
                    f"insert into {control}.agent_memory values (?, ?, ?, ?, ?, ?, ?, ?)",
                    [memory_id, domain, agent, subject, content, source, embedding, now],
                )
                return memory_id
            except Exception:  # noqa: BLE001 -- retry on a concurrent-insert conflict; re-raise otherwise
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1) + random.random() * 0.03)
    finally:
        con.close()


def recall(domain: str, agent: str, query: str, k: int = 5, target: str = "duckdb") -> list[dict[str, Any]]:
    """Every memory for this domain+agent, ranked by semantic similarity to `query`. Returns []
    (not an error) when there's nothing on record yet -- a new domain's first conversation is a
    real, expected case, not a failure."""
    con, control = _con(target, domain)
    try:
        rows = con.execute(
            f"select memory_id, subject, content, source, embedding, created_at from {control}.agent_memory "
            f"where domain = ? and agent = ? order by created_at desc",
            [domain, agent],
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return []
    query_vec = _embed(query)
    scored = []
    for memory_id, subject, content, source, embedding, created_at in rows:
        score = _cosine(query_vec, json.loads(embedding))
        scored.append({"memory_id": memory_id, "subject": subject, "content": content,
                       "source": source, "created_at": created_at, "score": round(score, 4)})
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:k]


def list_memory(domain: str, agent: str | None = None, target: str = "duckdb") -> list[dict[str, Any]]:
    con, control = _con(target, domain)
    try:
        if agent:
            rows = con.execute(
                f"select memory_id, agent, subject, content, source, created_at from {control}.agent_memory "
                f"where domain = ? and agent = ? order by created_at desc",
                [domain, agent],
            ).fetchall()
        else:
            rows = con.execute(
                f"select memory_id, agent, subject, content, source, created_at from {control}.agent_memory "
                f"where domain = ? order by created_at desc",
                [domain],
            ).fetchall()
        return [{"memory_id": r[0], "agent": r[1], "subject": r[2], "content": r[3],
                "source": r[4], "created_at": r[5]} for r in rows]
    finally:
        con.close()


def forget(domain: str, memory_id: int, target: str = "duckdb") -> bool:
    con, control = _con(target, domain)
    try:
        con.execute(f"delete from {control}.agent_memory where domain = ? and memory_id = ?", [domain, memory_id])
        return True
    finally:
        con.close()
