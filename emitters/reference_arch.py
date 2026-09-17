"""Reference architecture advice, grounded in published guidance and cited passage by passage.

An architecture recommendation that can't say where it came from is an opinion. This module keeps a
small corpus of published reference architectures -- Microsoft Learn for Azure Databricks,
Databricks' own documentation, the AWS analytics lens, Google Cloud's Architecture Center --
fetched from the web, split into sections and embedded. Advice for a company is then built in
four steps:

  facts       the company's own records: architecture, estate scan, gaps, sources, landing zone
              (emitters/agent_context.py builders, with the same citation ids agents use)
  retrieval   for each architecture topic, the passages most relevant to THIS company's facts
              (its RTO/RPO, its sensitive columns, its broken references, its source types...)
  drafting    Gemini writes recommendations; each must quote the passage it rests on
  checking    every quote is matched, mechanically, against the stored passage text, and every
              fact id against the facts pack. A recommendation with no citation that checks out
              is dropped and counted -- never shown as advice

Advice is stored (architecture_advice) so it can be reread and turned into a proposal for the team
(emitters/proposals.py): nothing it says changes the architecture record by itself.

The corpus is public documentation, shared by every tenant. Advice records are per company.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import threading
import uuid
from html.parser import HTMLParser
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters.control_plane import ensure_control_schema  # noqa: E402
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402

ADVICE_MODEL = os.environ.get("FF_ADVICE_MODEL", "google_genai:gemini-2.5-flash")
CHUNK_CHARS = 900
REFRESH_AFTER_DAYS = 7
CLOUDS = {"azure": "Microsoft Azure", "aws": "Amazon Web Services", "gcp": "Google Cloud"}

# Published reference architectures. `clouds` is where the guidance applies as written; advice for
# a company on Azure only cites Azure-specific pages, so an Azure customer is never pointed at an
# AWS console instruction.
REGISTRY: list[dict[str, Any]] = [
    {"source_id": "azure-databricks-modern-analytics", "publisher": "Microsoft Learn", "clouds": ["azure"],
     "url": "https://learn.microsoft.com/en-us/azure/architecture/solution-ideas/articles/azure-databricks-modern-analytics-architecture"},
    {"source_id": "azure-databricks-medallion", "publisher": "Microsoft Learn", "clouds": ["azure"],
     "url": "https://learn.microsoft.com/en-us/azure/databricks/lakehouse/medallion"},
    {"source_id": "azure-databricks-disaster-recovery", "publisher": "Microsoft Learn", "clouds": ["azure"],
     "url": "https://learn.microsoft.com/en-us/azure/databricks/admin/disaster-recovery"},
    {"source_id": "azure-databricks-unity-catalog-practices", "publisher": "Microsoft Learn", "clouds": ["azure"],
     "url": "https://learn.microsoft.com/en-us/azure/databricks/data-governance/unity-catalog/best-practices"},
    {"source_id": "azure-databricks-reliability", "publisher": "Microsoft Learn", "clouds": ["azure"],
     "url": "https://learn.microsoft.com/en-us/azure/databricks/lakehouse-architecture/reliability/best-practices"},
    {"source_id": "azure-databricks-security", "publisher": "Microsoft Learn", "clouds": ["azure"],
     "url": "https://learn.microsoft.com/en-us/azure/databricks/lakehouse-architecture/security-compliance-and-privacy/best-practices"},
    {"source_id": "azure-databricks-cost", "publisher": "Microsoft Learn", "clouds": ["azure"],
     "url": "https://learn.microsoft.com/en-us/azure/databricks/lakehouse-architecture/cost-optimization/best-practices"},
    {"source_id": "databricks-medallion", "publisher": "Databricks", "clouds": ["aws"],
     "url": "https://docs.databricks.com/aws/en/lakehouse/medallion"},
    {"source_id": "databricks-reference-architecture", "publisher": "Databricks", "clouds": ["aws"],
     "url": "https://docs.databricks.com/aws/en/lakehouse-architecture/reference"},
    {"source_id": "databricks-disaster-recovery", "publisher": "Databricks", "clouds": ["aws"],
     "url": "https://docs.databricks.com/aws/en/admin/disaster-recovery"},
    {"source_id": "databricks-unity-catalog-practices", "publisher": "Databricks", "clouds": ["aws"],
     "url": "https://docs.databricks.com/aws/en/data-governance/unity-catalog/best-practices"},
    {"source_id": "databricks-cdc", "publisher": "Databricks", "clouds": ["aws"],
     "url": "https://docs.databricks.com/aws/en/dlt/cdc"},
    {"source_id": "databricks-governance-practices", "publisher": "Databricks", "clouds": ["aws"],
     "url": "https://docs.databricks.com/aws/en/lakehouse-architecture/data-governance/best-practices"},
    {"source_id": "aws-analytics-lens", "publisher": "AWS Well-Architected", "clouds": ["aws"],
     "url": "https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/analytics-lens.html"},
    # (Google's "analytics lakehouse" page was dropped: it now only explains how to delete that
    # Jump Start deployment -- found by reading what was fetched, not by the fetch failing)
    {"source_id": "gcp-dr-scenarios-for-data", "publisher": "Google Cloud Architecture Center", "clouds": ["gcp"],
     "url": "https://cloud.google.com/architecture/dr-scenarios-for-data"},
    {"source_id": "gcp-reliability-pillar", "publisher": "Google Cloud Well-Architected Framework", "clouds": ["gcp"],
     "url": "https://cloud.google.com/architecture/framework/reliability"},
    {"source_id": "gcp-privacy-compliance", "publisher": "Google Cloud Well-Architected Framework", "clouds": ["gcp"],
     "url": "https://cloud.google.com/architecture/framework/security/privacy"},
    {"source_id": "databricks-gcp-unity-catalog-practices", "publisher": "Databricks", "clouds": ["gcp"],
     "url": "https://docs.databricks.com/gcp/en/data-governance/unity-catalog/best-practices"},
    {"source_id": "databricks-gcp-reference-architecture", "publisher": "Databricks", "clouds": ["gcp"],
     "url": "https://docs.databricks.com/gcp/en/lakehouse-architecture/reference"},
    {"source_id": "databricks-gcp-cdc", "publisher": "Databricks", "clouds": ["gcp"],
     "url": "https://docs.databricks.com/gcp/en/dlt/cdc"},
    {"source_id": "databricks-gcp-medallion", "publisher": "Databricks", "clouds": ["gcp"],
     "url": "https://docs.databricks.com/gcp/en/lakehouse/medallion"},
    {"source_id": "databricks-gcp-disaster-recovery", "publisher": "Databricks", "clouds": ["gcp"],
     "url": "https://docs.databricks.com/gcp/en/admin/disaster-recovery"},
]

# The questions every architecture review asks. Each query is sharpened with the company's facts.
TOPICS = [
    ("layering", "medallion bronze silver gold layers raw validated curated data"),
    ("history", "change data capture slowly changing dimension type 2 history tracking merge"),
    ("recovery", "disaster recovery RTO RPO backup replication region failover"),
    ("governance", "governance access control personal data PII masking catalog permissions"),
    ("quality", "data quality expectations constraints validation referential integrity"),
    ("ingestion", "ingestion landing raw files APIs databases incremental load"),
    ("cost", "cost optimization compute sizing storage"),
]

_DDL = [
    """create table if not exists {c}.reference_source (
        source_id varchar primary key, url varchar, title varchar, publisher varchar, clouds varchar,
        fetched_at timestamp, content_hash varchar, chunks integer, status varchar, error varchar)""",
    """create table if not exists {c}.reference_chunk (
        source_id varchar, chunk_no integer, heading varchar, text varchar, embedding varchar)""",
    """create table if not exists {c}.architecture_advice (
        advice_id varchar primary key, domain varchar, target varchar, cloud varchar, question varchar,
        created_by varchar, created_at timestamp, model varchar, facts varchar, sources varchar,
        recommendations varchar, rejected varchar, stats varchar)""",
]
_READY: set[str] = set()
_LOCK = threading.Lock()


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _iso(v: Any) -> Any:
    return v.isoformat() + "Z" if isinstance(v, _dt.datetime) else v


def _con():
    import yaml
    platform = yaml.safe_load((REPO_ROOT / "contracts" / "platform" / "duckdb.yaml").read_text())
    control = resolve_schema(platform, "platform", "control")
    con = sql_connect("duckdb", platform)
    if control not in _READY:
        with _LOCK:
            if control not in _READY:
                ensure_control_schema(con, control)
                for stmt in _DDL:
                    con.execute(stmt.format(c=control))
                _READY.add(control)
    return con, control


# --------------------------------------------------------------------------- fetching

class _Article(HTMLParser):
    """Headings and the text blocks under them, from a documentation page. Starts at the first
    h1: Microsoft Learn puts "Ask Learn", "Table of contents" and sign-in notices before it, and a
    citation that quotes page chrome is worthless."""

    BLOCKS = {"p", "li", "td", "th", "pre", "blockquote", "dd", "dt"}
    # not "header": Databricks docs put the article h1 inside one, and skipping it lost every title
    SKIP = {"script", "style", "nav", "footer", "aside", "button", "svg", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.started = False
        self.skip = 0
        self.heading_level = None
        self.heading_buf: list[str] = []
        self.title = ""
        self.section = ""
        self.buf: list[str] = []
        self.blocks: list[tuple[str, str]] = []   # (section heading, text)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in ("h1", "h2", "h3"):
            self._flush()
            self.heading_level, self.heading_buf = tag, []
            if tag == "h1":
                self.started = True
        elif tag in self.BLOCKS or tag == "br":
            if tag != "br":
                self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == self.heading_level:
            text = " ".join(self.heading_buf).strip()
            if tag == "h1" and not self.title:
                self.title = text
            self.section = text or self.section
            self.heading_level = None
        elif tag in self.BLOCKS:
            self._flush()

    def handle_data(self, data):
        if self.skip or not data.strip():
            return
        if self.heading_level:
            self.heading_buf.append(data.strip())
        elif self.started:
            self.buf.append(data.strip())

    def _flush(self):
        text = re.sub(r"\s+", " ", " ".join(self.buf)).strip()
        self.buf = []
        if self.started and len(text) >= 30:
            self.blocks.append((self.section, text))


def extract(html: str) -> tuple[str, list[tuple[str, str]]]:
    m = re.search(r"<main\b.*?</main>", html, re.S | re.I) or re.search(r"<article\b.*?</article>", html, re.S | re.I)
    p = _Article()
    p.feed(m.group(0) if m else html)
    p._flush()
    # the page's declared title beats its h1 when it has one: Google Cloud's h1 carries button
    # labels ("Delete the Analytics lakehouse Stay organized with collections...")
    og = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html) or \
        re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:title"', html)
    if og:
        import html as _html
        p.title = re.sub(r"\s*[|–-]\s*(Microsoft Learn|Google Cloud|Cloud Architecture Center).*$", "",
                         _html.unescape(og.group(1))).strip() or p.title
    if not p.title:
        t = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        p.title = re.sub(r"\s+", " ", t.group(1)).strip() if t else ""
    return p.title, p.blocks


def chunk(blocks: list[tuple[str, str]], size: int = CHUNK_CHARS) -> list[tuple[str, str]]:
    """Blocks packed into passages of about `size` characters, never across a section heading, so
    each passage can be cited as "this section of this page"."""
    out: list[tuple[str, str]] = []
    cur_heading, cur = None, ""
    for heading, text in blocks:
        if heading != cur_heading or len(cur) + len(text) > size:
            if cur:
                out.append((cur_heading or "", cur.strip()))
            cur_heading, cur = heading, ""
        cur += text + " "
    if cur:
        out.append((cur_heading or "", cur.strip()))
    return out


def _fetch(url: str) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Foundation First reference retrieval)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def _embed_many(texts: list[str]) -> list[list[float]]:
    from emitters.estate import _load_dotenv
    _load_dotenv()
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    emb = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
    out: list[list[float]] = []
    for i in range(0, len(texts), 50):
        out += emb.embed_documents(texts[i:i + 50])
    return out


def _embed_one(text: str) -> list[float]:
    return _embed_many([text])[0]


def refresh_corpus(source_ids: list[str] | None = None, force: bool = False) -> dict[str, Any]:
    """Fetch, extract, chunk and embed each registered page. A page fetched within
    REFRESH_AFTER_DAYS is kept unless forced; a page whose text hasn't changed isn't re-embedded.
    A page that fails is recorded with its error and keeps any passages from its last good fetch."""
    report = {"fetched": [], "unchanged": [], "fresh": [], "failed": [], "retired": []}
    con, c = _con()
    try:
        registered = {s["source_id"] for s in REGISTRY}
        for (sid,) in con.execute(f"select source_id from {c}.reference_source").fetchall():
            if sid not in registered:        # dropped from the registry: its passages can't be cited any more
                con.execute(f"delete from {c}.reference_chunk where source_id = ?", [sid])
                con.execute(f"delete from {c}.reference_source where source_id = ?", [sid])
                report["retired"].append(sid)
        known = {r[0]: (r[1], r[2]) for r in con.execute(
            f"select source_id, fetched_at, content_hash from {c}.reference_source where status = 'ok'").fetchall()}
        for src in REGISTRY:
            sid = src["source_id"]
            if source_ids and sid not in source_ids:
                continue
            if not force and sid in known and known[sid][0] and (_now() - known[sid][0]).days < REFRESH_AFTER_DAYS:
                report["fresh"].append(sid)
                continue
            try:
                title, blocks = extract(_fetch(src["url"]))
                passages = chunk(blocks)
                if len(passages) < 2:
                    raise ValueError(f"only {len(passages)} passage(s) extracted -- page layout not recognised")
                digest = hashlib.sha256(json.dumps(passages).encode()).hexdigest()
                if sid in known and known[sid][1] == digest:
                    con.execute(f"update {c}.reference_source set fetched_at = ?, status = 'ok', error = null where source_id = ?",
                                [_now(), sid])
                    report["unchanged"].append(sid)
                    continue
                vectors = _embed_many([f"{title}\n{h}\n{t}" for h, t in passages])
                con.execute(f"delete from {c}.reference_chunk where source_id = ?", [sid])
                con.executemany(f"insert into {c}.reference_chunk values (?, ?, ?, ?, ?)",
                                [[sid, i, h, t, json.dumps(v)] for i, ((h, t), v) in enumerate(zip(passages, vectors))])
                con.execute(f"delete from {c}.reference_source where source_id = ?", [sid])
                con.execute(f"insert into {c}.reference_source values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [sid, src["url"], title, src["publisher"], ",".join(src["clouds"]), _now(), digest,
                             len(passages), "ok", None])
                report["fetched"].append({"source_id": sid, "title": title, "passages": len(passages)})
            except Exception as exc:  # noqa: BLE001 -- one bad page never blocks the rest
                err = f"{type(exc).__name__}: {str(exc)[:200]}"
                if sid in known:
                    con.execute(f"update {c}.reference_source set error = ? where source_id = ?", [err, sid])
                else:
                    con.execute(f"delete from {c}.reference_source where source_id = ?", [sid])
                    con.execute(f"insert into {c}.reference_source values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                [sid, src["url"], "", src["publisher"], ",".join(src["clouds"]), None, None, 0, "failed", err])
                report["failed"].append({"source_id": sid, "error": err})
    finally:
        con.close()
    return report


def list_sources() -> list[dict[str, Any]]:
    con, c = _con()
    try:
        rows = con.execute(f"select source_id, url, title, publisher, clouds, fetched_at, chunks, status, error "
                           f"from {c}.reference_source order by publisher, title").fetchall()
    finally:
        con.close()
    seen = {r[0] for r in rows}
    registry = {s["source_id"]: s["clouds"] for s in REGISTRY}
    out = [{"source_id": r[0], "url": r[1], "title": r[2], "publisher": r[3], "clouds": registry.get(r[0], []),
            "fetched_at": _iso(r[5]), "passages": r[6], "status": r[7], "error": r[8]} for r in rows]
    out += [{"source_id": s["source_id"], "url": s["url"], "title": "", "publisher": s["publisher"], "clouds": s["clouds"],
             "fetched_at": None, "passages": 0, "status": "not fetched", "error": None} for s in REGISTRY if s["source_id"] not in seen]
    return out


# --------------------------------------------------------------------------- retrieval

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _passages(cloud: str) -> list[dict[str, Any]]:
    con, c = _con()
    try:
        rows = con.execute(
            f"select k.source_id, k.chunk_no, k.heading, k.text, k.embedding, s.title, s.url, s.publisher, s.fetched_at, s.clouds "
            f"from {c}.reference_chunk k join {c}.reference_source s on s.source_id = k.source_id").fetchall()
    finally:
        con.close()
    # which clouds a page applies to is the registry's call, not what was stored when it was fetched:
    # re-tagging a page (the AWS CDC page was once tagged for GCP too) must take effect at once
    clouds = {s["source_id"]: s["clouds"] for s in REGISTRY}
    return [{"source_id": r[0], "chunk_no": r[1], "heading": r[2], "text": r[3], "vec": json.loads(r[4]), "title": r[5],
             "url": r[6], "publisher": r[7], "fetched_at": _iso(r[8])}
            for r in rows if cloud in clouds.get(r[0], [])]


def search(query: str, cloud: str, k: int = 4, pool: list[dict[str, Any]] | None = None,
           query_vec: list[float] | None = None) -> list[dict[str, Any]]:
    pool = pool if pool is not None else _passages(cloud)
    if not pool:
        return []
    qv = query_vec if query_vec is not None else _embed_one(query)
    scored = sorted(pool, key=lambda p: _cosine(qv, p["vec"]), reverse=True)
    out, per_source = [], {}
    for p in scored:
        # at most two passages from one page, so one long page can't crowd out the others
        if per_source.get(p["source_id"], 0) >= 2:
            continue
        per_source[p["source_id"]] = per_source.get(p["source_id"], 0) + 1
        out.append({**p, "score": round(_cosine(qv, p["vec"]), 4)})
        if len(out) == k:
            break
    return out


# --------------------------------------------------------------------------- advice

def _facts(domain: str, target: str, wide: bool) -> list[dict[str, Any]]:
    from emitters import agent_context
    items = []
    for section in ("architecture", "estate", "gaps", "sources", "landing"):
        try:
            title, text = agent_context._BUILDERS[section](domain, target, wide)
            ok = True
        except Exception as exc:  # noqa: BLE001
            title, text, ok = section.title(), f"Unavailable: {type(exc).__name__}", False
        items.append({"id": f"{agent_context._PREFIX[section]}1", "title": title, "text": agent_context._clip(text, 1800), "ok": ok})
    return items


def _topic_query(topic: str, base: str, facts_text: str, question: str) -> str:
    """The topic's standard question, sharpened with what this company actually has."""
    hints = []
    low = facts_text.lower()
    if topic == "recovery":
        hints += re.findall(r"(RTO [^;.\n]+|RPO [^;.\n]+)", facts_text)[:2]
    if topic == "governance" and "sensitive columns" in low:
        hints.append("sensitive personal data columns")
    if topic == "quality" and ("orphans" in low or "broken references" in low or "reference nothing" in low):
        hints.append("foreign keys referencing missing records orphaned rows")
    if topic == "ingestion":
        hints += [k for k in ("api", "database", "unstructured") if f"- {k}:" in low and "nothing landed" not in low][:2]
    if topic == "history" and "scd" in low:
        hints.append("SCD type 2 dimensions")
    return " ".join([base, *hints, question]).strip()


