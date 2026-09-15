"""Load the .claude/agents/*.md team into LangChain deepagents.

The agent definitions are the portable asset. Claude Code reads .claude/agents/
natively; this adapter lets the SAME files drive a headless deepagents run so you
can move from interactive (Phase 1-4) to unattended CI (Phase 5) without rewriting
a single prompt.

Headless runs use Google Gemini (requires GOOGLE_API_KEY in the environment) rather
than the Claude Code session itself, since there is no Claude Code runtime to host
the agent outside an interactive session.
"""
from __future__ import annotations

import pathlib
import re

AGENT_DIR = pathlib.Path(__file__).parent / ".claude" / "agents"
FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)

# Cheap models for mechanical agents, stronger models where judgement matters.
MODEL_MAP = {
    # Explicit "google_genai:" provider prefix -- a bare "gemini-*" string resolves
    # to Vertex AI (needs GCP project/service-account creds) not the Gemini Developer
    # API (needs only GOOGLE_API_KEY). See langchain.chat_models.init_chat_model.
    "haiku": "google_genai:gemini-2.5-flash",
    "sonnet": "google_genai:gemini-3.1-pro-preview",
}


def _parse(path: pathlib.Path) -> dict:
    m = FRONTMATTER.match(path.read_text())
    if not m:
        raise ValueError(f"{path} is missing YAML frontmatter")
    meta, body = m.groups()
    fields = {}
    for line in meta.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return {
        "name": fields["name"],
        "description": fields.get("description", ""),
        "system_prompt": body.strip(),
        "model": MODEL_MAP.get(fields.get("model", "sonnet")),
    }


def load_subagents() -> list[dict]:
    return [_parse(p) for p in sorted(AGENT_DIR.glob("*.md"))
            if p.stem != "program-manager"]


def build_team(tools=None):
    """Program Manager as supervisor, specialists as subagents."""
    from deepagents import create_deep_agent

    pm = _parse(AGENT_DIR / "program-manager.md")
    return create_deep_agent(
        tools=tools or [],
        system_prompt=pm["system_prompt"],
        subagents=load_subagents(),
        model=pm["model"],
    )


if __name__ == "__main__":
    for a in [_parse(AGENT_DIR / "program-manager.md")] + load_subagents():
        print(f"{a['name']:<20} {a['model']:<28} {a['description'][:60]}")
