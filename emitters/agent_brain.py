"""The agents' brain: one conversational turn with a named agent, reasoning with Gemini over
three layers of what it knows, with every step recorded so a human can see how it answered.

    short-term memory   this conversation: the last few turns verbatim, older ones summarised
                        (emitters/agent_sessions.py)
    long-term memory    what this agent has learned about this company in earlier conversations,
                        recalled by meaning (emitters/agent_memory.py)
    context             what the platform's own records say right now -- estate, gaps, sources,
                        landing zone, intents, architecture, tickets (emitters/agent_context.py)
    tools               read-only lookups the model can call when the context isn't enough

A turn: rebuild context -> recall memories -> call the model (letting it call tools, a few rounds
at most) -> store both messages -> ask the model which durable memories, if any, the exchange
produced, and keep the ones that aren't already known -> fold old turns into the summary once
the conversation is long. Every one of those steps lands in the trace returned with the reply.

An agent cannot change anything directly. Its lookups read; the one tool that leads to a change,
propose_change, files a typed proposal (emitters/proposals.py) that does nothing until a person
approves it -- and then runs through the same functions the Control Room's buttons call, under
that person's name.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emitters import agent_context, agent_memory, agent_sessions  # noqa: E402

DEFAULT_MODEL = os.environ.get("FF_AGENT_MODEL", "google_genai:gemini-2.5-flash")
UTILITY_MODEL = os.environ.get("FF_UTILITY_MODEL", "google_genai:gemini-2.5-flash")
# Gemini 2.5 "thinks" before answering unless told not to. Measured on this platform: a memory
# extraction call spent 494 of 569 output tokens thinking (3.9s); with no thinking budget it used
# 74 (3.0s) for the same answer. Conversation keeps a modest budget -- tool choice benefits.
AGENT_THINKING_BUDGET = int(os.environ.get("FF_AGENT_THINKING", "512"))
UTILITY_THINKING_BUDGET = 0
MAX_TOOL_ROUNDS = 4
RECALL_K = 5
# calibrated on real gemini-embedding-001 vectors: related questions scored 0.62-0.85 against
# their memory, unrelated ones 0.41-0.58 ("what is the weather" vs a currency preference: 0.563)
RECALL_MIN_SCORE = 0.6
DUPLICATE_SCORE = 0.9

# slug -> blueprint agent number (agents/gates.py AGENTS), plus what this agent is for in a
# conversation. The persona names and role titles themselves come from gates.py, the one registry.
AGENT_SLUGS = {
    "detective": (1, "Finds and explains what is in the company's data: sources, the landing zone, "
                     "the estate scan, and which required data points exist where."),
    "architect": (2, "Advises on target architecture: layering, recovery objectives, history strategy, "
                     "conflicting copies and how personal data is controlled, quoting the published reference "
                     "architectures it relies on."),
    "delivery": (3, "Keeps the intents, gaps and sign-offs moving: what is blocked, what needs a decision, "
                    "and who owns the next step."),
    "engineer": (4, "Explains ingestion and pipelines: what landed, what loaded, what was quarantined and why, "
                    "and how broken references should be handled."),
    "quality": (5, "Explains data quality: empty values, broken references, what the tests check, "
                   "and whether a fix really held."),
    "insight": (6, "Helps shape dashboards and reports from what the intents need and what the data can support."),
    "nightwatch": (7, "Watches operations: incident tickets, recent failures, and what needs attention first."),
}


def agents() -> list[dict[str, Any]]:
    from agents.gates import AGENTS
    out = []
    for slug, (num, purpose) in AGENT_SLUGS.items():
        a = AGENTS[num]
        out.append({"slug": slug, "num": num, "persona": a["persona"], "role": a["role"], "colour": a["colour"],
                    "purpose": purpose, "model": DEFAULT_MODEL.split(":", 1)[-1],
                    "context_sections": agent_context.AGENT_SECTIONS[slug], "tools": [t for t, _ in TOOL_DOCS]})
    return out


def _agent(slug: str) -> dict[str, Any]:
    for a in agents():
        if a["slug"] == slug:
            return a
    raise KeyError(f"no agent {slug!r}")


def _charter(num: int) -> str:
    from agents.gates import AGENTS
    parts = []
    for d in AGENTS[num].get("defs") or []:
        p = REPO_ROOT / ".claude" / "agents" / f"{d}.md"
        if p.exists():
            body = re.sub(r"^---.*?---\s*", "", p.read_text(encoding="utf-8"), flags=re.S)
            # the charter's deliverables (requirements.md, FR-001...) stay out: left in, a real Gemini
            # conversation started drafting numbered requirements in markdown and saying it would
            # "capture" them -- which it cannot do from a chat
            body = re.sub(r"^## (Your job|Output|Outputs|Deliverables?)\b.*?(?=^## |\Z)", "", body, flags=re.S | re.M | re.I)
            parts.append(body.strip())
    return "\n\n".join(parts)[:2400]


# --------------------------------------------------------------------------- model access
# One seam for every model call, so tests (and a future provider switch) replace it in one place.

def _chat_model(name: str, thinking_budget: int | None = None):
    from emitters.estate import _load_dotenv
    _load_dotenv()
    from langchain.chat_models import init_chat_model
    kwargs = {"thinking_budget": thinking_budget} if thinking_budget is not None else {}
    return init_chat_model(name, **kwargs)


def _embed(text: str) -> list[float]:
    from emitters.estate import _load_dotenv
    _load_dotenv()
    return agent_memory._embed(text)


def _text(content: Any) -> str:
    """Gemini may return a list of content parts rather than a string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