_ADVICE_PROMPT = """You are The Chief Architect on a data platform team. Advise the company whose data domain is "{domain}", running on {cloud_name} with Databricks as the target platform, using ONLY the published passages below.

COMPANY FACTS (from its own records):
{facts}

PUBLISHED REFERENCE PASSAGES:
{passages}
{question_line}
Write 4 to 7 recommendations, most important first. For each:
- ground the recommendation in at least one passage, quoting it word for word (a short exact phrase or sentence of at most 40 words, copied exactly from the passage text);
- say why it matters for THIS company, citing fact ids like A1, E1, G1, S1, L1;
- if the company already does what the guidance says, say so and recommend keeping it;
- never recommend something the passages don't support, and never invent product names, numbers or settings.
If a concrete change to the architecture record follows, include a proposal: kind "update_architecture" with params from {{"rto","rpo","layering_rationale","volume_expectations","entity","scd_type","add_risk","mitigation"}} (entity and scd_type together; scd_type is scd1 or scd2; add_risk with mitigation), otherwise kind "recommendation" with params {{"owner": "an agent or role", "steps": ["..."]}}.

Reply with ONLY a JSON array:
[{{"topic": "layering|history|recovery|governance|quality|ingestion|cost", "title": "short imperative", "recommendation": "2-3 sentences", "why_here": "1-2 sentences", "facts": ["A1"], "citations": [{{"ref": "R3", "quote": "exact words from R3"}}], "confidence": "high|medium|low", "proposal": {{"kind": "...", "params": {{}}}}}}]"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


_TERM = re.compile(r"`([^`]{3,60})`|\b((?:[A-Z][A-Za-z0-9]+)(?:\s+[A-Z][A-Za-z0-9]+)+)\b")
_PLACEHOLDER = re.compile(r"\b(tbd|tbc|to be (defined|decided|confirmed|determined)|n/?a|unknown|placeholder)\b", re.I)


def _named_terms(text: str) -> set[str]:
    """Product, feature and setting names a recommendation relies on: `code` tokens and runs of
    Capitalised Words ("Auto Loader", "Unity Catalog"). The first word of a sentence alone
    doesn't qualify, which is why a run needs two words."""
    out = set()
    for code, words in _TERM.findall(text or ""):
        term = (code or words).strip()
        if term:
            out.add(term)
    return out


