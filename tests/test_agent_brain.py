"""Agent brain, context layer, short- and long-term memory -- offline: a scripted model and a
deterministic embedding stand in for Gemini, so these test the machinery around the model (what it
is shown, what it may call, what gets remembered and summarised), not the model's prose."""
import hashlib
import json
import math

import duckdb
import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from emitters import agent_brain, agent_context, agent_memory, agent_sessions
from emitters.sql_dialect import SqlConnection


def _vec(text: str) -> list[float]:
    """Bag-of-words hashed into 64 dims: similar wording -> high cosine, like a real embedding."""
    v = [0.0] * 64
    for w in text.lower().replace(":", " ").replace(".", " ").replace("?", " ").split():
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class ScriptedModel:
    """Chat turns: first asks for a tool, then answers citing the context. Utility prompts
    (memory extraction, summaries) get whatever the test queued."""

    def __init__(self, world):
        self.world = world

    def bind_tools(self, tools):
        self.world["bound_tools"] = [t.name for t in tools]
        return self

    def invoke(self, msgs):
        if isinstance(msgs, str):
            self.world["utility_prompts"].append(msgs)
            if msgs.startswith("Update the running summary"):
                return AIMessage(content="Summary: they asked about claims several times.")
            return AIMessage(content=self.world["extract"].pop(0) if self.world["extract"] else "[]")
        self.world["chat_calls"].append(msgs)
        if not any(isinstance(m, ToolMessage) for m in msgs) and self.world.get("use_tool"):
            return AIMessage(content="", tool_calls=[{"name": "search_estate_columns", "args": {"name_contains": "claim"}, "id": "c1"}],
                             usage_metadata={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105})
        return AIMessage(content="Claims land daily in the structured zone [L1]; see also [E1].",
                         usage_metadata={"input_tokens": 120, "output_tokens": 20, "total_tokens": 140})


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = tmp_path / "platform_state.duckdb"
    connect = lambda target, platform: SqlConnection(duckdb.connect(str(db)), "duckdb")  # noqa: E731
    monkeypatch.setattr(agent_memory, "sql_connect", connect)
    monkeypatch.setattr(agent_sessions, "sql_connect", connect)
    agent_memory._READY.clear()
    agent_sessions._READY.clear()
    w = {"utility_prompts": [], "chat_calls": [], "extract": [], "use_tool": True}
    monkeypatch.setattr(agent_brain, "_chat_model", lambda name, thinking_budget=None: ScriptedModel(w))
    monkeypatch.setattr(agent_brain, "_embed", _vec)
    monkeypatch.setattr(agent_memory, "_embed", _vec)
    monkeypatch.setattr(agent_context, "build_context", lambda domain, agent, target, wide: [
        {"id": "L1", "section": "landing", "title": "Landing zone", "ok": True, "text": "structured: claims (2 files)", "chars": 28},
        {"id": "E1", "section": "estate", "title": "Estate scan", "ok": True, "text": "48 tables", "chars": 9}])
    return w


def _search_tool_stub(monkeypatch):
    real = agent_brain._tools

    def tools(domain, agent, target, wide, trace):
        ts = real(domain, agent, target, wide, trace)
        from langchain_core.tools import tool

        @tool
        def search_estate_columns(name_contains: str) -> str:
            """stub"""
            return json.dumps([{"table": "insurance_silver.fact_claim", "column": "claim_id"}])
        return [search_estate_columns if t.name == "search_estate_columns" else t for t in ts]
    monkeypatch.setattr(agent_brain, "_tools", tools)


def test_a_turn_uses_context_tools_and_records_how_it_answered(world, monkeypatch):
    _search_tool_stub(monkeypatch)
    world["extract"].append(json.dumps([
        {"kind": "decision", "subject": "claims cadence", "content": "Claims are reported monthly, not daily.",
         "quote": "we report claims monthly"},
        # the agent's own finding dressed up as a company fact: must not be remembered
        {"kind": "fact", "subject": "estate size", "content": "The estate has 48 tables.", "quote": "48 tables"}]))
    out = agent_brain.ask("insurance", "detective", "When do claims land? Note we report claims monthly.", username="alice")

    assert out["reply"].startswith("Claims land daily")
    t = out["trace"]
    assert t["agent"] == "The Data Detective" and t["model"] == "gemini-2.5-flash"
    assert [c["id"] for c in t["context"]] == ["L1", "E1"]
    assert t["tools"][0]["name"] == "search_estate_columns" and t["tools"][0]["ok"]
    assert t["cited"] == ["E1", "L1"]
    assert t["usage"] == {"input_tokens": 220, "output_tokens": 25}
    assert [m["subject"] for m in t["memories_saved"]] == ["claims cadence"]
    assert t["memories_skipped"] == [{"subject": "estate size", "reason": "not in the person's own words"}]
    assert set(world["bound_tools"]) == {n for n, _ in agent_brain.TOOL_DOCS}

    system = world["chat_calls"][0][0]
    assert isinstance(system, SystemMessage)
    assert "The Data Detective" in system.content and "[L1] Landing zone" in system.content
    assert "you do not write files, change contracts or approve anything" in system.content

    # both messages are stored with the trace on the reply
    msgs = agent_sessions.messages(out["session_id"], "insurance")
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["trace"]["cited"] == ["E1", "L1"]