# --------------------------------------------------------------------------- tools

TOOL_DOCS = [
    ("get_gap_report", "Gaps between what the intents need and what the estate holds, optionally one gap type."),
    ("get_estate_findings", "One section of the latest estate report: open_questions, relationships, sensitivity, quality, mirrors or entities."),
    ("search_estate_columns", "Find columns in the latest estate scan whose name contains some text."),
    ("get_source_profile", "The live discovery profile of one source: columns, types, empty values, keys, review state."),
    ("recall_memories", "Search this agent's long-term memory about the company for something specific."),
    ("search_reference_architectures", "Search published reference architectures (Microsoft Learn, Databricks, AWS, Google Cloud) for passages to cite."),
    ("propose_change", "File a change for a person to approve: add data points to an intent, update the architecture, run a scan, accept or reject a source version, assign a ticket, or a recommendation."),
]


def _tools(domain: str, agent: str, target: str, wide: bool, trace: dict[str, Any]):
    from langchain_core.tools import tool

    @tool
    def get_gap_report(gap_type: str = "") -> str:
        """Gaps between what the company's intents need and what its estate holds. gap_type may be
        one of: missing, not_onboarded, unreviewed, quality, integrity, conflicting_copies,
        sensitive_uncontrolled, definition_conflict -- or empty for all."""
        from emitters.gap_report import build_gap_report
        r = build_gap_report(domain, target, include_unclassified=wide)
        rows = [{"data_point": x["data_point"], "intent": x["intent"], "found_in": x["found_in"],
                 "gaps": [{k: g[k] for k in ("type", "severity", "owner", "action")} for g in x["gaps"]
                          if not gap_type or g["type"] == gap_type]}
                for x in r.get("data_points", [])]
        rows = [x for x in rows if x["gaps"] or not gap_type]
        return json.dumps({"summary": r.get("summary"), "note": r.get("note"), "data_points": rows[:40]}, default=str)

    @tool
    def get_estate_findings(section: str) -> str:
        """One section of the latest estate report for this company. section is one of:
        open_questions, relationships, sensitivity, quality, mirrors, entities, coverage."""
        from emitters.estate import build_report
        r = build_report(domain, target, include_unclassified=wide)
        if not r.get("scan"):
            return "No estate scan yet."
        if section not in r:
            return f"Unknown section {section!r}."
        return json.dumps(r[section], default=str)[:6000]

    @tool
    def search_estate_columns(name_contains: str) -> str:
        """Columns in the latest estate scan whose name contains this text (case-insensitive), with
        their table, type, empty-value percentage and sensitivity."""
        from emitters import estate
        con, control = estate._con(target, domain)
        try:
            scope_sql, params = estate._scope_filter(wide)
            row = con.execute(f"select scan_id from {control}.estate_scan where domain = ? and target = ? "
                              f"and status = 'completed'{scope_sql} order by started_at desc limit 1",
                              [domain, target, *params]).fetchone()
            if row is None:
                return "No estate scan yet."
            hits = con.execute(
                f"select schema_name, table_name, column_name, data_type, null_pct, sensitivity from {control}.estate_column "
                f"where scan_id = ? and lower(column_name) like ? order by schema_name, table_name limit 40",
                [row[0], f"%{name_contains.lower()}%"]).fetchall()
        finally:
            con.close()
        return json.dumps([{"table": f"{h[0]}.{h[1]}", "column": h[2], "type": h[3], "null_pct": h[4],
                            "sensitivity": h[5]} for h in hits]) if hits else f"No column containing {name_contains!r}."

    @tool
    def get_source_profile(source_id: str) -> str:
        """The live discovery profile of one source: connection, columns with type, empty
        percentage and sample values, candidate keys, and its review state."""
        from emitters import source_review, versions
        versions.check_id(source_id)
        p = source_review._root(domain) / f"{source_id}.profile.json"
        if not p.exists():
            return f"No profiled source {source_id!r}."
        prof = json.loads(p.read_text())
        prof.pop("preview", None)
        return json.dumps({**prof, "review": source_review.review_summary(domain, source_id)}, default=str)[:6000]

    @tool
    def recall_memories(query: str) -> str:
        """Search this agent's long-term memory of the company for something specific."""
        hits = agent_memory.recall(domain, agent, query, k=5, query_vec=_embed(query))
        return json.dumps([{k: h[k] for k in ("memory_id", "kind", "subject", "content", "score")} for h in hits], default=str) \
            if hits else "Nothing remembered about that."

    @tool
    def propose_change(kind: str, title: str, rationale: str, params_json: str = "{}", evidence: str = "") -> str:
        """File a proposed change for a person to approve. Nothing happens until they approve it.
        kind and its params (params_json is a JSON object):
          add_intent_data_points: {"intent_id": existing id OR "name": new intent name, "report": str, "data_points": [str], "description"?: str}
          update_architecture: any of {"rto","rpo","platform_binding","layering_rationale","volume_expectations"} and/or {"entity","scd_type"} and/or {"add_risk","mitigation"}
          run_estate_scan: {"target"?: "duckdb"|"databricks", "tiers"?: 1-4}
          review_source_version: {"source_id": str, "version": int, "decision": "accept"|"reject", "comment": str (required to reject)}
          assign_ticket: {"ticket_id": int, "assigned_to": str}
          recommendation: {"owner"?: agent or role, "steps"?: [str]} -- for anything else; approving only records agreement
        title: a short imperative, e.g. "Add loss_date to Claims by month".
        rationale: one or two sentences on why, grounded in the context.
        evidence: comma-separated ids you are relying on, e.g. "G1,E1,M3"."""
        from emitters import proposals
        try:
            params = json.loads(params_json or "{}")
            if not isinstance(params, dict):
                raise ValueError("params_json must be a JSON object")
            p = proposals.propose(domain, target, agent, kind, title, rationale, params,
                                  evidence=[e.strip() for e in evidence.split(",") if e.strip()],
                                  session_id=trace.get("session_id"))
        except (ValueError, json.JSONDecodeError) as exc:
            return f"Not filed: {exc}. Fix the parameters and try again, or explain in words instead."
        trace.setdefault("proposals", []).append({"proposal_id": p["proposal_id"], "kind": p["kind"],
                                                  "title": p["title"], "preview": p["preview"]})
        return json.dumps({"filed": True, "proposal_id": p["proposal_id"], "will_do": p["preview"],
                           "status": "waiting for a person to approve"})

    @tool
    def search_reference_architectures(query: str, cloud: str = "") -> str:
        """Passages from published reference architectures -- Microsoft Learn (Azure Databricks),
        Databricks documentation, AWS Well-Architected, Google Cloud Architecture Center -- most
        relevant to the query. cloud: azure, aws or gcp (empty searches all). Each passage has a
        ref (R1, R2...) to cite, with its page title, section and URL. Quote passages exactly and
        name the page when you rely on them; say plainly if nothing relevant comes back."""
        from emitters import reference_arch
        clouds = [cloud] if cloud in reference_arch.CLOUDS else list(reference_arch.CLOUDS)
        seen, pool = set(), []
        for c in clouds:
            for p in reference_arch._passages(c):
                if (p["source_id"], p["chunk_no"]) not in seen:
                    seen.add((p["source_id"], p["chunk_no"]))
                    pool.append(p)
        if not pool:
            return "No reference passages are available yet -- an admin needs to refresh the reference sources."
        hits = reference_arch.search(query, "", k=4, pool=pool, query_vec=_embed(query))
        refs = trace.setdefault("reference_passages", [])
        out = []
        for h in hits:
            rid = f"R{len(refs) + 1}"
            refs.append({"ref": rid, "title": h["title"], "heading": h["heading"], "url": h["url"], "publisher": h["publisher"]})
            out.append({"ref": rid, "title": h["title"], "section": h["heading"], "publisher": h["publisher"],
                        "url": h["url"], "text": h["text"]})
        return json.dumps(out)

    return [get_gap_report, get_estate_findings, search_estate_columns, get_source_profile, recall_memories,
            search_reference_architectures, propose_change]