# Words that start a sentence or name a role, not a product: "Implement Lakeflow" is the product
# Lakeflow; "Engineering Team" names no product at all. Both were flagged on a real run, and a
# warning on every card is a warning nobody reads.
_LEADING = {"implement", "use", "utilize", "utilise", "configure", "define", "establish", "enable", "adopt", "leverage",
            "consider", "ensure", "create", "set", "apply", "deploy", "build", "standardize", "standardise", "enforce",
            "migrate", "replicate", "monitor", "review", "document", "assign", "introduce", "develop", "automate",
            "achieve", "maintain", "store", "run", "schedule", "centralize", "centralise", "organize", "organise",
            "for", "to", "the", "this", "a", "an", "in", "on", "with", "each", "all", "your", "our", "and", "or"}
_ROLE_ENDINGS = ("team", "teams", "owner", "owners", "group", "manager", "managers", "engineer", "engineers",
                 "lead", "architect", "analyst", "steward", "stewards", "department", "unit")


def _unsupported_terms(r: dict[str, Any], cited_text: str, facts_text: str) -> list[str]:
    """Named things in a recommendation that neither its cited passages nor the company's facts
    mention. A verified quote proves the passage exists, not that everything around it is in it:
    on a real run a recommendation to use "Auto Loader" with its "_rescued_data" column cited a
    passage that only says bronze keeps raw data.

    Checked: the recommendation text and the proposal's concrete parameters (not who owns it).
    Leading verbs are stripped from a capitalised run, role names are ignored, and a spelled-out
    term counts as grounded when its acronym is ("Recovery Point Objective" where the page says RPO)."""
    params = {k: v for k, v in ((r.get("proposal") or {}).get("params") or {}).items() if k != "owner"}
    said = " ".join([str(r.get("recommendation", "")), json.dumps(params)])
    ground_norm = _norm(cited_text + " " + facts_text)
    ground = " " + ground_norm + " "
    tokens = set(ground_norm.split())
    missing = set()
    for term in _named_terms(said):
        words = term.split()
        while len(words) > 1 and words[0].lower() in _LEADING:
            words = words[1:]
        name = " ".join(words)
        if not _norm(name) or words[-1].lower() in _ROLE_ENDINGS or (len(words) == 1 and words[0].lower() in _LEADING):
            continue
        acronym = "".join(w[0] for w in words).lower() if len(words) >= 2 else ""
        if f" {_norm(name)} " in ground or (acronym and acronym in tokens):
            continue
        # a grounded name with a stray capitalised word in front ("Undefined RTO"): any tail of two
        # or more words, or a lone acronym/CamelCase tail, that the sources contain grounds it
        tails = [words[i:] for i in range(1, len(words))]
        if any((len(t) >= 2 or re.fullmatch(r"[A-Z0-9]{2,}|[A-Za-z]+[A-Z][A-Za-z]*", t[0])) and f" {_norm(' '.join(t))} " in ground
               for t in tails):
            continue
        missing.add(name)
    return sorted(missing)


