"""Short-term agent memory: the conversation.

A conversation with an agent is a session, stored in the control plane rather than the browser,
so it survives a closed tab, can be picked up on another device, and is what the agent actually
read when it answered. Each session belongs to one login, one company and one agent.

What the model sees of it is bounded. The last WINDOW messages go in verbatim; everything older
is folded into a rolling summary, written by a model call once enough unsummarised turns pile up
(SUMMARISE_AFTER). The full transcript is always kept -- the summary is what the agent carries
forward, not a replacement for the record.
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys
import threading
import uuid
from typing import Any, Callable

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters.control_plane import ensure_control_schema  # noqa: E402
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402

WINDOW = 8
SUMMARISE_AFTER = 16

_DDL = [
    """create table if not exists {c}.agent_session (
        session_id varchar primary key, domain varchar, agent varchar, username varchar,
        title varchar, summary varchar, summarized_upto integer, message_count integer,
        created_at timestamp, updated_at timestamp)""",
    """create table if not exists {c}.agent_message (
        session_id varchar, seq integer, role varchar, content varchar, trace varchar,
        created_at timestamp)""",
]
_READY: set[tuple[str, str]] = set()
_LOCK = threading.Lock()


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _iso(v: Any) -> Any:
    return v.isoformat() + "Z" if isinstance(v, _dt.datetime) else v


def _con(domain: str, target: str = "duckdb"):
    import yaml
    platform = yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())
    control = resolve_schema(platform, domain, "control")
    con = sql_connect(target, platform)
    if (target, control) not in _READY:
        with _LOCK:
            if (target, control) not in _READY:
                ensure_control_schema(con, control)
                for stmt in _DDL:
                    con.execute(stmt.format(c=control))
                _READY.add((target, control))
    return con, control


_SESSION_COLS = ["session_id", "domain", "agent", "username", "title", "summary", "summarized_upto",
                 "message_count", "created_at", "updated_at"]


def create_session(domain: str, agent: str, username: str, title: str = "New conversation") -> dict[str, Any]:
    sid = uuid.uuid4().hex[:16]
    con, c = _con(domain)
    try:
        now = _now()
        con.execute(f"insert into {c}.agent_session values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [sid, domain, agent, username, title, None, 0, 0, now, now])
    finally:
        con.close()
    return get_session(sid, domain, username)


def get_session(session_id: str, domain: str, username: str) -> dict[str, Any]:
    """Only the login that owns a session can read it, and only within its company."""
    con, c = _con(domain)
    try:
        row = con.execute(f"select {', '.join(_SESSION_COLS)} from {c}.agent_session "
                          f"where session_id = ? and domain = ? and username = ?",
                          [session_id, domain, username]).fetchone()
    finally:
        con.close()
    if row is None:
        raise KeyError(f"no conversation {session_id!r} for this login")
    s = dict(zip(_SESSION_COLS, row))
    s["created_at"], s["updated_at"] = _iso(s["created_at"]), _iso(s["updated_at"])
    return s


def list_sessions(domain: str, agent: str, username: str, limit: int = 20) -> list[dict[str, Any]]:
    con, c = _con(domain)
    try:
        rows = con.execute(f"select {', '.join(_SESSION_COLS)} from {c}.agent_session "
                           f"where domain = ? and agent = ? and username = ? order by updated_at desc limit {int(limit)}",
                           [domain, agent, username]).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        s = dict(zip(_SESSION_COLS, r))
        s["created_at"], s["updated_at"] = _iso(s["created_at"]), _iso(s["updated_at"])
        out.append(s)
    return out


def messages(session_id: str, domain: str) -> list[dict[str, Any]]:
    import json
    con, c = _con(domain)
    try:
        rows = con.execute(f"select seq, role, content, trace, created_at from {c}.agent_message "
                           f"where session_id = ? order by seq", [session_id]).fetchall()
    finally:
        con.close()
    return [{"seq": r[0], "role": r[1], "content": r[2], "trace": json.loads(r[3]) if r[3] else None,
             "created_at": _iso(r[4])} for r in rows]


def append(session: dict[str, Any], role: str, content: str, trace: dict | None = None) -> int:
    import json
    con, c = _con(session["domain"])
    try:
        seq = con.execute(f"select coalesce(max(seq), 0) + 1 from {c}.agent_message where session_id = ?",
                          [session["session_id"]]).fetchone()[0]
        con.execute(f"insert into {c}.agent_message values (?, ?, ?, ?, ?, ?)",
                    [session["session_id"], seq, role, content, json.dumps(trace, default=str) if trace else None, _now()])
        title = session["title"]
        if role == "user" and seq == 1:
            title = (content.strip().splitlines() or ["New conversation"])[0][:80]
        con.execute(f"update {c}.agent_session set message_count = ?, updated_at = ?, title = ? where session_id = ?",
                    [seq, _now(), title, session["session_id"]])
        session["title"], session["message_count"] = title, seq
        return seq
    finally:
        con.close()


def window(session: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    """(rolling summary, the recent messages the model sees verbatim)."""
    msgs = messages(session["session_id"], session["domain"])
    upto = session.get("summarized_upto") or 0
    recent = [m for m in msgs if m["seq"] > upto][-WINDOW:]
    return session.get("summary"), recent


def maybe_summarise(session: dict[str, Any], summarise: Callable[[str | None, list[dict[str, Any]]], str]) -> dict[str, Any] | None:
    """Folds older turns into the rolling summary once more than SUMMARISE_AFTER messages sit
    unsummarised, keeping the latest WINDOW verbatim. Returns what happened, or None."""
    msgs = messages(session["session_id"], session["domain"])
    upto = session.get("summarized_upto") or 0
    pending = [m for m in msgs if m["seq"] > upto]
    if len(pending) <= SUMMARISE_AFTER:
        return None
    fold = pending[:-WINDOW]
    new_summary = summarise(session.get("summary"), fold)
    new_upto = fold[-1]["seq"]
    con, c = _con(session["domain"])
    try:
        con.execute(f"update {c}.agent_session set summary = ?, summarized_upto = ? where session_id = ?",
                    [new_summary, new_upto, session["session_id"]])
    finally:
        con.close()
    session["summary"], session["summarized_upto"] = new_summary, new_upto
    return {"folded_messages": len(fold), "summarized_upto": new_upto}
