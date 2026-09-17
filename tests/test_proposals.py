"""Agent proposals: typed, checked at filing, run only on a person's approval, under their name and
their scope, decided once -- and an agent's proposal from a conversation goes through the same path."""
import json

import duckdb
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from emitters import agent_brain, agent_context, agent_memory, agent_sessions, profiler, proposals, source_review
from emitters import intent as intent_mod
from emitters.sql_dialect import SqlConnection


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = tmp_path / "platform_state.duckdb"
    connect = lambda *a, **k: SqlConnection(duckdb.connect(str(db)), "duckdb")  # noqa: E731
    for mod in (proposals, agent_memory, agent_sessions):
        monkeypatch.setattr(mod, "sql_connect", connect)
        mod._READY.clear()
    monkeypatch.setattr(agent_memory, "_embed", lambda text: [1.0, 0.0, float(len(text) % 7)])
    monkeypatch.setattr(intent_mod, "INTENT_DIR", tmp_path / "intent")
    monkeypatch.setattr(profiler, "DISCOVERY_DIR", tmp_path / "discovery")
    monkeypatch.setattr(profiler, "HARNESS_DIR", tmp_path / "harness")
    return tmp_path


def _file(kind, params, **kw):
    return proposals.propose("testco", "duckdb", kw.pop("agent", "delivery"), kind,
                             kw.pop("title", "A change"), kw.pop("rationale", "Because the gap report says so [G1]."),
                             params, evidence=["G1"], **kw)


def test_parameters_are_checked_when_filed(world):
    with pytest.raises(ValueError, match="unknown proposal kind"):
        _file("drop_table", {})
    with pytest.raises(ValueError, match="unexpected parameter"):
        _file("run_estate_scan", {"tiers": 3, "include_unclassified": True})   # scope is never the agent's to set
    with pytest.raises(ValueError, match="'report' is required"):
        _file("add_intent_data_points", {"name": "Claims", "data_points": ["loss_date"]})
    with pytest.raises(ValueError, match="needs a comment"):
        _file("review_source_version", {"source_id": "claims", "version": 1, "decision": "reject"})
    with pytest.raises(ValueError, match="no intent"):
        _file("add_intent_data_points", {"intent_id": "../../contracts/sources/insurance/claims", "report": "R", "data_points": ["x"]})
    with pytest.raises(ValueError, match="no intent 'nope'"):
        _file("add_intent_data_points", {"intent_id": "nope", "report": "R", "data_points": ["x"]})
    with pytest.raises(ValueError, match="has no version 7"):
        _file("review_source_version", {"source_id": "claims", "version": 7, "decision": "accept"})
    with pytest.raises(ValueError, match="scd1 or scd2"):
        _file("update_architecture", {"entity": "dim_policy", "scd_type": "scd9"})
    with pytest.raises(ValueError, match="title and a rationale"):
        _file("recommendation", {"owner": "The Delivery Lead"}, rationale=" ")
    assert proposals.list_proposals("testco") == []


def test_approving_an_intent_proposal_runs_it_under_the_approvers_name(world, monkeypatch):
    intent_mod.capture_intent("testco", "default", {"name": "Claims", "reports": [
        {"name": "Claims by month", "required_data_points": ["claim_id"]}]}, "alice")
    # an intent named instead of id'd resolves to its id when unambiguous
    by_name = _file("add_intent_data_points", {"intent_id": "claims ", "report": "Claims by month", "data_points": ["x"]})
    assert by_name["params"]["intent_id"] == "claims"
    p = _file("add_intent_data_points", {"intent_id": "claims", "report": "Claims by month",
                                         "data_points": ["loss_date", "claim_id"]}, agent="detective")
    assert p["status"] == "proposed"
    assert "Add loss_date to report" in p["preview"]            # claim_id is already there, so not repeated

    done = proposals.decide("testco", p["proposal_id"], "approve", by="carol")
    assert done["status"] == "approved" and done["decided_by"] == "carol"
    assert intent_mod.load_intent("testco", "claims")["reports"][0]["required_data_points"] == ["claim_id", "loss_date"]
    assert proposals.get("testco", p["proposal_id"])["preview"] == p["preview"]   # what was approved, not re-worded after
    from emitters.intent_change import change_view
    import emitters.intent_change as ic
    monkeypatch.setattr(ic, "_estate_leads", lambda *a, **k: {"scan_id": None, "matches": {}, "note": None})
    view = change_view("testco", "claims")
    assert view["meta"]["created_by"] == "carol" and view["meta"]["review"]["status"] == "pending"   # still needs sign-off

    with pytest.raises(ValueError, match="already approved"):
        proposals.decide("testco", p["proposal_id"], "approve", by="carol")


