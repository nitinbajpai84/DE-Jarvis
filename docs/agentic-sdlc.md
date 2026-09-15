# The Jarvis Agentic SDLC

Ten stages, three human gates. Every phase (P1..P5) runs the full loop.

## Stages

| # | Stage | Owner agent | Output artifact |
|---|-------|-------------|-----------------|
| 1 | Intent | Program Manager | `evidence/gates/<phase>-intent.md` |
| 2 | Requirement Analysis | BA | `specs/<phase>/requirements.md` + Open Questions |
| 3 | Codebase Discovery | Discovery (PM sub-task) | `specs/<phase>/discovery.md` |
| 4 | Design | Solution Architect + Data Architect | `specs/<phase>/design.md` + ADR |
| 5 | Plan | Program Manager | `specs/<phase>/plan.md` (task list, file list) |
| 6 | Implementation | DE Agent (per layer) | contracts + generated code |
| 7 | Testing & Validation | Test Manager | test pack + `evidence/runs/` |
| 8 | Review | Reviewer | `evidence/decisions/<phase>-review.md` |
| 9 | Impact Analysis | Solution Architect | lineage delta + breaking-change list |
|10 | Evidence | Evidence Agent | traceability matrix, rollback tag |

## Gates (human decision required — the agent stops)

**G1 Intent Gate** — after Stage 2.
Approve: scope, success criteria, and the Open Questions list is EMPTY.
An unanswered ambiguity is a blocker, not a note.

**G2 Architecture Gate** — after Stage 5.
Approve: data model / platform binding / grain / SCD choices / plan.
This is the "Freeze" — the spec version is bumped and implementation may not deviate.

**G3 Validation Gate** — after Stage 9.
Approve: tests passed, impact understood, rollback point exists.

## Gate record format
Every gate writes a file with: decision, approver, date, spec version, tests defined/run,
risks accepted, rollback tag. Nothing proceeds without it.

## Author != Reviewer
The DE Agent that wrote a model may not review it. The Reviewer Agent runs with a fresh
context and only the contract + the diff. This is the cheapest quality control you have.
