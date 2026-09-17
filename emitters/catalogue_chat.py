"""Step 02 conversational capture: talk to an agent instead of filling in the intent/
architecture forms field by field. A real Gemini call, not a scripted form-filler -- it asks
follow-up questions the way a Business Analyst or Solution Architect actually would, and only
proposes a structured record once it genuinely has enough to draft one.

Stateless by design: the frontend holds the conversation history (a list of
{"role": "user"|"assistant", "text": str}) and resends it whole on every turn, the same
"no new persistence layer" reasoning emitters/profiler.py's content extraction and
emitters/architecture.py's vision interpretation already use for a one-shot LLM call. A dropped
tab loses the conversation, not a contract -- nothing here is written to contracts/ until the
human clicks "Apply to the form" and then "Save", same propose-then-approve pattern as every
other capture path in this project.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_SYSTEM_PROMPTS = {
    "intent": (
        "You are Agent 3, the Program Manager on a data platform team, having a conversation "
        "with a business stakeholder to capture their INTENT for a data domain: what they want "
        "this platform to do for them. Ask natural follow-up questions, one or two at a time -- "
        "don't interrogate them with a wall of questions at once. You need to learn: the "
        "business outcome (why this matters), the reports/dashboards it should feed (each with "
        "a name, description, and the specific data points -- like column names -- it needs), "
        "SLAs (freshness, availability), and any business term whose definition might differ "
        "across systems (e.g. what counts as an 'active customer'). If this is their first "
        "message, greet them briefly and ask what they're trying to achieve.\n\n"
        "Once you genuinely have enough for a first draft (even if incomplete -- they can "
        "always refine it later), end your reply with a fenced JSON block on its own, "
        "shaped EXACTLY like this (omit fields you don't have yet):\n"
        "```json\n"
        '{"name": "short intent name", "business_outcome": "...", '
        '"definition_of_done": "...", "sla": {"freshness": "...", "availability": "..."}, '
        '"reports": [{"name": "...", "description": "...", "required_data_points": ["..."]}], '
        '"definitions": [{"term": "...", "definition": "...", "source": "..."}]}\n'
        "```\n"
        "Do not include the JSON block until you actually have real content for at least "
        "business_outcome and one report -- a premature or empty block is worse than none."
    ),
    "architecture": (
        "You are Agent 2, the Solution Architect on a data platform team, having a conversation "
        "with an engineer or architect to capture the ARCHITECTURE decisions for a data domain: "
        "RTO/RPO, why the bronze/silver/gold layering is shaped the way it is, expected data "
        "volumes, per-entity SCD (slowly changing dimension) strategy, and risks with "
        "mitigations. Ask natural follow-up questions, one or two at a time. If this is their "
        "first message, greet them briefly and ask about their recovery objectives or their "
        "biggest architectural concern.\n\n"
        "Once you genuinely have enough for a first draft, end your reply with a fenced JSON "
        "block on its own, shaped EXACTLY like this (omit fields you don't have yet):\n"
        "```json\n"
        '{"rto": "...", "rpo": "...", "layering_rationale": "...", '
        '"volume_expectations": "...", '
        '"entity_scd": [{"entity": "...", "scd_type": "scd1|scd2"}], '
        '"risks": [{"risk": "...", "mitigation": "..."}]}\n'
        "```\n"
        "Do not include the JSON block until you have real content for at least rto/rpo or "
        "layering_rationale -- a premature or empty block is worse than none."
    ),
}


def _extract_json_block(text: str) -> tuple[str, dict[str, Any] | None]:
    """Splits the assistant's reply into (prose, structured_suggestion). The JSON block is
    parsed defensively -- a malformed block just means no suggestion this turn, not a crash."""
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not m:
        return text.strip(), None
    prose = (text[:m.start()] + text[m.end():]).strip()
    try:
        return prose, json.loads(m.group(1))
    except Exception:  # noqa: BLE001 -- an unparseable block is treated as no suggestion, not an error
        return text.strip(), None


def chat_turn(kind: str, domain: str, history: list[dict[str, str]], message: str) -> dict[str, Any]:
    """One turn: the full conversation so far plus the new user message, and back comes the
    assistant's reply, split into the conversational text and (if present) a structured
    suggestion the frontend can offer to apply to the form.

    Memory (Phase 1): before replying, recalls whatever this domain's past conversations with
    this agent are relevant to the new message and folds it into the system prompt as real prior
    context -- this is concretely what "the agent remembers" means here, not a vague ambient
    trait. When a turn produces a suggestion, that suggestion is written back as a new memory,
    so the NEXT conversation (a different session, possibly days later) starts already knowing
    it. Best-effort: any memory failure (no rows yet, an embedding error) degrades to "no prior
    context" rather than breaking the conversation -- remembering is an enhancement, not a
    dependency of the chat working at all."""
    if kind not in _SYSTEM_PROMPTS:
        raise ValueError(f"kind must be one of {list(_SYSTEM_PROMPTS)}, got {kind!r}")

    from langchain.chat_models import init_chat_model
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    system_text = _SYSTEM_PROMPTS[kind] + f"\n\nThe domain is {domain!r}."
    try:
        from emitters.agent_memory import recall
        memories = recall(domain, kind, message, k=5)
        if memories:
            recalled = "\n".join(f"- {m['subject']}: {m['content']}" for m in memories)
            system_text += (
                f"\n\nYou have prior context from earlier conversations about this domain -- "
                f"use it, don't ask for things you already know:\n{recalled}"
            )
    except Exception:  # noqa: BLE001 -- memory is an enhancement; its absence must never break the chat
        pass

    messages = [SystemMessage(content=system_text)]
    for turn in history:
        cls = HumanMessage if turn.get("role") == "user" else AIMessage
        messages.append(cls(content=turn.get("text", "")))
    messages.append(HumanMessage(content=message))

    model = init_chat_model("google_genai:gemini-2.5-flash")
    reply = model.invoke(messages).content
    prose, suggestion = _extract_json_block(reply)

    if suggestion:
        try:
            from emitters.agent_memory import remember
            subject = suggestion.get("name") or f"{kind} draft"
            remember(domain, kind, subject, json.dumps(suggestion), source="catalogue_chat")
        except Exception:  # noqa: BLE001 -- same reasoning: never let memory writing break the chat
            pass

    return {"reply": prose, "suggestion": suggestion}
