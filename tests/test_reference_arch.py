"""Reference architecture advice, offline: a fixture page stands in for the web, a scripted model
for Gemini and a bag-of-words embedding for the vectors -- so these test what makes advice
trustworthy (extraction, retrieval by cloud, quote verification, unsupported-term and placeholder
checks, the path to a proposal), not the model's prose."""
import hashlib
import json
import math

import duckdb
import pytest

from emitters import agent_memory, proposals, reference_arch as ra
from emitters import intent as intent_mod
from emitters.sql_dialect import SqlConnection

PAGE = """<html><head><title>ignored</title><meta property="og:title" content="What is the medallion lakehouse architecture? - Azure Databricks | Microsoft Learn"></head>
<body><div>Ask Learn Table of contents Sign in</div><main>
<header><h1>What is the medallion lakehouse architecture?</h1></header>
<p>The medallion architecture describes a series of data layers that denote the quality of data stored in the lakehouse.</p>
<h2>Ingest raw data to the bronze layer</h2>
<p>The bronze layer contains raw, unvalidated data. Data ingested in the bronze layer typically maintains the raw state of the data source in its original formats.</p>
<nav><p>Previous page next page navigation that must never be quoted</p></nav>
<h2>Configure high availability and disaster recovery</h2>
<ul><li>Define the Recovery Time Objective (RTO) and Recovery Point Objective (RPO) for your organization before choosing a disaster recovery approach.</li></ul>
<script>var x = "not text";</script>
</main><footer><p>Copyright footer text that is not guidance</p></footer></body></html>"""


def _vec(text):
    v = [0.0] * 64
    for w in (text or "").lower().split():
        v[int(hashlib.md5(w.strip(".,()").encode()).hexdigest(), 16) % 64] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def test_extraction_keeps_the_article_and_its_sections_only():
    title, blocks = ra.extract(PAGE)
    assert title == "What is the medallion lakehouse architecture? - Azure Databricks"
    text = " ".join(t for _, t in blocks)
    assert "Ask Learn" not in text and "navigation" not in text and "Copyright" not in text and "not text" not in text
    headings = [h for h, _ in blocks]
    assert "Ingest raw data to the bronze layer" in headings and "Configure high availability and disaster recovery" in headings
    chunks = ra.chunk(blocks, size=900)
    assert all(h for h, _ in chunks[1:])          # every passage after the intro knows its section


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    db = tmp_path / "platform_state.duckdb"
    connect = lambda *a, **k: SqlConnection(duckdb.connect(str(db)), "duckdb")  # noqa: E731
    for mod in (ra, proposals, agent_memory):
        monkeypatch.setattr(mod, "sql_connect", connect)
        mod._READY.clear()
    monkeypatch.setattr(ra, "REGISTRY", [
        {"source_id": "azure-medallion", "publisher": "Microsoft Learn", "clouds": ["azure"], "url": "https://learn.example/medallion"},
        {"source_id": "aws-only", "publisher": "Databricks", "clouds": ["aws"], "url": "https://docs.example/aws"},
        {"source_id": "broken", "publisher": "Nobody", "clouds": ["azure"], "url": "https://down.example/"},
    ])
    pages = {"https://learn.example/medallion": PAGE,
             "https://docs.example/aws": PAGE.replace("Microsoft Learn", "Databricks").replace("Azure Databricks", "AWS")}

    def fetch(url):
        if url not in pages:
            raise OSError("connection reset")
        return pages[url]
    monkeypatch.setattr(ra, "_fetch", fetch)
    monkeypatch.setattr(ra, "_embed_many", lambda texts: [_vec(t) for t in texts])
    monkeypatch.setattr(ra, "_facts", lambda d, t, w: [
        {"id": "A1", "title": "Architecture", "text": "No architecture record captured yet.", "ok": True},
        {"id": "E1", "title": "Estate scan", "text": "8.0% of policy.agent_id values reference no agent.", "ok": True}])
    monkeypatch.setattr(intent_mod, "INTENT_DIR", tmp_path / "intent")
    return tmp_path


def test_corpus_refresh_records_failures_and_retires_dropped_pages(corpus, monkeypatch):
    r = ra.refresh_corpus()
    assert {f["source_id"] for f in r["fetched"]} == {"azure-medallion", "aws-only"}
    assert r["failed"][0]["source_id"] == "broken" and "connection reset" in r["failed"][0]["error"]
    assert ra.refresh_corpus()["fresh"] == ["azure-medallion", "aws-only"]        # not re-fetched within a week
    assert {p["source_id"] for p in ra._passages("azure")} == {"azure-medallion"}   # an Azure customer never gets the AWS page
    monkeypatch.setattr(ra, "REGISTRY", [s for s in ra.REGISTRY if s["source_id"] != "aws-only"])
    assert ra.refresh_corpus()["retired"] == ["aws-only"]
    assert ra._passages("aws") == []


def _draft(**over):
    base = {"topic": "recovery", "title": "Set recovery objectives", "confidence": "high",
            "recommendation": "Define the Recovery Time Objective and Recovery Point Objective before choosing an approach.",
            "why_here": "No architecture record exists [A1].", "facts": ["A1", "Z9"],
            "citations": [{"ref": "R1", "quote": "Define the Recovery Time Objective (RTO) and Recovery Point Objective (RPO)"}],
            "proposal": {"kind": "update_architecture", "params": {"rto": "4 hours", "rpo": "1 hour"}}}
    base.update(over)
    return base


