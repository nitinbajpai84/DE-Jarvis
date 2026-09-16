# Evidence: Phase I -- Step 02 architecture doc and RTO/RPO capture

**Date:** 2026-09-16
**Goal:** Agent 2's (Solution Architect) half of Step 02 -- RTO/RPO, platform binding, layering
rationale, per-entity SCD strategy, and risks/mitigations, with the same rigor as intent
capture and gap analysis (Phase H): captured for real, and checked mechanically where a check
is actually possible.

## Where this fits in the existing gate structure -- extended G2, not a new gate

`docs/agentic-sdlc.md` already names "G2 Architecture Gate... This is the 'Freeze'" -- the
project's own design doc treats architecture sign-off and the Freeze gate as the same moment,
not two separate approvals. So this phase extends `write_intake_contracts` (G2), the same way
Phase H extended `accept_catalogue` (G1) for intent and gap analysis, rather than inventing a
fifth gate that would contradict the already-established doc.

## The mechanical, not semantic, design decision -- same rule as Phase H

`check_consistency()` checks only what's verifiable against a fact the platform already has:
- **Platform binding**: does the architecture's declared target match
  `spec["environment"]["platform"]` -- the real target THIS compile is about to produce?
- **Per-entity SCD strategy**: does a declared entity's `scd_type` match what the compiled
  silver model actually specifies (`compiled["model"]["dimensions"]`/`["facts"]`)? And does
  that SCD type need a platform capability the target actually has
  (`contracts/platform/<target>.yaml`'s `capabilities.scd2_snapshot`)?

It does **not** judge whether an RTO of "4 hours" is reasonable for the described workload --
that is exactly the semantic call CLAUDE.md rule 4 says not to silently make. RTO/RPO are
recorded as evidence for a human to weigh, never graded.

## What was built

- **`emitters/architecture.py`** -- `capture_architecture()` (replace, not merge, same as
  `capture_intent`), `check_consistency()` (recomputed fresh at G2 approval time from the exact
  spec/compiled-model that write is about to act on, never trusted from an earlier click).
  Persisted to `contracts/architecture/<domain>/architecture.yaml` -- tracked in git, same
  reasoning as `contracts/intent/`.
- **Three new tools**: `capture_architecture` (Agent 2, ungated), `check_architecture_consistency`
  (Agent 2, ungated, read-only preview of what G2 will check), and G2
  (`write_intake_contracts`) itself extended to recheck and refuse. `ALL_TOOLS` count: 15 -> 17.
- **G2 now writes a proper gate record**, `evidence/gates/<run>-G2.md` -- it previously only
  logged to the control plane, unlike G1/G3/G4, which all write evidence files. Brought in line
  with the rest of the gate machinery while touching this code anyway.
- **Step 02 screen**: an Architecture panel (RTO, RPO, platform binding, layering rationale,
  volume expectations, per-entity SCD strategy, risks), Save + Check consistency actions, and
  an issues panel listing each mismatch by kind and detail.

## Verification -- against the real asset_management domain, not a fixture

A deliberately wrong architecture doc first:
```
platform_binding: databricks   (real target is duckdb)
entity_scd: [{dim_awm_portfolio: scd2}]   (compiled model actually specifies scd1)
```
```
check_consistency -> issues: 2
  platform_mismatch -> declares 'databricks', compile targets 'duckdb'
  scd_mismatch -> dim_awm_portfolio: declares 'scd2', compiled model specifies 'scd1'
write_intake_contracts (G2) -> ok: False, refused: True
  reason: "G2 cannot pass while the architecture record disagrees with what this compile
           is about to produce."
```

Then a real, clean architecture doc for asset_management's actual design -- SCD1 for both
reference dimensions, duckdb target, real risk/mitigation for late-arriving positions:
```
check_consistency -> issues: 0
write_intake_contracts (G2) -> ok: True, written: 8 contract files
```
This clean record is kept as `contracts/architecture/asset_management/architecture.yaml`, a
real deliverable alongside the domain's other contracts.

**Both the refusal and the pass driven live through the actual browser**, not just at the
module level: opened Step 02, filled the architecture form, clicked Check consistency (real
"0 issues" result), then deliberately broke it (platform -> databricks, one entity -> scd2),
clicked Save then Check consistency again -- watched the real issue rows render
(`PLATFORM_MISMATCH`, `SCD_MISMATCH`, with their exact detail text) -- then called the real G2
tool directly and confirmed the identical refusal a human clicking Approve in a paused run
would hit. Reverted to the clean architecture, confirmed "0 issues" again, then confirmed G2
passes and genuinely writes the 8 contract files.

## Regression

25/25 on duckdb and Databricks. `agents_loader.GATE_INTERRUPTS` unchanged (still the same four
gates -- `write_intake_contracts` is the same tool name, its body changed, not its identity).

## Known gaps, stated plainly

- **The `capabilities.scd2_snapshot` check is real but unexercised against a failing case.**
  Both `duckdb.yaml` and `databricks.yaml` currently set `scd2_snapshot: true`, so there is no
  real platform in this project today that would trip that specific sub-check. The code path
  is correct and would fire against a platform that lacked the capability; it just hasn't been
  proven against one, because none exists here yet. Same honesty standard as Phase G's
  unverified API sampler.
- **The agent doesn't produce the architecture doc on its own.** A human (or an agent told
  exactly what to say) still fills the form -- there's no reasoning step where the SA agent
  proposes RTO/RPO from the stated intent's SLA, or infers a layering rationale from the
  catalogue.
- **No sign-off distinct from G2 itself.** The blueprint describes architecture as "a document
  which should be signed off," and this phase folds that sign-off into the existing Freeze
  decision rather than giving it a separate human gesture -- a deliberate scope call (see
  "Where this fits" above), not an oversight, but worth naming if a future pass wants
  architecture approved independently of contract-writing.
