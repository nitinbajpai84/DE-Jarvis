"""The journey and its human gates -- one registry, read by both the agent graph and the UI.

Phase C shipped exactly one real gate (write_intake_contracts) by naming it in a dict inside
agents_loader.py. That worked, but it put the gate list in the same file as the graph builder
and gave the Control Room nothing to render from, so the frontend's SDLC strip was a separate
hand-maintained list that could silently drift from what the graph actually interrupts on.

This module is the single source of truth instead:
  - agents_loader.GATE_INTERRUPTS is derived from GATES (a gate is real iff its tool exists),
  - the Control Room's journey view renders from JOURNEY + GATES,
so a gate cannot appear in the UI without actually interrupting the graph, and cannot interrupt
the graph without appearing in the UI. Adding a sixth gate is a row here, not an edit in four
files.

capability is deliberately part of the data, not a comment: "planned" gates are drawn in the
journey with what they WILL approve, greyed, because a customer walking through this should see
the whole path including the parts that aren't built -- not a shorter path that implies the
product is finished. Never quietly promote a gate to "live" without its tool actually existing;
_live_gate_tools() below is what stops that from being a lie.
"""
from __future__ import annotations

# Agent numbering follows the product blueprint (1=BA, 2=SA, 3=PM, 4=DE, 5=Test, 6=Viz, 7=Ops),
# which is what the customer sees. The repo's .claude/agents/*.md names are the implementation
# detail underneath, mapped here so neither vocabulary has to win.
AGENTS = {
    1: {"num": 1, "role": "Business Analyst", "colour": "#4C8DFF", "defs": ["business-analyst"]},
    2: {"num": 2, "role": "Solution Architect", "colour": "#9B7FE8", "defs": ["solution-architect", "data-architect"]},
    3: {"num": 3, "role": "Program Manager", "colour": "#2FE0C6", "defs": ["program-manager"]},
    4: {"num": 4, "role": "Data Engineer", "colour": "#CF8A4E", "defs": ["de-bronze", "de-silver", "de-gold"]},
    5: {"num": 5, "role": "Test Manager", "colour": "#3FC07A", "defs": ["test-manager"]},
    6: {"num": 6, "role": "Visualisation", "colour": "#5CC8E8", "defs": []},
    7: {"num": 7, "role": "Ops Manager", "colour": "#E2793D", "defs": ["ops-monitor"]},
}

JOURNEY = [
    {
        "step": 0, "id": "onboard", "name": "Workspace",
        "tagline": "The customer lands, signs in, and gets a domain of their own.",
        "agents": [], "capability": "partial",
        "gap": "Login exists; per-customer workspace provisioning and isolation do not.",
    },
    {
        "step": 1, "id": "sources", "name": "Connect & explore",
        "tagline": "The BA agent connects to the estate, profiles what it finds, and takes context from the team.",
        "agents": [1], "capability": "partial",
        "gap": "Connectors and a real profiler exist for file, database (Databricks) and API sources -- "
               "test a connection, sample it, see column types/nulls/candidate keys. What's still missing: "
               "the agent doesn't yet decide what to connect to on its own, and there's no free-form "
               "context intake (docs, notes) or conversational BA surface.",
    },
    {
        "step": 2, "id": "catalogue", "name": "Catalogue & intent",
        "tagline": "The catalogue is agreed, the intent and SLAs are captured, the architecture is signed.",
        "agents": [1, 2, 3], "capability": "partial",
        "gap": "The catalogue compiles for real from an uploaded workbook; intent capture and gap analysis "
               "are real and enforced at G1; architecture (RTO/RPO, layering, per-entity SCD strategy) is "
               "real and enforced at G2. What's still missing: the agent doesn't yet draft the catalogue "
               "or the architecture itself -- a human (or an agent told exactly what to say) fills both in.",
    },
    {
        "step": 3, "id": "build", "name": "Data engineering",
        "tagline": "Contracts to bronze to silver to gold, with data quality and lineage at every hop.",
        "agents": [4], "capability": "live",
        "gap": "",
    },
    {
        "step": 4, "id": "validate", "name": "Test & visualise",
        "tagline": "Tests per layer, then end-to-end against the intent that was signed.",
        "agents": [5, 6], "capability": "partial",
        "gap": "The regression pack is real but hand-written; per-layer generation and the visualisation "
               "agent do not exist.",
    },
    {
        "step": 5, "id": "operate", "name": "Operations",
        "tagline": "Readiness, the daily report, and the incident loop back through the DE and test agents.",
        "agents": [7, 4, 5, 3], "capability": "partial",
        "gap": "Slack alerting and the daily digest are live; readiness sign-off, tickets and the "
               "resolution loop are not.",
    },
]

