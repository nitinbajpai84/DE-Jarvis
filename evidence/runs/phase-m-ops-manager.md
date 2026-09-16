# Evidence: Phase M -- Agent 7 (Ops Manager), the full incident loop, and G5

**Date:** 2026-09-17
**Goal:** the user asked for the Operations Manager built "complete, all its functionalities,"
connected to Slack: when the operate step finds a real issue, raise a tracked ticket, assign it
to the Data Engineer (Agent 4), have the DE propose a fix, require a human to approve it, and
have that approval independently re-verify the fix before closing the ticket -- then prove the
whole scenario end to end, including through a real live agent run, the way every other gate in
this project has been proven.

## What this closes

Step 05 (`agents/gates.py`'s own gap text, before this phase) said plainly: "Ops doesn't run on
a schedule... What's still missing" listed the ticket loop as not yet built. It is now real:
detect -> raise -> assign -> DE proposes a fix -> a human-gated, test-re-verified sign-off (G5)
-> Slack notification, with nothing invented -- every ticket traces to a real failing case from
`emitters/test_pack.py` (the same per-layer checker G3 already uses), never a second, parallel
definition of "broken."

## What was built

- **`control.incident_ticket`** (`emitters/control_plane.py`): one row per detected issue --
  domain, phase, entity (the test pack's own stable case id, e.g.
  `fk:fact_policy_coverage:dim_policy`), status, assignment, resolution note, verification flag.
  `raise_ticket()`/`update_ticket()` reuse the retry-on-conflict pattern already proven necessary
  for `log_sdlc_stage` (DuckDB's non-atomic `max(id)+1` assignment under concurrent writers).
- **`emitters/ops_tickets.py`**: the business logic. `scan_for_issues` wraps
  `generate_test_pack` and returns exactly the failing cases -- no separate detection heuristic.
  `raise_tickets_from_scan` dedups by `(case id, issue_type)` against currently-open tickets, so
  re-scanning a standing issue doesn't spam duplicate tickets. `verify_and_resolve` is the actual
  enforcement: re-runs the test pack fresh and matches the ticket's exact case id -- refuses to
  resolve if it still fails, regardless of the DE's resolution_note. `notify_slack` reuses
  `harness/ops_monitor.py`'s existing webhook pattern.
- **Six new agent tools** (`agents/jarvis_tools.py`, now 26 total): `scan_and_raise_tickets`,
  `list_open_tickets`, `assign_ticket_tool`, `propose_ticket_fix`, **`accept_ticket_resolution`**
  (the gated G5 tool), `reject_ticket_tool`.
- **G5 registered** in `agents/gates.py`'s single registry -- the same mechanism that derives
  both `agents_loader.GATE_INTERRUPTS` and the Control Room's rendering, so G5 could not appear
  in the UI without actually interrupting the graph (`live_gate_tools()`'s drift guard passed).
- **Backend API** (`webapp/backend/ops_tickets.py`, wired into `main.py`):
  `GET /api/tickets`, `POST /api/tickets/{scan,assign,propose-fix,reject}` -- deliberately does
  NOT expose G5 itself; closing a ticket for real still has to go through the gated agent-run
  flow like every other gate.
- **`journey.py`'s `operate` step** now reports real `open_tickets`/`resolved_tickets` counts,
  not a placeholder.
- **Control Room UI** (`webapp/frontend/index.html`): an "Incident tickets" panel on the
  Operations screen -- a "Scan for issues" button, ticket cards with status badges (open /
  assigned / fix pending approval / resolved / rejected) and inline action controls (assign,
  propose fix, reject) scoped to what each status allows.

## Real detection, real dedup

Ran a real scan against insurance (real data, real DuckDB): found the exact 6 known orphan-FK
failures already on record from Phase J (`fk:fact_claim:dim_customer`,
`fk:fact_claim:dim_policy`, `fk:fact_payment:dim_customer`, `fk:fact_payment:dim_policy`,
`fk:fact_premium:dim_policy`, `fk:fact_policy_coverage:dim_policy`) and raised 6 real tickets.
Re-ran the scan: `already_ticketed: 6, newly_raised: []` -- dedup confirmed correct, not
theoretical.

## G5 refuses a false claim -- proven twice

**Direct tool call:** assigned ticket #6 to `de-silver`, proposed a fix with a deliberately
honest fake note ("Investigated -- claimed fix without actually doing anything (test)"), then
called `accept_ticket_resolution` after simulated human approval. Refused:
`{"ok": false, "gate": "G5", "refused": true, "reason": "the case still fails"}`. No
`evidence/gates/*-G5.md` was written.

**Live agent run**, the same standard every other gate in this project has been held to (not
just a direct tool call): extended `agents/run_cli.py`'s `--kind` mechanism (the same pattern
Phase K used for G3) with a new `--kind ops --ticket-id N` task. Ran it against ticket #6:
```
$ python agents/run_cli.py start --kind ops --domain-hint insurance --target duckdb --ticket-id 6
AWAITING_APPROVAL run_id=983cd30a... thread_id=run-983cd30a
$ python agents/run_cli.py resume --run-id 983cd30a... --decision approve
COMPLETED run_id=983cd30a...
```
Read the ticket back directly (not the agent's own summary): `status: fix_pending_approval`,
unchanged -- a real agent, given a human approval, still could not force a false claim through.

## G5 genuinely resolves a real fix -- proven twice, via a small controlled test

The insurance orphan-FK issue's actual historical fix was deliberately not forced through a
check it legitimately fails (see Phase J's evidence: a real FQC row-count-deviation block, not a
bug) -- fabricating filler rows to pass it would violate this project's own "never invent data"
rule. Proving the resolve mechanism instead used a small, clearly-labeled, temporary, real
controlled test: inserted one real `not_null`-violating row into the live
`asset_management_bronze.awm_portfolios` table, raised the resulting real ticket, assigned it,
proposed a fix -- then verified refusal *before* the row was actually deleted, deleted it for
real, and verified genuine resolution *after*. Run twice end-to-end (tickets #7/#8 direct-tool
and CLI-prompt bugs found along the way, see below; ticket #9 clean):

```
$ python agents/run_cli.py start --kind ops --domain-hint asset_management --target duckdb --ticket-id 9
AWAITING_APPROVAL run_id=45f391ac... thread_id=run-45f391ac
$ python agents/run_cli.py resume --run-id 45f391ac... --decision approve
COMPLETED run_id=45f391ac...
```
Ticket #9 read back directly: `status: resolved, verified_by_test: True, resolved_by: de-bronze`.
`evidence/gates/45f391ac-G5.md` exists, signed, with the real re-verification detail. Confirmed
in the running Control Room UI (asset_management domain, Operations screen): G5 renders
**PASSED**, `signed 2026-09-16T17:57:49 · evidence\gates\45f391ac-G5.md`.

## Two real bugs found and fixed while proving this, not before

**1. `ensure_control_schema` write-write conflict under real concurrency.** The Operations
screen fires roughly a dozen API calls in parallel on load; each calls `ensure_control_schema`
first. DuckDB is single-writer, and two connections' `ALTER TABLE` calls landing close together
raised `duckdb.duckdb.TransactionException: write-write conflict` -- reproduced live via the
actual browser, not synthetically. Fixed with the same retry-on-conflict pattern already proven
for `log_sdlc_stage`/`raise_ticket`: the DDL is idempotent, so retrying after another
connection's commit is always safe (`emitters/control_plane.py`).

**2. `run_cli.py`'s new `ops` task never told the agent which `run_id` to use** (unlike the
`validate` task, which explicitly passes it). The agent invented its own id
(`"ticket-8-signoff"`), and `_write_gate_record`'s `run_id[:8]` filename convention produced
`ticket-8-G5.md` -- which doesn't match `journey.py`'s `_GATE_FILE` regex
(`^[0-9a-f]{8}-G\d\.md$`), so the Control Room would never have shown G5 as passed even after a
genuine resolution. Caught by checking the journey API's own output, not by trusting the CLI's
"COMPLETED" line. Fixed by adding `run_id='{run_id}'` to the ops task prompt, exactly as the
validate task already does; re-ran the controlled test (ticket #9) to confirm the corrected
filename and a correctly-rendering PASSED gate.

## UI verification, through the actual browser

Local Control Room (`webapp/backend/main.py` on :8010), reloaded after every backend/frontend
change. Confirmed: the operate stat tiles show real open/resolved ticket counts; the "Incident
tickets" panel renders real ticket cards with correct status badges; clicking "Assign to Data
Engineer" on an open ticket moved it to `assigned` and re-rendered with a "Propose fix" control;
typing a resolution note and clicking "Propose fix" moved it to `fix_pending_approval` and
correctly showed "Awaiting G5 sign-off -- start an Ops agent run to reach the gate" (G5 is
deliberately NOT exposed as a quick UI action, same as every other gate); switching to the
asset_management domain showed G5 rendering PASSED with the real signed evidence file.
(Native `prompt()`/`alert()` dialogs don't fire reliably in the automated browser used for this
check, and are poor UX regardless -- replaced with inline text inputs in the ticket action row
before testing, rather than working around the dialog.)

## Regression

25/25 on duckdb and Databricks, both before and after the two bug fixes above.

## Known gaps, stated plainly

- **Ops still doesn't run on a schedule** -- every check here is triggered on demand (a "Scan
  for issues" click, or an agent task), not a cron/daily job.
- **No dashboard view of ticket history over time** -- the UI shows current open/resolved state,
  not a trend.
- **The insurance orphan-FK issue itself remains genuinely unfixed** -- the resolve mechanism
  was proven via a controlled test on asset_management specifically because forcing insurance's
  real historical fix through would have required fabricating data to clear an unrelated,
  legitimate FQC block. This is a real, honest open item, not a gap in the ticket system.
- **`--kind ops` inherits the same `BOOKKEEPING_TARGET="duckdb"` limitation** `run_cli.py`'s
  intake/validate paths already have and document -- a Databricks-driven ops run's own audit
  trail would still need this generalized.