def test_verification_keeps_only_what_checks_out():
    refs = {"R1": {"text": "Define the Recovery Time Objective (RTO) and Recovery Point Objective (RPO) for your organization before choosing a disaster recovery approach."}}
    drafts = [
        _draft(),
        _draft(title="Invented quote", citations=[{"ref": "R1", "quote": "Always replicate every table to three regions for safety"}]),
        _draft(title="Short quote", citations=[{"ref": "R1", "quote": "Define the"}]),
        _draft(title="Unknown ref", citations=[{"ref": "R7", "quote": "Define the Recovery Time Objective (RTO) and Recovery Point"}]),
        _draft(title="Uses Auto Loader", recommendation="Use Auto Loader with `_rescued_data` to meet the RPO.", proposal=None),
        _draft(title="Placeholder", proposal={"kind": "update_architecture", "params": {"rto": "To be defined by business"}}),
    ]
    kept, rejected, stats = ra.verify(drafts, refs, {"A1"}, facts_text="No architecture record captured yet.")
    assert [k["title"] for k in kept] == ["Set recovery objectives", "Uses Auto Loader", "Placeholder"]
    assert {r["title"] for r in rejected} == {"Invented quote", "Short quote", "Unknown ref"}
    assert kept[0]["facts"] == ["A1"] and stats["facts_dropped"] >= 1                        # Z9 isn't a real fact id
    assert kept[0]["proposal"] == {"kind": "update_architecture", "params": {"rto": "4 hours", "rpo": "1 hour"}}
    assert kept[1]["unsupported_terms"] == ["Auto Loader", "_rescued_data"] and kept[1]["confidence"] == "low"
    assert kept[2]["proposal"]["kind"] is None and "placeholder" in kept[2]["proposal"]["invalid"]


def test_advice_end_to_end_and_into_a_proposal(corpus, monkeypatch):
    ra.refresh_corpus()
    seen = {}

    def chat(prompt):
        seen["prompt"] = prompt
        rid = next(line.split("]")[0][1:] for line in prompt.splitlines()
                   if line.startswith("[R") and "Configure high availability" in line)
        return json.dumps([_draft(citations=[{"ref": rid, "quote": "Define the Recovery Time Objective (RTO) and Recovery Point Objective (RPO)"}])])
    monkeypatch.setattr(ra, "_chat", chat)

    a = ra.advise("testco", "duckdb", "azure", question="How do we meet a 1 hour RPO?", created_by="carol")
    assert "THE PERSON ASKED: How do we meet a 1 hour RPO?" in seen["prompt"]
    assert "[A1] Architecture" in seen["prompt"] and "Microsoft Learn" in seen["prompt"]
    [rec] = a["recommendations"]
    src = a["sources"][rec["citations"][0]["ref"]]
    assert src["url"] == "https://learn.example/medallion" and src["heading"] == "Configure high availability and disaster recovery"
    assert a["stats"]["citations_verified"] == 1 and a["rejected"] == []
    assert ra.list_advice("testco")[0]["advice_id"] == a["advice_id"]
    assert ra.list_advice("otherco") == []

    p = ra.propose_recommendation("testco", a["advice_id"], 0, by="carol")
    assert p["kind"] == "update_architecture" and p["agent"] == "architect" and p["params"] == {"rto": "4 hours", "rpo": "1 hour"}
    assert rec["citations"][0]["ref"] in p["evidence"] and f"advice:{a['advice_id']}" in p["evidence"]
    assert "learn.example/medallion" in p["rationale"]
    with pytest.raises(KeyError):
        ra.propose_recommendation("otherco", a["advice_id"], 0, by="mallory")                     # another company's advice

    with pytest.raises(ValueError, match="cloud must be"):
        ra.advise("testco", "duckdb", "oracle")


def test_reply_parsing_survives_citations_before_the_json():
    raw = ('Based on [E1] and [A1], here is my advice:\n```json\n'
           '[{"title": "Set RTO", "citations": [{"ref": "R1"}]}]\n```\nSee [R1].')
    assert ra.parse_json_array(raw) == [{"title": "Set RTO", "citations": [{"ref": "R1"}]}]
    assert ra.parse_json_array('Advice [E1]: [{"title": "x"}] thanks') == [{"title": "x"}]
    assert ra.parse_json_array("[E1] sorry, I can't") is None


def test_unsupported_names_ignore_verbs_roles_and_acronym_expansions():
    ground = "Define the RTO and RPO for your organization. Centralise data governance with Unity Catalog."
    check = lambda rec, params=None: ra._unsupported_terms({"recommendation": rec, "proposal": {"params": params or {}}}, ground, "")  # noqa: E731
    assert check("Achieve a 1-hour Recovery Point Objective (RPO) for claims.") == []          # RPO is in the source
    assert check("Implement Unity Catalog with the Governance Team.", {"owner": "Data Engineering Team"}) == []
    assert check("Implement Lakeflow Declarative Pipelines.") == ["Lakeflow Declarative Pipelines"]
    assert check("Use Auto Loader with `_rescued_data`.") == ["Auto Loader", "_rescued_data"]
    assert check("An Undefined RTO leaves recovery unplanned.") == []                             # stray capital before a grounded acronym
    assert check("Adopt Lakeflow Pipelines now.") == ["Lakeflow Pipelines"]
    assert ra._unsupported_terms({"recommendation": "Use Streaming Tables in SQL."}, "Continuous incremental ingestion Streaming Table", "") == []
