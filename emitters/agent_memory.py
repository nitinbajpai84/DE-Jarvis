"""Long-term agent memory: durable, retrievable facts an agent has learned across conversations,
so a new chat with The Delivery Lead or The Chief Architect doesn't start from zero every time.

The three layers an agent draws on, and where each lives:
  short-term   the conversation itself and its rolling summary     emitters/agent_sessions.py
  long-term    what it has learned about this company, across chats  this module
  context      what the platform's records say right now             emitters/agent_context.py

Scope. Facts, decisions and preferences belong to the whole team (stored under agent "team"):
a decision agreed with The Data Detective must be known to The Delivery Lead -- on a real run it
wasn't, and the Delivery Lead answered "I don't recall any agreement". Corrections stay with the
agent that was corrected. recall() reads an agent's own memories plus the team's.

Each memory has a kind, because they are used differently:
  fact        something true about this company that no record holds ("claims close monthly")
  decision    something the team agreed ("exclude orphaned agent rows from the dashboard")
  preference  how this company wants things done ("show amounts in SGD thousands")
  correction  where a human told the agent it was wrong, so the mistake doesn't come back

Deliberately built on the platform's own control plane rather than a new graph database or
vector store -- the need (store a fact, retrieve what's relevant to a new question) is served by a
table plus a semantic-similarity read, and every other subsystem already lives in the control
schema. Retrieval is cosine similarity over Gemini embeddings computed in Python: memory volume
per company is small, so a full scan is the honest, simple choice at this scale. A human can see
and delete everything remembered (list_memory / forget) -- never a black box.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import pathlib
import sys
import threading
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
    created_at   timestamp,
    kind         varchar,
    session_id   varchar
)
"""

KINDS = ("fact", "decision", "preference", "correction")
TEAM = "team"
TEAM_KINDS = ("fact", "decision", "preference")


def scope_for(agent: str, kind: str) -> str:
    """Which memory a new memory is filed under: the team's, or only this agent's."""
    return TEAM if kind in TEAM_KINDS else agent


# Columns added after the table already existed on live control planes: check-then-alter, the
# same approach as estate._MIGRATIONS (Databricks has no ADD COLUMN IF NOT EXISTS). Memories from
# before kinds existed were all drafts a conversation proposed -- facts.
_MIGRATIONS = [("kind", "varchar", "fact"), ("session_id", "varchar", None)]
_READY: set[tuple[str, str]] = set()
_LOCK = threading.Lock()

_EMBED_MODEL = "models/gemini-embedding-001"


def _load_platform(target: str) -> dict:
    import yaml
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


def _con(target: str, domain: str):
    platform = _load_platform(target)
    control = resolve_schema(platform, domain, "control")
    con = sql_connect(target, platform)
    if (target, control) not in _READY:
        with _LOCK:   # serialised: concurrent first-use DDL conflicts on DuckDB (see estate._con)
            if (target, control) not in _READY:
                ensure_control_schema(con, control)
                con.execute(_DDL.format(control=control))
                cols = {d[0].lower() for d in con.execute(f"select * from {control}.agent_memory limit 0").description}
                for column, ddl_type, backfill in _MIGRATIONS:
                    if column not in cols:
                        con.execute(f"alter table {control}.agent_memory add column {column} {ddl_type}")
                        if backfill is not None:
                            con.execute(f"update {control}.agent_memory set {column} = ? where {column} is null", [backfill])
                _READY.add((target, control))
    return con, control


def _iso_utc(value: Any) -> Any:
    """Stored as naive UTC; sent with an explicit Z so a browser doesn't read it as local time
    (the same "8h ago" bug the estate scan had -- see estate._utcnow)."""
    if isinstance(value, _dt.datetime):
        return (value.astimezone(_dt.timezone.utc).replace(tzinfo=None) if value.tzinfo else value).isoformat() + "Z"
    return value


def _embed(text: str) -> list[float]:
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    emb = GoogleGenerativeAIEmbeddings(model=_EMBED_MODEL)
    return emb.embed_query(text)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def remember(domain: str, agent: str, subject: str, content: str, source: str, target: str = "duckdb",
             kind: str = "fact", session_id: str | None = None, embedding: list[float] | None = None) -> int:
    """Writes one durable memory. Retry-safe id assignment -- same jittered-backoff pattern as
    raise_ticket/log_sdlc_stage, proven necessary by a real Phase C bug, not theoretical."""
    import random
    import time
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    con, control = _con(target, domain)
    try:
        vector = json.dumps(embedding if embedding is not None else _embed(f"{subject}\n{content}"))
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        for attempt in range(5):
            try:
                memory_id = con.execute(f"select coalesce(max(memory_id), 0) + 1 from {control}.agent_memory").fetchone()[0]
                con.execute(
                    f"insert into {control}.agent_memory values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [memory_id, domain, agent, subject, content, source, vector, now, kind, session_id],
                )
                return memory_id
            except Exception:  # noqa: BLE001 -- retry on a concurrent-insert conflict; re-raise otherwise
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1) + random.random() * 0.03)
    finally:
        con.close()


def recall(domain: str, agent: str, query: str, k: int = 5, target: str = "duckdb",
           query_vec: list[float] | None = None, min_score: float = 0.0) -> list[dict[str, Any]]:
    """This company's memories for this agent, ranked by semantic similarity to `query`. Returns
    [] (not an error) when nothing is on record -- a first conversation is a real, expected case.
    `query_vec` lets a caller that already embedded the query avoid paying for it twice."""
    con, control = _con(target, domain)
    try:
        rows = con.execute(
            f"select memory_id, subject, content, source, embedding, created_at, kind, agent from {control}.agent_memory "
            f"where domain = ? and agent in (?, ?) order by created_at desc",
            [domain, agent, TEAM],
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return []
    query_vec = query_vec if query_vec is not None else _embed(query)
    scored = []
    for memory_id, subject, content, source, embedding, created_at, kind, owner in rows:
        score = _cosine(query_vec, json.loads(embedding))
        if score < min_score:
            continue
        scored.append({"memory_id": memory_id, "subject": subject, "content": content, "kind": kind or "fact",
                       "source": source, "created_at": _iso_utc(created_at), "score": round(score, 4),
                       "scope": "team" if owner == TEAM else "agent"})
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:k]


def list_memory(domain: str, agent: str | None = None, target: str = "duckdb") -> list[dict[str, Any]]:
    """With an agent: what that agent recalls from -- its own memories and the team's."""
    con, control = _con(target, domain)
    cols = "memory_id, agent, subject, content, source, created_at, kind, session_id"
    try:
        if agent:
            rows = con.execute(
                f"select {cols} from {control}.agent_memory where domain = ? and agent in (?, ?) order by created_at desc",
                [domain, agent, TEAM]).fetchall()
        else:
            rows = con.execute(
                f"select {cols} from {control}.agent_memory where domain = ? order by created_at desc",
                [domain]).fetchall()
        return [{"memory_id": r[0], "agent": r[1], "subject": r[2], "content": r[3], "source": r[4],
                 "created_at": _iso_utc(r[5]), "kind": r[6] or "fact", "session_id": r[7]} for r in rows]
    finally:
        con.close()


def forget(domain: str, memory_id: int, target: str = "duckdb") -> bool:
    con, control = _con(target, domain)
    try:
        con.execute(f"delete from {control}.agent_memory where domain = ? and memory_id = ?", [domain, memory_id])
        return True
    finally:
        con.close()