def test_declining_needs_a_reason_and_teaches_the_agent(world):
    p = _file("recommendation", {"owner": "The Superstar Data Engineer", "steps": ["fix agents at source"]}, agent="engineer")
    with pytest.raises(ValueError, match="say why"):
        proposals.decide("testco", p["proposal_id"], "decline", by="carol")
    out = proposals.decide("testco", p["proposal_id"], "decline", by="carol", note="Source system is frozen until Q3.")
    assert out["status"] == "declined" and out["decision_note"] == "Source system is frozen until Q3."
    mem = agent_memory.list_memory("testco", "engineer")
    assert mem[0]["kind"] == "correction" and "frozen until Q3" in mem[0]["content"]
    assert agent_memory.list_memory("testco", "detective") == []      # the correction stays with the proposer


def test_source_review_and_failed_apply(world):
    source_review.upload_source("testco", "claims", "claims.csv", b"claim_id\nC1\nC2\n", by="alice")
    p = _file("review_source_version", {"source_id": "claims", "version": 1, "decision": "accept"})
    assert proposals.decide("testco", p["proposal_id"], "approve", by="carol")["status"] == "approved"
    assert source_review.review_summary("testco", "claims")["review_status"] == "accepted"

    # the thing it pointed at vanished between filing and approval: recorded as failed, never approved
    intent_mod.capture_intent("testco", "default", {"name": "Gone", "reports": []}, "alice")
    q = _file("add_intent_data_points", {"intent_id": "gone", "report": "R", "data_points": ["x"], "new_report": True})
    intent_mod.delete_intent("testco", "gone")
    failed = proposals.decide("testco", q["proposal_id"], "approve", by="carol")
    assert failed["status"] == "failed" and "error" in failed["result"]


def test_scan_scope_comes_from_the_approver(world, monkeypatch):
    calls = []
    from webapp.backend import estate as estate_api
    monkeypatch.setattr(estate_api, "start_scan", lambda d, t, tiers, include_unclassified=False:
                        calls.append((d, t, tiers, include_unclassified)) or {"started": True, "scan_id": "s1"})
    a = _file("run_estate_scan", {"tiers": 3, "target": "databricks"})
    b = _file("run_estate_scan", {"tiers": 3})
    proposals.decide("testco", a["proposal_id"], "approve", by="company-user", approver_is_admin=False)
    proposals.decide("testco", b["proposal_id"], "approve", by="admin", approver_is_admin=True)
    assert calls == [("testco", "databricks", 3, False), ("testco", "duckdb", 3, True)]


class ProposingModel:
    """Calls propose_change once (with whatever params the test set), then answers."""

    def __init__(self, world):
        self.w = world

    def bind_tools(self, tools):
        return self

    def invoke(self, msgs):
        if isinstance(msgs, str):
            return AIMessage(content="[]")
        if not any(isinstance(m, ToolMessage) for m in msgs):
            return AIMessage(content="", tool_calls=[{"name": "propose_change", "id": "p1", "args": self.w["call"]}])
        self.w["tool_result"] = [m.content for m in msgs if isinstance(m, ToolMessage)][-1]
        return AIMessage(content="I've proposed that; it's waiting for your approval under Approvals.")


