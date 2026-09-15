# Jarvis — Contract-Driven Agentic Data Platform

A platform-agnostic data platform built by an agent team under an Agentic SDLC.

## The one idea that makes this work

**Agents are build-time, not run-time.**

Agents read *contracts* and emit *deterministic code*. The code is committed to Git,
reviewed by a human, and executed by the platform. No LLM sits in the data path.

    contracts/  →  [agent team]  →  generated code  →  platform runtime  →  evidence/
     (YAML)         (build time)      (in Git)          (no LLM)          (audit)

This is what makes the whole thing cheap, reproducible and auditable. If an agent
"loads the data", you cannot reproduce a run, cannot cost-control it, and cannot
pass a governance review.

## Platform agnosticism

Agnosticism comes from the contract + emitter layer, not from asking the model to
remember Snowflake vs Databricks syntax each time.

| Concern        | Tool            | Why                                              |
|----------------|-----------------|--------------------------------------------------|
| Ingestion      | `dlt`           | source/destination agnostic, schema evolution     |
| Transformation | `dbt-core`      | adapters for Databricks/Snowflake/Fabric/DuckDB   |
| SQL dialects   | `sqlglot`       | transpile the few hand-written bits              |
| Data quality   | `soda-core`     | landing-zone + layer checks, YAML-defined         |
| Local runtime  | `duckdb`        | full medallion locally, $0                        |

Swapping platform = swapping `contracts/platform/<target>.yaml`. Nothing else.

## Layers

- `contracts/`  — the ONLY source of truth. Hand-authored or agent-drafted, human-approved.
- `emitters/`   — contract → platform code. Deterministic templates, not prompts.
- `generated/`  — emitted dbt models, dlt pipelines, soda checks. Committed, reviewed.
- `harness/`    — DuckDB local runner + synthetic seed data.
- `evidence/`   — gate approvals, decision log, run records. The audit trail.
- `.claude/agents/` — the agent team definitions (portable to deepagents).

## Control plane

Every layer writes to `_control`:

- `run_registry`   — run_id, phase, layer, start/end, status, row counts
- `file_audit`     — file name, size, row count, column count, checksum, arrival time
- `dq_results`     — check name, layer, entity, severity, passed/failed, observed value
- `load_lineage`   — source entity → target entity, run_id, watermark

Every bronze/silver row carries `_run_id`, `_source_file`, `_ingested_at`, `_record_hash`.

Alerting reads `_control` from **outside** the platform (see docs/alerting.md) —
Databricks Free Edition restricts outbound internet, so webhooks from inside notebooks
are unreliable.

## Phases

| Phase | Scope                                   | Gate            |
|-------|-----------------------------------------|-----------------|
| P0    | Foundations, contracts, harness, team   | Intent          |
| P1    | Source → Bronze (catalogue-driven)      | Architecture    |
| P2    | Bronze → Silver (model-driven)          | Architecture    |
| P3    | Silver → Gold (semantics-driven)        | Architecture    |
| P4    | Dashboards + test packs                 | Validation      |
| P5    | Port DuckDB → Databricks Free Edition   | Validation      |

Each phase runs the full 10-stage SDLC. See `docs/agentic-sdlc.md`.

## Quickstart

    pip install -r requirements.txt
    python harness/seed/generate.py          # synthetic source files
    duckdb harness/jarvis.duckdb             # inspect