def verify(recs: list[dict[str, Any]], refs: dict[str, dict[str, Any]], fact_ids: set[str],
           facts_text: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Keep what checks out.
      - A citation counts only if its quote is really in the passage it names (at least six
        words, compared ignoring case and punctuation). No verified citation: rejected.
      - Fact ids must exist in the facts pack.
      - Named products or features the cited passages and facts never mention are listed, and
        the recommendation is marked low confidence rather than presented as grounded.
      - A proposal must pass the proposal kind's own validation and carry no placeholder values
        ("to be defined"): approving it would write that placeholder into the record."""
    kept, rejected = [], []
    stats = {"drafted": len(recs), "citations_checked": 0, "citations_verified": 0, "facts_dropped": 0,
             "flagged_unsupported": 0, "proposals_refused": 0}
    from emitters import proposals
    for r in recs:
        if not isinstance(r, dict) or not r.get("title") or not r.get("recommendation"):
            rejected.append({"title": str((r or {}).get("title", "(untitled)")) if isinstance(r, dict) else "(malformed)",
                             "reason": "missing title or recommendation"})
            continue
        good = []
        for cit in r.get("citations") or []:
            stats["citations_checked"] += 1
            ref, quote = str(cit.get("ref", "")).strip(), str(cit.get("quote", ""))
            q = _norm(quote)
            if ref in refs and len(q.split()) >= 6 and q in _norm(refs[ref]["text"]):
                good.append({"ref": ref, "quote": quote.strip()})
                stats["citations_verified"] += 1
        facts = [f for f in (r.get("facts") or []) if f in fact_ids]
        stats["facts_dropped"] += len(r.get("facts") or []) - len(facts)
        if not good:
            rejected.append({"title": r["title"], "reason": "no citation matched its passage word for word"})
            continue
        confidence = r.get("confidence") if r.get("confidence") in ("high", "medium", "low") else "medium"
        # the cited pages' titles and section headings count as what they say, not just the body text
        cited_text = " ".join(" ".join([refs[c["ref"]].get("title", ""), refs[c["ref"]].get("heading", ""),
                                        refs[c["ref"]]["text"]]) for c in good)
        unsupported = _unsupported_terms(r, cited_text, facts_text)
        if unsupported:
            confidence = "low"
            stats["flagged_unsupported"] += 1
        proposal = r.get("proposal") if isinstance(r.get("proposal"), dict) else None
        if proposal:
            try:
                params = proposal.get("params") or {}
                bad = [k for k, v in params.items() if isinstance(v, str) and _PLACEHOLDER.search(v)]
                if bad:
                    raise ValueError(f"placeholder value for {', '.join(bad)}")
                proposal = {"kind": proposal.get("kind"), "params": proposals.validate(proposal.get("kind"), params)}
            except ValueError as exc:
                proposal = {"kind": None, "params": {}, "invalid": str(exc)}
                stats["proposals_refused"] += 1
        kept.append({"topic": r.get("topic") if r.get("topic") in dict(TOPICS) else "other",
                     "title": str(r["title"]).strip(), "recommendation": str(r["recommendation"]).strip(),
                     "why_here": str(r.get("why_here") or "").strip(), "facts": facts, "citations": good,
                     "confidence": confidence, "unsupported_terms": unsupported, "proposal": proposal})
    return kept, rejected, stats


def parse_json_array(raw: str) -> list[Any] | None:
    """The JSON array in a model reply, or None if there isn't a readable one. Tries a fenced
    ```json block first, then decodes from each "[" in turn. The first version took everything
    between the first "[" and the last "]" -- a reply that mentioned "[E1]" before its JSON came
    back unreadable and the advice was silently empty (seen live: drafted 0, parse_error 1)."""
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw or "", re.S)
    candidates = [fence.group(1)] if fence else []
    decoder = json.JSONDecoder()
    for text in candidates + [raw or ""]:
        for m in re.finditer(r"\[", text):
            try:
                value, _end = decoder.raw_decode(text, m.start())
            except ValueError:
                continue
            if isinstance(value, list) and (not value or isinstance(value[0], dict)):
                return value
    return None


def _chat(prompt: str) -> str:
    from emitters.agent_brain import _chat_model, _text
    return _text(_chat_model(ADVICE_MODEL, 1024).invoke(prompt).content)


def advise(domain: str, target: str = "duckdb", cloud: str = "azure", question: str = "",
           created_by: str = "local", include_unclassified: bool = False) -> dict[str, Any]:
    from emitters import versions
    versions.check_id(domain)
    if cloud not in CLOUDS:
        raise ValueError(f"cloud must be one of {', '.join(CLOUDS)}")
    question = (question or "").strip()[:500]
    pool = _passages(cloud)
    if not pool:
        refresh_corpus([s["source_id"] for s in REGISTRY if cloud in s["clouds"]])
        pool = _passages(cloud)
    if not pool:
        raise ValueError(f"no reference passages for {CLOUDS[cloud]} could be fetched -- see the sources list for errors")

    facts = _facts(domain, target, include_unclassified)
    facts_text = "\n\n".join(f"[{f['id']}] {f['title']}\n{f['text']}" for f in facts)

    chosen: dict[tuple[str, int], dict[str, Any]] = {}
    queries = [(t, _topic_query(t, base, facts_text, "")) for t, base in TOPICS]
    if question:
        queries.insert(0, ("question", question))
    vectors = _embed_many([q for _, q in queries])
    for (topic, _q), qv in zip(queries, vectors):
        for p in search("", cloud, k=3 if topic != "question" else 4, pool=pool, query_vec=qv):
            chosen.setdefault((p["source_id"], p["chunk_no"]), {**p, "topics": []})["topics"].append(topic)
    refs = {f"R{i + 1}": p for i, p in enumerate(chosen.values())}
    passages_text = "\n\n".join(f"[{rid}] {p['title']} -- {p['heading']} ({p['publisher']})\n{p['text']}" for rid, p in refs.items())

    prompt = _ADVICE_PROMPT.format(domain=domain, cloud_name=CLOUDS[cloud], facts=facts_text, passages=passages_text,
                                   question_line=f"\nTHE PERSON ASKED: {question}\nAnswer that first.\n" if question else "")
    drafted, attempts = None, 0
    while drafted is None and attempts < 2:      # one retry: an unreadable reply is a model hiccup, not an answer
        attempts += 1
        drafted = parse_json_array(_chat(prompt))
    kept, rejected, stats = verify(drafted or [], refs, {f["id"] for f in facts}, facts_text)
    stats["model_calls"] = attempts
    if drafted is None:
        stats["parse_error"] = 1

    cited = {c["ref"] for r in kept for c in r["citations"]}
    sources = {rid: {k: p[k] for k in ("source_id", "title", "url", "publisher", "heading", "text", "fetched_at", "topics", "score")}
               for rid, p in refs.items() if rid in cited}
    advice = {"advice_id": uuid.uuid4().hex[:12], "domain": domain, "target": target, "cloud": cloud, "question": question,
              "created_by": created_by, "created_at": _iso(_now()), "model": ADVICE_MODEL.split(":", 1)[-1],
              "facts": [{k: f[k] for k in ("id", "title", "ok")} for f in facts], "sources": sources,
              "recommendations": kept, "rejected": rejected,
              "stats": {**stats, "passages_considered": len(refs)}}
    con, c = _con()
    try:
        con.execute(f"insert into {c}.architecture_advice values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [advice["advice_id"], domain, target, cloud, question, created_by, _now(), advice["model"],
                     json.dumps(advice["facts"]), json.dumps(sources), json.dumps(kept), json.dumps(rejected),
                     json.dumps(advice["stats"])])
    finally:
        con.close()
    return advice


_ADVICE_COLS = ["advice_id", "domain", "target", "cloud", "question", "created_by", "created_at", "model", "facts",
                "sources", "recommendations", "rejected", "stats"]


def _advice_row(r) -> dict[str, Any]:
    d = dict(zip(_ADVICE_COLS, r))
    for k in ("facts", "sources", "recommendations", "rejected", "stats"):
        d[k] = json.loads(d[k]) if d[k] else ([] if k != "sources" and k != "stats" else {})
    d["created_at"] = _iso(d["created_at"])
    return d


def list_advice(domain: str, limit: int = 10) -> list[dict[str, Any]]:
    con, c = _con()
    try:
        rows = con.execute(f"select {', '.join(_ADVICE_COLS)} from {c}.architecture_advice where domain = ? "
                           f"order by created_at desc limit {int(limit)}", [domain]).fetchall()
    finally:
        con.close()
    return [_advice_row(r) for r in rows]


def get_advice(domain: str, advice_id: str) -> dict[str, Any]:
    con, c = _con()
    try:
        r = con.execute(f"select {', '.join(_ADVICE_COLS)} from {c}.architecture_advice where domain = ? and advice_id = ?",
                        [domain, advice_id]).fetchone()
    finally:
        con.close()
    if r is None:
        raise KeyError(f"no advice {advice_id!r} for {domain}")
    return _advice_row(r)


def propose_recommendation(domain: str, advice_id: str, index: int, by: str) -> dict[str, Any]:
    """Turn one recommendation into a proposal for the team, carrying its citations as evidence."""
    from emitters import proposals
    a = get_advice(domain, advice_id)
    if not 0 <= index < len(a["recommendations"]):
        raise ValueError("no such recommendation")
    r = a["recommendations"][index]
    p = r.get("proposal") or {}
    kind = p.get("kind") or "recommendation"
    params = p.get("params") if p.get("kind") else {"owner": "The Chief Architect", "steps": [r["recommendation"]]}
    srcs = "; ".join(f"{a['sources'][c['ref']]['title']} ({a['sources'][c['ref']]['url']})" for c in r["citations"] if c["ref"] in a["sources"])
    rationale = f"{r['why_here']} Published guidance: “{r['citations'][0]['quote']}” -- {srcs}".strip()
    return proposals.propose(domain, a["target"], "architect", kind, r["title"], rationale, params,
                             evidence=[c["ref"] for c in r["citations"]] + r["facts"] + [f"advice:{advice_id}"])