# --------------------------------------------------------------------------- prompts

def _system_prompt(a: dict[str, Any], domain: str, summary: str | None, memories: list[dict[str, Any]],
                   context_items: list[dict[str, Any]]) -> str:
    mem = "\n".join(f"- [M{m['memory_id']}] ({m['kind']}, {m.get('scope', 'agent')}) {m['subject']}: {m['content']}" for m in memories) \
        or "(nothing relevant remembered yet)"
    # today's date is stated: without it a real run called a scan finished that morning "from the future"
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""You are {a['persona']}, the {a['role']} on the Foundation First data platform team, talking with someone from the company whose data domain is "{domain}". It is now {today}.
What you're for: {a['purpose']}

Your working principles, from your role charter. In this conversation you advise and look things up; you do not write files, change contracts or approve anything:
{_charter(a['num'])}

How to answer:
- Ground every factual claim in the CONTEXT below or a tool result, and cite it with its id in square brackets exactly as given: [E1], [G1], [S1], or a memory as [M12]. Only cite ids that appear below or in a tool result. Never invent a table, column, number or decision.
- If the context and your tools don't hold the answer, say so plainly and say what would find it out.
- Use the tools when the context summary isn't detailed enough.
- MEMORY is what you learned in earlier conversations. When it disagrees with the CONTEXT, trust the context and point out the difference.
- If something is another agent's job, say which agent.
- When the person tells you a fact, a decision, a preference or a correction, acknowledge it and say you'll remember it -- you will.
- You can't change anything yourself. When something should change -- and the person asks for it, or it clearly follows from what you found -- file it with propose_change, then tell them what you proposed and that it's waiting for their approval under Approvals. Propose one concrete change at a time, only with parameters you can ground in the context; never claim a change has been made. Past proposals are history: before saying something is already done, check the current records show it.
- If a tool refuses your parameters, correct them from the context and try once more without commentary. Never narrate your reasoning, retries or tool errors to the person; tell them the outcome in a sentence or two.
- Be concise and conversational: plain text, short paragraphs, or a short list with "- ". No markdown bold or headings, no numbered requirement IDs.