def test_long_term_memory_is_recalled_next_time_and_not_duplicated(world, monkeypatch):
    world["use_tool"] = False
    world["extract"].append(json.dumps([{"kind": "preference", "subject": "amounts in SGD thousands",
                                         "content": "Show claim amounts in SGD thousands.",
                                         "quote": "always show claim amounts in SGD thousands"}]))
    agent_brain.ask("insurance", "insight", "Please always show claim amounts in SGD thousands.", username="alice")

    # a new conversation, days later: the preference is recalled and put in front of the model
    world["extract"].append(json.dumps([{"kind": "preference", "subject": "amounts in SGD thousands",
                                         "content": "Show claim amounts in SGD thousands.",
                                         "quote": "shown in SGD thousands"}]))
    out = agent_brain.ask("insurance", "insight", "Should claim amounts be shown in SGD thousands?", username="alice")
    assert [m["subject"] for m in out["trace"]["memories_recalled"]] == ["amounts in SGD thousands"]
    assert "[M1] (preference, team) amounts in SGD thousands" in world["chat_calls"][-1][0].content
    assert out["trace"]["memories_saved"] == [] and out["trace"]["memories_skipped"][0]["duplicate_of"] == 1

    # a preference is the team's: another agent recalls it too -- but never another company
    assert [m["scope"] for m in out["trace"]["memories_recalled"]] == ["team"]
    assert agent_memory.recall("insurance", "detective", "SGD thousands", query_vec=_vec("SGD thousands"))[0]["memory_id"] == 1
    assert agent_memory.recall("asset_management", "insight", "SGD thousands", query_vec=_vec("SGD thousands")) == []


def test_corrections_stay_with_the_agent_that_was_corrected(world):
    world["use_tool"] = False
    world["extract"].append(json.dumps([{"kind": "correction", "subject": "premium grain",
                                         "content": "Premiums are per coverage, not per policy.",
                                         "quote": "premiums are per coverage not per policy"}]))
    out = agent_brain.ask("insurance", "quality", "No -- premiums are per coverage, not per policy.", username="alice")
    assert out["trace"]["memories_saved"][0]["scope"] == "agent"
    q = _vec("premium grain premiums per coverage")
    assert agent_memory.recall("insurance", "quality", "premium grain", query_vec=q)
    assert agent_memory.recall("insurance", "detective", "premium grain", query_vec=q) == []


def test_citations_that_point_at_nothing_are_flagged(world, monkeypatch):
    world["use_tool"] = False
    monkeypatch.setattr(ScriptedModel, "invoke", lambda self, msgs: AIMessage(
        content="[]" if isinstance(msgs, str) else "Per the landing zone [L1] and what we agreed [M7 decision], plus [X9]. [tool_code]"))
    out = agent_brain.ask("insurance", "delivery", "what did we agree?", username="alice")
    assert out["trace"]["cited"] == ["L1"]
    assert out["trace"]["citations_unverified"] == ["M7", "X9"]
    assert "[tool_code]" not in out["reply"]


def test_short_term_memory_keeps_a_window_and_summarises_the_rest(world, monkeypatch):
    world["use_tool"] = False
    monkeypatch.setattr(agent_sessions, "WINDOW", 4)
    monkeypatch.setattr(agent_sessions, "SUMMARISE_AFTER", 6)
    sid = None
    for i in range(5):
        out = agent_brain.ask("insurance", "delivery", f"question number {i} about claims", username="alice", session_id=sid)
        sid = out["session_id"]
    s = agent_sessions.get_session(sid, "insurance", "alice")
    assert s["message_count"] == 10
    assert s["summary"] == "Summary: they asked about claims several times."
    # folded once 8 messages were pending (turn 4): seq 1-4 summarised, the rest still verbatim
    assert s["summarized_upto"] == 4
    summary, recent = agent_sessions.window(s)
    assert [m["seq"] for m in recent] == [7, 8, 9, 10]

    # the next turn sees the summary plus only the recent window
    agent_brain.ask("insurance", "delivery", "and now?", username="alice", session_id=sid)
    last = world["chat_calls"][-1]
    assert "Summary: they asked about claims several times." in last[0].content
    assert len(last) == 1 + 4 + 1                         # system + window + new message


def test_conversations_belong_to_one_login_and_one_agent(world):
    world["use_tool"] = False
    out = agent_brain.ask("insurance", "quality", "hello", username="alice")
    with pytest.raises(KeyError):
        agent_brain.ask("insurance", "quality", "hi", username="mallory", session_id=out["session_id"])
    with pytest.raises(KeyError):
        agent_brain.ask("asset_management", "quality", "hi", username="alice", session_id=out["session_id"])
    with pytest.raises(ValueError):
        agent_brain.ask("insurance", "architect", "hi", username="alice", session_id=out["session_id"])
    assert [s["session_id"] for s in agent_sessions.list_sessions("insurance", "quality", "alice")] == [out["session_id"]]
    assert agent_sessions.list_sessions("insurance", "quality", "mallory") == []


def test_context_pack_budget_and_unreadable_sections(monkeypatch):
    monkeypatch.setitem(agent_context._BUILDERS, "tickets", lambda d, t, w: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setitem(agent_context._BUILDERS, "landing", lambda d, t, w: ("Landing zone", "x\n" * 5000))
    monkeypatch.setitem(agent_context._BUILDERS, "sources", lambda d, t, w: ("Sources", "s\n" * 5000))
    monkeypatch.setitem(agent_context._BUILDERS, "estate", lambda d, t, w: ("Estate scan", "e\n" * 5000))
    items = agent_context.build_context("insurance", "nightwatch", budget=3000)
    by = {i["section"]: i for i in items}
    assert [i["section"] for i in items] == ["tickets", "landing", "sources", "estate"]
    assert not by["tickets"]["ok"] and "db down" in by["tickets"]["text"]
    assert by["landing"]["text"].endswith("(truncated to fit the context budget)")
    assert "left out" in by["estate"]["text"]            # budget exhausted: said, not silently dropped
