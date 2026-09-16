# Evidence: Phase H -- Step 02 intent capture and gap analysis

**Date:** 2026-09-16
**Goal:** build the two Step 02 capabilities the blueprint calls for that had no home anywhere
in the platform: the PM capturing *why* this platform is being built (business outcome, SLAs,
which reports it feeds), and the BA checking that intent against what the platform actually
knows -- "do we have the data points," "whose definition of active portfolio applies."

## Where this had to go, and where it didn't already exist

The intake workbook's `01_Project` sheet is purely technical/environment config (platform,
schemas, auth profile) -- confirmed by reading it, not assumed. There was no home anywhere for
"why are we building this" or "which report needs which column." This is genuinely new
capability, not a wiring exercise over something that already existed.

## The mechanical, not semantic, design decision

Gap analysis does EXACT (case-insensitive) name matching, never fuzzy or LLM-judged matching.
A required data point `settlement_date` does not match a column `settlement_dt` -- that is a
deliberate refusal to guess at a mapping the platform has no basis for, the same CLAUDE.md rule
4 (never silently invent) that governs every DQ rule and business check already in this
platform. A human resolves what exact-match can't; the tool only refuses to hide that a
decision is needed.

## What was built

- **`emitters/intent.py`** -- `capture_intent()` (replace, not merge, so a revision shows
  exactly what was submitted), `run_gap_analysis()` (recomputed fresh every call, never trusted
  from a stale file -- reads BOTH approved contracts, `contracts/sources/<domain>/*.yaml`, AND
  Step 01 discovery profiles, `contracts/discovery/<domain>/*.json`, so a data point that's only
  been profiled but not yet approved still resolves, with its provenance labeled). Persisted to
  `contracts/intent/<domain>/` -- tracked in git, unlike `contracts/discovery/`, because a human
  deliberately authored it rather than it being derived from probing a live system.
- **Two new ungated tools**: `capture_intent` (Agent 3, PM) and `run_gap_analysis` (Agent 1,
  BA). `ALL_TOOLS` count: 13 -> 15.
- **G1 (`accept_catalogue`) extended**, not replaced: still checks open_questions as before,
  now ALSO recomputes gap analysis at approval time and refuses if intent has been captured for
  the domain and gap analysis finds any open item -- unresolved data-point gap or a term with
  more than one definition. A domain with no intent record is unaffected (intent is additive,
  not required), so this doesn't retroactively invalidate the G1 passes asset_management and
  insurance already have on record.
- **Step 02 screen**: an intent form (business outcome, definition of done, SLA, a
  pipe-delimited "reports" field with per-report required data points, a pipe-delimited
  "definitions" field), Save + Run gap analysis actions, and a results panel showing each data
  point as found/open with exactly which source resolved it, and any definition conflict with
  every competing wording listed.
- **`webapp/backend/intent.py`** + 3 routes -- same "call the real function directly, don't
  spin up an agent run" pattern as `discovery.py`.

## Verification -- against the real asset_management domain, not a fixture

A deliberately broken intent first, to prove the refusal is real:
```
reports: [{required_data_points: [..., "settlement_date"]}]   <- doesn't exist anywhere
definitions: [{"active portfolio": "...last 30 days..."}, {"active portfolio": "...not closed..."}]
```
```
run_gap_analysis -> open_count: 2
  open      settlement_date    []
  CONFLICT: active portfolio -> two different wordings
accept_catalogue (G1) -> ok: False, refused: True
  reason: "G1 cannot pass while gap analysis has open data-point gaps or definition conflicts."
```

Then a real, clean intent for asset_management's actual Portfolio Value Dashboard need:
```
required_data_points: [portfolio_code, portfolio_type, market_value, instrument_code, quantity]
```
```
run_gap_analysis -> open_count: 0
  portfolio_code    -> awm_portfolios (contracted), awm_positions (contracted)
  portfolio_type    -> awm_portfolios (contracted)
  market_value      -> awm_positions (contracted)
  instrument_code   -> awm_instruments (contracted), awm_positions (contracted)
  quantity          -> awm_positions (contracted)
accept_catalogue (G1) -> ok: True
```
Every match is real -- found by reading the actual compiled `.source.yaml` schemas, not
asserted. This clean intent is kept as `contracts/intent/asset_management/intent.yaml`, a real
deliverable alongside the domain's other contracts, not a test fixture.

**The refusal proven live through the actual browser, not just at the module level**: opened
Step 02 in the running Control Room, edited the reports field to include the bad data point
again, clicked Save, watched the gap panel render "1 open item" with `settlement_date` marked
open in real time, then called `accept_catalogue` directly and confirmed the same refusal a
human clicking Approve in a real paused run would hit. Then reverted to the clean intent and
confirmed the panel returned to "0 open items."

## Regression

25/25 on duckdb and Databricks. `agents_loader.GATE_INTERRUPTS` unchanged (still the same four
gates) -- adding two ungated tools didn't touch gate registration, confirmed by import.

## Known gaps, stated plainly

- **The `consumers` field on a report (who reads it) has no UI input.** `capture_intent`'s
  schema supports it; the pipe-delimited form only collects name/description/required data
  points. Saving through the form drops any `consumers` a domain's intent previously had --
  found while re-testing after this phase's UI edits, not designed around. A real, if minor,
  round-trip gap.
- **The agent doesn't capture intent on its own.** A human (or an agent told exactly what to
  say) still fills the form; there's no conversation where the PM agent asks clarifying
  questions and drafts the intent itself.
- **Gap analysis has no equivalent for the architecture step.** RTO/RPO, layering, platform
  binding -- Agent 2's (Solution Architect) piece of Step 02 -- still has no capability behind
  it at all. Explicitly out of scope for this phase; the user asked for intent capture and gap
  analysis specifically.
- **No UI for editing a definition once entered** beyond retyping the whole textarea -- workable
  for a first pass, not a polished editing experience.
