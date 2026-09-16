# Evidence: Phase C -- real agent activation with a structural Freeze gate

**Date:** 2026-09-16
**Goal:** make "the agents" real. Before this, `.claude/agents/*.md` were prompt files that had
only ever driven interactive Claude Code roleplay -- `agents_loader.py` existed to headlessly
run them via deepagents/Gemini, but passed it an empty tool list and no gate. The Control
Room's SDLC strip showed ten robots that had never executed anything.

## What was built

- `agents/jarvis_tools.py` -- six `@tool`-wrapped functions calling Jarvis's **real** pipeline
  (`intake_compiler`, `bronze_loader.run`, `silver_transform.run`, `gold_transform.run`, the
  pytest suite) -- not a sandboxed stand-in. Every tool also writes to the new
  `control.sdlc_run` / `sdlc_stage_run` tables (added to `control_plane.py`'s DDL), so a run is
  a durable, queryable object, not just LLM chat history.
- `agents_loader.py`: `build_team()` now wires those tools onto the PM and every subagent, adds
  `interrupt_on={"write_intake_contracts": True}`, a `SqliteSaver` checkpointer persisted to
  `harness/agent_checkpoints.sqlite`, and `FilesystemPermission` rules restricting agent writes
  to `contracts/`, `docs/templates/`, `webapp/uploads/` and explicitly denying anything matching
  `*credential*`/`*secret*`/`*.env`/`*.duckdb`/`*profile*`/`*token*`.
- Only `write_intake_contracts` is gated -- it's the one action that changes the source of
  truth every other tool and the real engine trusts. Running bronze/silver/gold against
  already-approved contracts isn't gated a second time: it's idempotent, domain-scoped (can't
  touch another domain's schema -- `resolve_schema`), and safe to re-run.

## Verification: this is structural enforcement, not a prompt convention

Ran the real PM agent (Gemini 3.1 Pro via `agents_loader`'s existing headless path, no new
credentials) against a live task: preview-compile a workbook, then write its contracts.

**First invocation:**
```
contracts dir exists after first invoke: False
INTERRUPTED as expected. Interrupt payload:
  action_requests: [{'name': 'write_intake_contracts', 'args': {...}}]
  review_configs: [{'action_name': 'write_intake_contracts', 'allowed_decisions': ['approve','edit','reject','respond']}]
```
The graph paused **before** the write ran. Confirmed on disk, not just by the returned state.

**Resuming — a separate `app.invoke()` call, same `thread_id`, simulating an "Approve" click
arriving later from a different process (e.g. the Control Room, Phase D):**
```
result = app.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config)
contracts dir exists after resume: True
files: ['parties.source.yaml']
```
The exact same call that was interrupted actually executed, and only then. This is LangChain's
own human-in-the-loop middleware plus langgraph's checkpoint/resume, not something built from
scratch here -- but wiring it onto Jarvis's real write path and proving it survives a fresh
`invoke()` call (not just a `while` loop in the same process) was the real risk, and it's now
demonstrated, not assumed. Cleaned up afterward (throwaway `verify_gate` domain).

## Two real concurrency bugs found by stress-testing this, not by inspection

The first real run left `sdlc_stage_run` completely empty despite both tool calls succeeding --
`_log()`'s original design deliberately swallowed logging failures so a logging hiccup could
never take down the actual pipeline action, which meant the failure was *fully* silent. Fixed
`_log()` to append unswallowed failures to `harness/sdlc_log_failures.log` first, which
immediately surfaced the real cause:

1. **`log_sdlc_stage`'s `select max(id)+1, then insert` wasn't atomic.** Two close-together tool
   calls could read the same max before either committed. Fixed with a retry-on-conflict loop
   with jittered backoff (not a schema change -- an IDENTITY column needs dialect-specific DDL,
   which this project avoids everywhere else).
2. **`_control_con()` re-ran full DDL (`ensure_control_schema`) on every single tool call.**
   DuckDB does not safely allow concurrent DDL from separate connections to the same file --
   this was the actual root cause of the first failure ("Catalog write-write conflict on
   alter"), not just the ID race. Fixed by caching "already ensured this (target, domain) this
   process" so DDL runs once per process, not once per call -- the real fix, since it also
   removes most of the concurrency window the retry loop exists to cover.

Verified with a direct stress test (10 threads, each opening its own connection like the real
tools do) -- before the fix: 4-8 of 10 failed with catalog conflicts or duplicate-key errors;
after both fixes: **10/10 succeeded, 10 distinct ids, zero entries in the failures log.**

**Regression:** full 25-test suite on both duckdb and Databricks after every change in this
phase -- 25/25 on both throughout.

## Known gaps (not done, not silently claimed)

- Only the single-agent gate mechanism was proven end-to-end with a real model. A full PM ->
  BA/architect -> DE-fork -> Test Manager delegation run (the actual six-stage factory in
  practice, not just the one gated tool) hasn't been run for real yet -- that's the natural next
  proof point, and the likely shape of Phase E's asset_management onboarding.
- `control.sdlc_run` / `sdlc_stage_run` have no reader yet -- nothing in the Control Room
  surfaces them. That's Phase D.
- The six-stage-factory -> ten-stage-`docs/agentic-sdlc.md` mapping used in code comments is
  informational only; no frontend enforces or visualizes it yet.
- `run_regression_tests` is honestly documented as testing insurance regardless of which domain
  an agent is actually working on (the suite isn't parametrized per-domain) -- the tool's own
  docstring says so, so an agent (or a human reading its output) can't mistake it for
  domain-specific coverage.
