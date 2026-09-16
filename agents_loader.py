"""Load the .claude/agents/*.md team into LangChain deepagents.

The agent definitions are the portable asset. Claude Code reads .claude/agents/
natively; this adapter lets the SAME files drive a headless deepagents run so you
can move from interactive (Phase 1-4) to unattended CI (Phase 5) without rewriting
a single prompt.

Headless runs use Google Gemini (requires GOOGLE_API_KEY in the environment) rather
than the Claude Code session itself, since there is no Claude Code runtime to host
the agent outside an interactive session.

Real tool-calling + a real human gate (Phase C): every agent gets agents/jarvis_tools.py's
tools, wired to Jarvis's actual pipeline -- not a sandboxed stand-in. write_intake_contracts
is the one tool configured as an interrupt point (GATE_INTERRUPTS below): the compiled
LangGraph pauses BEFORE that call runs and returns control to whoever invoked it; nothing
resumes it except an explicit second call with the same thread_id, after a human has looked at
what compile_intake_preview showed. That pause is enforced by langgraph's Command/interrupt
machinery, not by asking the model nicely -- the same mechanism CLAUDE.md's Freeze-gate rule
was always describing, just made structurally impossible to skip rather than merely instructed.
A SqliteSaver checkpointer makes the paused state durable to a file (harness/agent_checkpoints.
sqlite), on purpose: the run can be resumed from a different process later (e.g. an "Approve"
click in the Control Room, Phase D), not only within the same Python session that started it.
"""
from __future__ import annotations

import pathlib
import re
import sqlite3

AGENT_DIR = pathlib.Path(__file__).parent / ".claude" / "agents"
FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)
CHECKPOINT_DB = pathlib.Path(__file__).parent / "harness" / "agent_checkpoints.sqlite"

GATE_INTERRUPTS = {"write_intake_contracts": True}

PERMISSIONS_MODULE_AVAILABLE = True
try:
    from deepagents.backends import FilesystemBackend
    from deepagents.middleware.filesystem import FilesystemPermission
except ImportError:  # noqa: BLE001 -- older/newer deepagents without this middleware; degrade to no sandboxing
    PERMISSIONS_MODULE_AVAILABLE = False

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


def load_subagents(tools: list | None = None) -> list[dict]:
    subagents = [_parse(p) for p in sorted(AGENT_DIR.glob("*.md")) if p.stem != "program-manager"]
    if tools:
        for s in subagents:
            s["tools"] = tools
    return subagents


def build_team(model: str | None = None, tools=None, gated: bool = True, checkpointer=None):
    """Program Manager as supervisor, specialists as subagents.

    gated=True (the default) wires the real Freeze gate: write_intake_contracts pauses the
    graph and a SqliteSaver checkpointer persists that pause to disk so a separate process can
    resume it later. gated=False skips both -- useful for a quick ungated smoke test, never for
    a run that will actually write contracts.

    checkpointer, if given, is used as-is instead of opening a fresh connection to
    CHECKPOINT_DB -- for a caller (webapp/backend/agent_runs.py's _inspect_pending) that needs
    to reconnect to an ALREADY-paused run's exact state to read it, not start a new one.
    Reassigning a compiled graph's .checkpointer after the fact isn't a documented, reliable
    operation, so the checkpointer has to go in at construction time instead."""
    from deepagents import create_deep_agent

    if tools is None:
        from agents.jarvis_tools import ALL_TOOLS
        tools = ALL_TOOLS

    pm = _parse(AGENT_DIR / "program-manager.md")
    kwargs: dict = dict(
        model=model or pm["model"],
        tools=tools,
        system_prompt=pm["system_prompt"],
        subagents=load_subagents(tools),
        name="jarvis-program-manager",
    )

    if gated:
        if checkpointer is not None:
            kwargs["checkpointer"] = checkpointer
        else:
            CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(CHECKPOINT_DB), check_same_thread=False)
            from langgraph.checkpoint.sqlite import SqliteSaver
            kwargs["checkpointer"] = SqliteSaver(conn)
        kwargs["interrupt_on"] = GATE_INTERRUPTS
        if PERMISSIONS_MODULE_AVAILABLE:
            repo_root = pathlib.Path(__file__).parent
            kwargs["backend"] = FilesystemBackend(root_dir=str(repo_root), virtual_mode=False)
            kwargs["permissions"] = [
                FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
                FilesystemPermission(operations=["write"],
                                     paths=["/contracts/**", "/docs/templates/**", "/webapp/uploads/**"],
                                     mode="allow"),
                FilesystemPermission(operations=["write"],
                                     paths=["/**/*profile*", "/**/*credential*", "/**/*secret*", "/**/*.env",
                                            "/**/*token*", "/**/*.duckdb"],
                                     mode="deny"),
                FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
            ]

    return create_deep_agent(**kwargs)


if __name__ == "__main__":
    for a in [_parse(AGENT_DIR / "program-manager.md")] + load_subagents():
        print(f"{a['name']:<20} {a['model']:<28} {a['description'][:60]}")