def test_an_agent_files_proposals_from_a_conversation(world, monkeypatch):
    w = {}
    monkeypatch.setattr(agent_brain, "_chat_model", lambda name, thinking_budget=None: ProposingModel(w))
    monkeypatch.setattr(agent_brain, "_embed", lambda text: [1.0, 0.0, 0.0])
    monkeypatch.setattr(agent_context, "build_context", lambda d, a, t, wide: [
        {"id": "G1", "section": "gaps", "title": "Gap report", "ok": True, "text": "loss_date missing", "chars": 17}])
    intent_mod.capture_intent("testco", "default", {"name": "Claims", "reports": [{"name": "R", "required_data_points": []}]}, "alice")

    w["call"] = {"kind": "add_intent_data_points", "title": "Add loss_date to R",
                 "rationale": "The dashboard needs it [G1].", "params_json": json.dumps({"intent_id": "claims", "report": "R", "data_points": ["loss_date"]}),
                 "evidence": "G1"}
    out = agent_brain.ask("testco", "delivery", "Please add loss_date to the claims report.", username="alice")
    [filed] = out["trace"]["proposals"]
    p = proposals.get("testco", filed["proposal_id"])
    assert p["agent"] == "delivery" and p["session_id"] == out["session_id"] and p["evidence"] == ["G1"]
    assert p["status"] == "proposed"
    assert intent_mod.load_intent("testco", "claims")["reports"][0]["required_data_points"] == []   # nothing ran

    # a malformed proposal is refused back to the model, and nothing is filed
    w["call"] = {"kind": "assign_ticket", "title": "Assign", "rationale": "Because.", "params_json": '{"ticket_id": "six"}'}
    out2 = agent_brain.ask("testco", "delivery", "Assign it.", username="alice", session_id=out["session_id"])
    assert out2["trace"]["proposals"] == [] and w["tool_result"].startswith("Not filed:")
    assert len(proposals.list_proposals("testco")) == 1


def test_report_names_must_match_an_existing_report(world):
    intent_mod.capture_intent("testco", "default", {"name": "Claims", "reports": [
        {"name": "Claims by month", "required_data_points": ["claim_id"]}]}, "alice")
    ok = _file("add_intent_data_points", {"intent_id": "claims", "report": "claims by month report", "data_points": ["paid_amount"]})
    assert ok["params"]["report"] == "Claims by month"            # resolved to the real report
    with pytest.raises(ValueError, match="its reports are"):
        _file("add_intent_data_points", {"intent_id": "claims", "report": "Monthly claims", "data_points": ["x"]})
    new = _file("add_intent_data_points", {"intent_id": "claims", "report": "Monthly claims", "data_points": ["x"], "new_report": True})
    proposals.decide("testco", ok["proposal_id"], "approve", by="carol")
    proposals.decide("testco", new["proposal_id"], "approve", by="carol")
    reports = {r["name"]: r["required_data_points"] for r in intent_mod.load_intent("testco", "claims")["reports"]}
    assert reports == {"Claims by month": ["claim_id", "paid_amount"], "Monthly claims": ["x"]}


def test_past_approvals_are_checked_against_current_records(world):
    intent_mod.capture_intent("testco", "default", {"name": "Claims", "reports": [
        {"name": "Claims by month", "required_data_points": ["claim_id"]}]}, "alice")
    p = _file("add_intent_data_points", {"intent_id": "claims", "report": "Claims by month", "data_points": ["paid_amount"]})
    done = proposals.decide("testco", p["proposal_id"], "approve", by="carol")
    assert proposals.in_effect(done) == (True, "intent claims, report \u201cClaims by month\u201d has them")
    # the intent is re-created without the data point: the approval is history, not current state
    intent_mod.delete_intent("testco", "claims")
    assert proposals.in_effect(done) == (False, "intent claims no longer exists")
    intent_mod.capture_intent("testco", "default", {"name": "Claims", "reports": [
        {"name": "Claims by month", "required_data_points": ["claim_id"]}]}, "alice")
    ok, why = proposals.in_effect(done)
    assert not ok and "does not need paid_amount" in why
    from emitters import agent_context
    title, text = agent_context._proposals("testco", "duckdb", False)
    assert "NOT in effect now: intent claims" in text