# A gate is a genuine pause in the agent graph: `tool` is registered as a langgraph interrupt
# point, so the graph stops BEFORE that tool runs and only a human decision resumes it. `prep`
# is the ungated tool that produces the evidence the human looks at while deciding -- the
# compile_intake_preview -> write_intake_contracts pattern proven in Phase C, applied to every
# gate rather than just the one.
GATES = [
    {
        "id": "G0", "step": 1, "name": "Sources confirmed",
        "approves": "That the agent found everything, or the team names what it missed.",
        "prep": None, "tool": None, "capability": "planned",
        "blocked_by": "Discovery (connect + profile) now exists, but nothing yet asks the customer to "
                       "confirm the inventory is complete -- the gate tool itself hasn't been built.",
    },
    {
        "id": "G1", "step": 2, "name": "Catalogue & intent",
        "approves": "Domain, target platform, schemas, every mapped source, and the business intent.",
        "prep": "compile_intake_preview", "tool": "accept_catalogue", "capability": "live",
        "rule": "Open questions must be empty, and (where intent has been captured) gap analysis "
                "must show 0 open data-point gaps or definition conflicts. An unanswered "
                "ambiguity is a blocker, not a note.",
    },
    {
        "id": "G2", "step": 2, "name": "Freeze",
        "approves": "Writing the contracts. After this the spec is frozen and implementation may not deviate.",
        "prep": "compile_intake_preview", "tool": "write_intake_contracts", "capability": "live",
        "rule": "Nothing reaches contracts/ before this decision, and (where architecture has been "
                "captured) the declared platform binding and per-entity SCD strategy must match "
                "what this exact compile produces.",
    },
    {
        "id": "G3", "step": 4, "name": "Validation / UAT",
        "approves": "Test results and data-quality evidence, checked against the intent signed at G1.",
        "prep": "gather_validation_pack", "tool": "accept_validation", "capability": "live",
        "rule": "Tests passed, impact understood, rollback point exists.",
    },
    {
        "id": "G4", "step": 5, "name": "Go-live readiness",
        "approves": "Handing the pipeline to the daily schedule and the on-call loop.",
        "prep": "run_ops_readiness", "tool": "accept_go_live", "capability": "live",
        "rule": "The whole pipeline has run end to end on the target platform since the last change.",
    },
]

STEPS_BY_ID = {s["id"]: s for s in JOURNEY}
GATES_BY_ID = {g["id"]: g for g in GATES}
GATE_BY_TOOL = {g["tool"]: g for g in GATES if g["tool"]}


def live_gate_tools() -> list[str]:
    """Tool names that must interrupt the graph. Cross-checked against the tools actually
    registered in agents/jarvis_tools.py, so a gate marked "live" here with no implementation
    behind it fails loudly at import instead of silently never firing -- the failure mode that
    would make the whole journey view a lie."""
    from agents import jarvis_tools
    registered = {t.name for t in jarvis_tools.ALL_TOOLS}
    live = [g["tool"] for g in GATES if g["capability"] == "live"]
    missing = [t for t in live if t not in registered]
    if missing:
        raise RuntimeError(
            f"gates.py marks {missing} as live gates, but no such tool is registered in "
            f"agents/jarvis_tools.py ALL_TOOLS. Either implement the tool or set capability='planned'."
        )
    return live


def journey_view() -> dict:
    """Everything the Control Room needs to draw the journey, from this one registry."""
    return {
        "agents": AGENTS,
        "steps": [
            {**s, "agent_detail": [AGENTS[n] for n in s["agents"]],
             "gates": [g for g in GATES if g["step"] == s["step"]]}
            for s in JOURNEY
        ],
        "gates": GATES,
    }
