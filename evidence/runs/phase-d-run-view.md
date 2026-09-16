# Evidence: Phase D -- a live Run view over the real Freeze gate

**Date:** 2026-09-16
**Goal:** make Phase C's proven gate mechanism actually usable by a human through the Control
Room, not just a script and a terminal.

## What was built

- `agents/run_cli.py` -- standalone `start`/`resume` subcommands. Agent invocation is real LLM
  time (30s-2min+), far too long to hold an HTTP request open for, so this is what a background
  OS process runs; also directly runnable by hand.
- `webapp/backend/agent_runs.py` -- the API-facing layer: `start_run()` launches `run_cli.py`
  via `subprocess.Popen` and returns immediately; `list_runs()`/`get_run_detail()` read
  `control.sdlc_run`/`sdlc_stage_run`, the same poll-the-database pattern the rest of the
  Control Room already uses. Inspecting a **paused** run's pending action doesn't parse
  anything this module wrote -- it reconnects the exact `SqliteSaver` checkpointer and
  `thread_id` from `agents_loader.py` and calls the compiled graph's own `get_state()`, which
  returns langgraph's real `StateSnapshot.interrupts`. Live truth from the paused graph itself.
- New routes: `POST /api/agent-runs` (start), `GET /api/agent-runs`, `GET /api/agent-runs/{id}`,
  `POST /api/agent-runs/{id}/approve`, `POST /api/agent-runs/{id}/reject`.
- Frontend: a new "Live agent runs" panel, and a "Run via Agent" button next to the existing
  manual "Approve & write" button in the upload list. A paused run renders a gate card showing
  the exact pending tool call and its args, with Approve/Reject buttons.
- `agents_loader.build_team()` gained a `checkpointer=` override so a caller can reconnect to
  an *existing* paused run's exact state instead of only ever starting a fresh one --
  reassigning `.checkpointer` on an already-compiled graph isn't a documented, reliable
  operation, so it has to go in at construction time.

## Verification: the full loop, through the real UI, with real files on disk

Uploaded the intake workbook, clicked **Run via Agent** in the actual browser, watched the new
panel update live, clicked **Approve** in the actual browser. Confirmed via the real API and
the filesystem, not just the UI's own claim:

```
contracts/sources/example_domain/ did not exist before approval
POST /api/agent-runs/{id}/approve
-> stages: [specify: completed, freeze: completed ("wrote 4 contract files for domain='example_domain'")]
contracts/sources/example_domain/parties.source.yaml now exists
```

## A real gate-bypass scare, root-caused rather than shrugged off

The first live-UI run **appeared** to skip the gate entirely: it went from "running" straight
to "completed" with contracts already written, and I never clicked Approve. Before reporting
anything as working, this got investigated properly rather than assumed fine:

1. Inspected the paused thread's full checkpoint history directly (`get_state_history`) --
   confirmed `interrupts=True` genuinely appeared in the trace at the exact point
   `write_intake_contracts` was requested. The interrupt *did* fire.
2. Reproduced the identical `agents_loader.build_team(gated=True)` call path, in isolation, on
   a fresh thread -- it paused correctly (`'__interrupt__' in result: True`, nothing written).
   So the gate configuration itself was not the problem.
3. Reproduced via the **exact** subprocess path (`agent_runs.start_run()`, matching what the
   UI actually triggers) while the webapp server was **also running** (as it was during the
   live UI test) -- this is where a bug was found, but not the one first suspected: a background
   run's `subprocess.Popen` had `stdout=DEVNULL, stderr=DEVNULL`. A run that failed before ever
   reaching `start_sdlc_run()` would leave **zero trace** -- not a failed status, no row at all.
   Fixed immediately: every background process now logs to `harness/agent_run_logs/<run_id>.log`.
4. With logging fixed and the webapp server **stopped** (removing DuckDB cross-process
   contention as a variable), the identical subprocess path paused correctly and stayed paused
   until an explicit, separate `resume_run()` call completed it.

**Conclusion, stated plainly:** the gate mechanism itself is correct -- proven twice more, on
top of Phase C's own proof. What actually happened during the live-UI test was **DuckDB's
single-writer-per-file model under my own aggressive concurrent polling** (manual `curl` checks
layered on top of the frontend's own poll, both hitting `harness/jarvis.duckdb` while a
background subprocess was also writing to it) -- not a broken interrupt. A real,
reproducible `IOException: File is already open in <other pid>` was hit directly while
diagnosing this. Mitigated, not eliminated (DuckDB's concurrency model is what it is):
- `webapp/backend/agent_runs.py`'s own DB access now retries with jittered backoff, and caches
  `ensure_control_schema` per (process, schema) the same way `agents/jarvis_tools.py` already
  does -- the same two fixes proven under a 10-thread stress test in Phase C, now applied here
  too, not just where the bug first showed up.
- The frontend's agent-runs poll interval was widened from 6s to 15s, reducing overlap
  likelihood during a live run.

This is the honest way to read this finding: not "the gate has a bug," but "background
subprocess + a web server + an LLM in the loop + DuckDB's single-writer file model is a genuine
combination worth being careful with," documented here so it isn't rediscovered the hard way
later.

## Regression

Full 25-test suite on both duckdb and Databricks after every change in this phase -- 25/25 on
both, checked again after the concurrency fixes specifically.

## Known gaps

- No Reject-path UI test yet (the button exists and calls the real endpoint, but the actual
  "model receives a rejection and explains why" loop hasn't been watched end-to-end).
- `sdlc_run.domain` is set once at start (from `domain_hint`) and never updated once the real
  domain is discovered by `compile_intake_preview` -- cosmetically wrong (shows `unknown` or
  whatever hint was passed, not the true domain) until the run completes and the stage detail
  reveals it. Not a safety issue, just imprecise bookkeeping, not fixed in this phase.
- Multi-agent delegation (PM -> BA/architect -> DE-fork -> Test Manager) still hasn't been
  proven live through this UI -- every real run so far has been the PM calling the two intake
  tools directly. Phase E's onboarding run is the natural place to finally exercise that.
