# Evidence: Phase K -- G3 driven by a real, live agent run for the first time

**Date:** 2026-09-16
**Goal:** the user asked to "continue testing for insurance" specifically by running the full
G3 gate flow live: a real Gemini-driven agent, not a script calling `gather_validation_pack`/
`accept_validation` directly, reaching the G3 gate for insurance and genuinely refusing on the
real orphan-FK finding from Phase J.

## The gap this closes

Phase F's own evidence doc named it plainly: "G3 and G4 have not been exercised end-to-end
through a live agent run... every real run so far has been the PM calling the two intake tools
directly." Every prior gate proof in this project (G1, G2, G3's refusal logic, G4) was verified
by calling the tool functions directly from a script -- correct proof that the *tool* works,
but never proof that an *agent* would actually choose to call it correctly in sequence.

## Why this needed new plumbing, not just a new prompt

`agents/run_cli.py`'s `cmd_start` was hard-wired to one task: "a client uploaded a workbook,
compile and maybe freeze it." Insurance has no workbook to upload -- it's an already-onboarded
domain with real contracts and real data. There was no way to start an agent run whose task is
"validate this existing domain," so `run_cli.py start` gained a `--kind {intake,validate}` flag
(intake remains the default, unchanged). `--kind validate` needs `--target` instead of
`--workbook`, and drives a different task prompt: call `run_test_pack`, then
`gather_validation_pack`, then attempt `accept_validation` -- explicitly instructed not to skip
the gate attempt even if the test pack results already show failures, because "the gate's own
refusal, re-derived from fresh evidence, is the record that matters, not your own summary."
Wired through to the webapp as `POST /api/agent-runs/validate` and a "Validate via Agent"
button on the Step 04 screen -- no new gate-rendering code was needed, since the existing
attention bar and gate-card machinery (built generically in Phase F) already renders whatever
gate a run is actually paused at.

## Verification -- twice, deliberately

**First, via the CLI directly** (to isolate the new plumbing from the browser):
```
$ python agents/run_cli.py start --kind validate --domain-hint insurance --target duckdb ...
AWAITING_APPROVAL run_id=d6e94209... thread_id=cli-validate-test-01
```
Inspected the paused state directly against the checkpoint: genuinely paused on
`accept_validation` with real args (`domain=insurance`, `target=duckdb`). Resumed with
`--decision approve`:
```
COMPLETED run_id=d6e94209...
```
Read back the real stage log (`control.sdlc_stage_run`), not the run's own summary:
```
validate | test-manager  | failed | test pack for domain='insurance': 6 failing case(s) across 49 total
validate | test-manager  | failed | validation pack: platform regression passed, test pack 6 case(s) failing
validate | human+system  | failed | G3 refused: platform_regression_passed=True, test_pack_failing=6
```
No `evidence/gates/*-G3.md` was written -- confirming the refusal is genuine, not a pass that
forgot to log. Read the agent's own final message: it correctly summarized all 6 real
orphan-FK failures by name and row count, reported the G3 refusal reason verbatim, and
explicitly noted the refusal held "regardless of approval."

**Second, through the actual browser**, because a CLI proof and a browser proof are different
claims -- the CLI test proves the plumbing works; the browser test proves a human can actually
use it. Clicked "Validate via Agent" on Step 04 for insurance in the running Control Room;
watched the run appear and progress to `awaiting_approval`; the attention bar rendered
`⏸ PAUSED · G3 · VALIDATION / UAT` with the real pending tool call and its args, exactly as
designed. Clicked Approve. Confirmed via `read_network_requests` that the real
`POST /api/agent-runs/<id>/approve` request actually fired (a first click attempt silently
missed its target -- caught by checking the network log rather than assuming the click worked,
not by trusting the screenshot). The run completed; the control-plane stage log for this exact
browser-driven run_id shows the identical three-row refusal trail as the CLI run.

## What this proves, stated plainly

Not "the tool refuses when called" (already proven in Phase J) -- **a real agent, given only a
plain-language task, correctly chooses to run the test pack, correctly summarizes real
per-domain findings, correctly attempts the gate, and correctly reports a refusal it did not
have the authority to override.** This is the multi-step, tool-sequencing behavior the whole
gate architecture depends on, exercised for the first time against G3.

## Regression

25/25 on duckdb and Databricks. `tests/test_pipeline.py` itself untouched.

## Known gaps, stated plainly

- **G4 (go-live readiness) still hasn't been driven by a live agent run.** Same gap Phase F
  named for G3, now closed for G3 only -- G4 is the natural next candidate if this matters again.
- **The underlying orphan-FK data problem for insurance remains unfixed**, as decided when this
  phase was scoped: the point was proving the gate works live, not resolving the data issue.
- **`--kind validate` assumes `duckdb` bookkeeping** (`run_cli.py`'s existing
  `BOOKKEEPING_TARGET` constant, unchanged by this phase) even when `--target databricks` is
  passed for the actual test-pack execution -- the same known, already-documented limitation
  `run_cli.py`'s own module docstring names for the intake path, now shared by the validate path
  too. Not new, not hidden, just inherited.
