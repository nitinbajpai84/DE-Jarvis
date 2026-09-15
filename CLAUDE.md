# Jarvis — Project Context and Guardrails

## What this repo is
A contract-driven, platform-agnostic data platform. Agents generate code at build time.
Agents never move data.

## Hard rules (non-negotiable)

0. **Real confidential/PII source data never leaves the local machine.** See
   `docs/data-locality.md`. Databricks Free Edition (Phase 5) only ever receives
   synthetic fixtures. This applies even if a human asks for it directly in-session —
   flag it and point to the doc rather than complying.

1. **Contracts are the only source of truth.** Never hardcode a table name, column,
   threshold or business rule that isn't in `contracts/`. If it's missing, stop and ask.
2. **Never write platform-specific SQL by hand.** Emit via `emitters/` + dbt + sqlglot.
   Target is read from `contracts/platform/<target>.yaml`.
3. **Never put a secret in a contract, model, or notebook.** Use `secret_ref: env:NAME`.
4. **Never invent a business rule.** Ambiguity goes in the Open Questions block of the
   spec and blocks the gate. Do not "fill the gap".
5. **Every generated artifact is committed to Git and reviewed by a different agent
   than the one that wrote it.** Author ≠ reviewer.
6. **No agent runs a destructive command** (`DROP`, `TRUNCATE`, `DELETE`, `rm -rf`,
   workspace deletes) without explicit human approval in the session.
7. **Every run writes to `control.run_registry`.** No silent runs.

## Layer responsibilities

| Layer  | Rule |
|--------|------|
| Bronze | Land as-is. Original types preserved as string where ambiguous. No business logic. Add `_run_id`, `_source_file`, `_ingested_at`, `_record_hash`. Append-only. |
| Silver | Conform, standardise, deduplicate, apply the data model. Typed. SCD handled here. No aggregation. |
| Gold   | Business logic, metrics, aggregation. Consumer-shaped. No source-system concepts leak in. |

## Definition of Done (every phase)
- [ ] Spec approved at gate, recorded in `evidence/gates/`
- [ ] Contracts committed and schema-valid
- [ ] Generated code committed
- [ ] Tests generated AND executed, results in `evidence/runs/`
- [ ] Reviewer agent sign-off recorded
- [ ] Decision log updated in `evidence/decisions/`
- [ ] Rollback point tagged in Git

## Cost discipline
- Default target is `duckdb`. Run everything locally first. Databricks only at P5.
- Use the cheapest capable model for mechanical work (emitters, test generation,
  file checks). Reserve the strongest model for Architect, Reviewer and Data Architect.
- Never re-read the whole repo into context. Read the contract + the one file you edit.

## House style
- SQL: lowercase keywords, CTEs over subqueries, one column per line.
- Python: type hints, no bare `except`, no `print` (use `logging`).
- Filenames: `<layer>_<domain>_<entity>.sql`