CONVERSATION SO FAR (summary of earlier turns): {summary or '(this is the start, or everything is still in the recent messages)'}

MEMORY (long-term, about this company -- "team" memories were learned by any agent on the team):
{mem}

CONTEXT (rebuilt from the platform's records just now):
{agent_context.render(context_items)}"""


_EXTRACT_PROMPT = """You maintain the long-term memory of {persona} for one company.
From the exchange below, pick out at most 3 things worth remembering in FUTURE conversations. Only things THE PERSON said:
- fact: something true about the company that the person stated
- decision: something the person decided or agreed
- preference: how the person or company wants things done
- correction: where the person told the agent it was wrong, and what is right instead
Never remember what the agent said, looked up or found in the platform's records (scan results, gaps, counts) -- those are re-read fresh every time and would go stale in memory. A request for the agent to do, check or propose something is NOT a memory -- proposals record those. Ignore greetings, questions, requests and anything uncertain. If the person only asked or requested, reply []. "content" must restate what the PERSON said, never the agent's reply.
Already remembered (don't repeat these):
{known}

Exchange:
PERSON: {user}
AGENT: {reply}

Reply with ONLY a JSON array, possibly empty: [{{"kind": "fact|decision|preference|correction", "subject": "a few words", "content": "one sentence", "quote": "the exact words from PERSON this comes from"}}]"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


_STOP = {"the", "and", "that", "this", "with", "from", "have", "been", "will", "would", "should", "their", "there",
         "they", "them", "what", "when", "which", "about", "into", "only", "also", "than", "then", "were", "does",
         "person", "company", "team"}


def _words(text: str) -> set[str]:
    # 5-letter prefixes, so "report" and "reported" count as the same word
    return {w[:5] for w in _norm(text).split() if len(w) > 3 and w not in _STOP}


def _grounded(quote: str, user_message: str, content: str = "") -> bool:
    """A memory is kept only if it is the person's words, on two counts, both caught on real runs:
      - the quote must appear in their message: the model remembered "no estate scan has been run",
        the agent's own finding restated as a company fact;
      - the content must mostly be made of their words too: the person said "the scan looks old",
        the agent disagreed, and the model stored the AGENT's claim as a "correction" -- with a
        genuine quote from the person attached."""
    q = _norm(quote)
    if len(q) < 12 or q not in _norm(user_message):
        return False
    content_words = _words(content)
    if not content_words:
        return True
    return len(content_words & _words(user_message)) / len(content_words) >= 0.5

_SUMMARY_PROMPT = """Update the running summary of a conversation between a person and {persona}.
Keep what matters for continuing it: questions asked, answers given, decisions, open threads. At most 120 words.
Existing summary: {summary}
Newer messages to fold in:
{messages}
Reply with only the updated summary."""


def _parse_json_list(raw: str) -> list[dict[str, Any]]:
    raw = raw.strip()
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return []
    return [d for d in data if isinstance(d, dict)]


# --------------------------------------------------------------------------- the turn

def ask(domain: str, agent: str, message: str, username: str, session_id: str | None = None,
        target: str = "duckdb", include_unclassified: bool = False) -> dict[str, Any]:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    message = (message or "").strip()
    if not message:
        raise ValueError("Say something to the agent first.")
    a = _agent(agent)
    session = agent_sessions.get_session(session_id, domain, username) if session_id \
        else agent_sessions.create_session(domain, agent, username)
    if session["agent"] != agent:
        raise ValueError("That conversation belongs to a different agent.")

    trace: dict[str, Any] = {"agent": a["persona"], "model": DEFAULT_MODEL.split(":", 1)[-1], "timings_ms": {},
                             "tools": [], "memories_saved": [], "memories_skipped": [], "errors": [],
                             "proposals": [], "session_id": session["session_id"]}
    t0 = time.perf_counter()

    def lap(name: str, start: float) -> None:
        trace["timings_ms"][name] = round((time.perf_counter() - start) * 1000)

    # short-term memory
    s = time.perf_counter()
    summary, recent = agent_sessions.window(session)
    trace["short_term"] = {"recent_messages": len(recent), "summary_used": bool(summary),
                           "summarized_upto": session.get("summarized_upto") or 0}
    lap("short_term", s)

    # context layer
    s = time.perf_counter()
    items = agent_context.build_context(domain, agent, target, include_unclassified)
    trace["context"] = [{"id": i["id"], "title": i["title"], "ok": i["ok"], "chars": i["chars"]} for i in items]
    lap("context", s)

    # long-term memory
    s = time.perf_counter()
    query_vec = None
    memories: list[dict[str, Any]] = []
    try:
        query_vec = _embed(message)
        memories = agent_memory.recall(domain, agent, message, k=RECALL_K, query_vec=query_vec, min_score=RECALL_MIN_SCORE)
    except Exception as exc:  # noqa: BLE001 -- remembering is an enhancement; its failure is shown, not fatal
        trace["errors"].append(f"memory recall: {type(exc).__name__}: {str(exc)[:160]}")
    trace["memories_recalled"] = [{"memory_id": m["memory_id"], "kind": m["kind"], "subject": m["subject"], "scope": m.get("scope"),
                                   "score": m["score"]} for m in memories]
    lap("recall", s)

    # reasoning, with tools
    s = time.perf_counter()
    msgs: list[Any] = [SystemMessage(content=_system_prompt(a, domain, summary, memories, items))]
    for m in recent:
        msgs.append(HumanMessage(content=m["content"]) if m["role"] == "user" else AIMessage(content=m["content"]))
    msgs.append(HumanMessage(content=message))
    tools = _tools(domain, agent, target, include_unclassified, trace)
    by_name = {t.name: t for t in tools}
    model = _chat_model(DEFAULT_MODEL, AGENT_THINKING_BUDGET).bind_tools(tools)
    usage = {"input_tokens": 0, "output_tokens": 0}
    tool_text: list[str] = []
    reply = None
    for round_no in range(MAX_TOOL_ROUNDS + 1):
        resp = model.invoke(msgs)
        for k in usage:
            usage[k] += int((getattr(resp, "usage_metadata", None) or {}).get(k, 0) or 0)
        calls = getattr(resp, "tool_calls", None) or []
        if not calls or round_no == MAX_TOOL_ROUNDS:
            reply = _text(resp.content).strip()
            break
        msgs.append(resp)
        for call in calls:
            ts = time.perf_counter()
            fn = by_name.get(call["name"])
            try:
                result = fn.invoke(call.get("args") or {}) if fn else f"No tool named {call['name']!r}."
                ok = fn is not None
            except Exception as exc:  # noqa: BLE001 -- the model is told the tool failed, and so is the human
                result, ok = f"Tool failed: {type(exc).__name__}: {str(exc)[:200]}", False
            trace["tools"].append({"name": call["name"], "args": call.get("args") or {}, "ok": ok,
                                   "result_chars": len(str(result)), "ms": round((time.perf_counter() - ts) * 1000)})
            tool_text.append(str(result))
            msgs.append(ToolMessage(content=str(result), tool_call_id=call.get("id") or call["name"]))
    # Gemini occasionally leaks a "[tool_code]" marker into the prose; it means nothing to a reader
    reply = re.sub(r"\s*\[tool_code\]\s*", " ", reply or "").strip()
    if not reply:
        reply = "I couldn't put an answer together that time. Try asking again, or more narrowly."
    trace["usage"] = usage
    # every id inside brackets, including several in one: [M4, P1]
    cited = sorted({i for group in re.findall(r"\[([A-Z]\d{1,6}(?:\s*[,;]\s*[A-Z]\d{1,6})*)[^\]]*\]", reply)
                    for i in re.findall(r"[A-Z]\d{1,6}", group)})
    # A citation is only worth something if it points at what the agent was actually given. On a
    # real run an agent cited [M1] while saying it remembered nothing -- so every id is checked
    # against the context pack, the recalled memories and the tool results, and any that match
    # none of them are shown to the person as unverified rather than trusted.
    known = {i["id"] for i in items} | {f"M{m['memory_id']}" for m in memories}
    joined = "\n".join(tool_text)
    trace["cited"] = [c for c in cited if c in known or re.search(rf'"memory_id": {c[1:]}\b', joined)
                      or f'"ref": "{c}"' in joined]
    trace["citations_unverified"] = [c for c in cited if c not in trace["cited"]]
    lap("reasoning", s)

    agent_sessions.append(session, "user", message)

    # long-term memory: what did this exchange teach?
    s = time.perf_counter()
    try:
        known = "\n".join(f"- {m['subject']}: {m['content']}" for m in memories) or "(nothing)"
        raw = _text(_chat_model(UTILITY_MODEL, UTILITY_THINKING_BUDGET).invoke(_EXTRACT_PROMPT.format(
            persona=a["persona"], known=known, user=message, reply=reply)).content)
        for cand in _parse_json_list(raw)[:3]:
            kind = str(cand.get("kind", "")).lower()
            subject, content = str(cand.get("subject", "")).strip(), str(cand.get("content", "")).strip()
            if kind not in agent_memory.KINDS or not subject or not content:
                continue
            if not _grounded(str(cand.get("quote", "")), message, content):
                trace["memories_skipped"].append({"subject": subject, "reason": "not in the person's own words"})
                continue
            vec = _embed(f"{subject}\n{content}")
            dup = agent_memory.recall(domain, agent, subject, k=1, query_vec=vec, min_score=DUPLICATE_SCORE)
            if dup:
                trace["memories_skipped"].append({"subject": subject, "duplicate_of": dup[0]["memory_id"]})
                continue
            scope = agent_memory.scope_for(agent, kind)
            mid = agent_memory.remember(domain, scope, subject, content, source=f"conversation with {a['persona']}",
                                        kind=kind, session_id=session["session_id"], embedding=vec)
            trace["memories_saved"].append({"memory_id": mid, "kind": kind, "subject": subject, "content": content,
                                            "scope": "team" if scope == agent_memory.TEAM else "agent"})
    except Exception as exc:  # noqa: BLE001
        trace["errors"].append(f"memory write: {type(exc).__name__}: {str(exc)[:160]}")
    lap("remember", s)

    trace["timings_ms"]["total"] = round((time.perf_counter() - t0) * 1000)
    seq = agent_sessions.append(session, "assistant", reply, trace)

    # short-term memory upkeep, after the reply is safely stored
    try:
        def summarise(prev: str | None, fold: list[dict[str, Any]]) -> str:
            text = "\n".join(f"{m['role'].upper()}: {m['content'][:600]}" for m in fold)
            return _text(_chat_model(UTILITY_MODEL, UTILITY_THINKING_BUDGET).invoke(_SUMMARY_PROMPT.format(
                persona=a["persona"], summary=prev or "(none)", messages=text)).content).strip()
        folded = agent_sessions.maybe_summarise(session, summarise)
        if folded:
            trace["short_term"]["folded_now"] = folded
    except Exception as exc:  # noqa: BLE001
        trace["errors"].append(f"summary: {type(exc).__name__}: {str(exc)[:160]}")

    return {"session_id": session["session_id"], "title": session["title"], "seq": seq,
            "reply": reply, "trace": trace}
