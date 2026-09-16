# Evidence: Phase F -- the journey shell, and four real gates instead of one

**Date:** 2026-09-16
**Goal:** turn the Control Room from a dashboard of panels into the five-step journey the
product blueprint describes, and generalise the Phase C Freeze gate so the other gates in that
blueprint are real interrupts rather than drawings.

## What was built

- **`agents/gates.py`** -- one registry for the journey (6 steps, the agents on each) and the
  gates (G0..G4). Both the agent graph and the Control Room render from it.
  `agents_loader.GATE_INTERRUPTS` is now *derived* from this registry rather than hand-listed,
  so a gate cannot appear in the UI without interrupting the graph, or interrupt the graph
  without appearing in the UI. The two used to be independent lists, free to drift.
- **Four gated tools** (`agents/jarvis_tools.py`), up from one:
  - G1 `accept_catalogue` -- records acceptance of the compiled catalogue.
  - G2 `write_intake_contracts` -- the existing Freeze gate, unchanged.
  - G3 `accept_validation` -- UAT/validation sign-off, prepared by `gather_validation_pack`.
  - G4 `accept_go_live` -- go-live sign-off, prepared by `run_ops_readiness` (which runs the
    whole pipeline end to end for the domain).
  Each writes a gate record to `evidence/gates/<run>-<gate>.md` in the same shape as the
  hand-written P1 records, so the evidence trail doesn't fork by author.
- **`webapp/backend/journey.py`** + `GET /api/journey` -- the registry plus what is actually
  true for a domain right now. Every number observed (contracts on disk, live row counts,
  DQ results, gate records), never asserted.
- **A rebuilt `webapp/frontend/index.html`** -- left rail of the five steps with capability and
  gate badges, one screen per step, and a sticky attention bar that surfaces any paused gate
  from any screen.

## The design decision that matters: gates re-derive their own evidence

A gate whose blocking rule is evaluated against a `summary` string the model wrote is not a
gate -- the thing being checked and the thing doing the checking would have the same author.
So every accept_* tool re-derives what it is gating:

- `accept_catalogue` recompiles the workbook itself and refuses if any open question remains.
- `accept_validation` reads back the pack `gather_validation_pack` persisted, and refuses if
  the suite failed or any DQ check is failing.
- `accept_go_live` reads back the readiness report and refuses if any step of the end-to-end
  run failed.

A human clicking Approve is approving *evidence*, so there has to be evidence. These tools
refuse **after** approval when there isn't.

## Verification

Every claim below was run, not reasoned about.

**1. The registry actually drives the graph.**
```
GATE_INTERRUPTS: {'accept_catalogue': True, 'write_intake_contracts': True,
                  'accept_validation': True, 'accept_go_live': True}
```

**2. The drift guard fires.** Marking G0 live with a tool that doesn't exist:
```
RuntimeError: gates.py marks ['confirm_sources'] as live gates, but no such tool is
registered in agents/jarvis_tools.py ALL_TOOLS.
```
A gate cannot be claimed as real in the UI without an implementation behind it.

**3. G1's blocking rule genuinely blocks.** Built a workbook with one `natural_language` DQ
rule (which the compiler turns into an open question), then called the gate tool directly --
i.e. simulating the state *after* a human approved:
```
preview ok: True | open_questions: 1
G1 -> ok: False | refused: True
reason: G1 cannot pass while open questions remain.
```
"An unanswered ambiguity is a blocker, not a note" was a line in docs/agentic-sdlc.md. It is
now enforced in code.

**4. G3 and G4 refuse without evidence.**
```
G3 -> refused: No validation pack for this run -- call gather_validation_pack first.
G4 -> refused: No readiness report for this run -- call run_ops_readiness first.
```

**5. A NEW gate really interrupts a real agent graph.** Not configuration -- a real
Gemini-driven run, fresh thread:
```
PAUSED at: ['accept_catalogue']
  gate: G1 Catalogue & intent | step 2
  args: {'workbook_path': 'docs/templates/_awm_intake_workbook.xlsx', 'run_id': 'GT-0001'}
```

**6. The whole loop through the real browser.** Started a run via `POST /api/agent-runs`; it
paused at G2; the attention bar appeared with the gate's identity and what it approves; the
step-02 gate card showed the exact pending tool call and its arguments; clicked **Approve** in
the actual browser; the run resumed and completed:
```
[1] running | stages: specify
[2] running | stages: specify,freeze
[3] completed | stages: specify,freeze
```

**7. Regression.** 25/25 on duckdb and 25/25 on Databricks, after all of the above.

## Bug caught in my own code before it shipped

The first version had `loadAgentRuns()` re-render the whole screen when a gate's state changed,
while the build screen's renderer calls `loadAgentRuns()` -- harmless only because that step
happens to have no gates today. A step with both would have recursed infinitely, and the
re-render would also have wiped the upload list mid-interaction. Replaced with
`refreshGatesInPlace()`, which updates only the gate cards.

## Known gaps (unchanged by this phase, stated plainly)

- **G0 has no capability behind it** and is correctly marked `planned` -- there is nothing to
  confirm until Step 01 discovery exists. It is drawn in the journey, greyed, with what it
  will approve.
- **G3 and G4 have not been exercised end-to-end through a live agent run.** Their tools, their
  refusal paths and their interrupt registration are all verified; what has not been watched is
  an agent choosing to call them in a full run. G1 and G2 have been.
- Steps 01, 04 and 05 screens show real observed data where any exists and an explicit dash
  where the capability doesn't. That is deliberate: "not built" and "built and empty" must not
  look the same to someone being walked through the product.
- Multi-agent delegation (PM -> BA -> SA -> DE -> Test) still hasn't been proven live; runs so
  far remain the PM calling tools directly.
