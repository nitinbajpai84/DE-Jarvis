# Gate Record

Run:              ca83cae1-588d-4c22-bf28-6d8f913cee69
Gate:             G4 Go-live readiness
Domain:           insurance
Date:             2026-09-16T22:42:33.413020+00:00
Approver (human): via Control Room approval (langgraph interrupt resume)

## Decision
APPROVED

## What was approved
- Target platform: duckdb
- End-to-end run: 12 steps, all clean
- Sources exercised: addresses, agents, claims, customers, parties, payments, policies, policy_coverages, premiums, products
- Layers: bronze: 10 tables / 42160 rows, silver: 10 tables / 41489 rows, gold: 4 tables / 118 rows

## Rollback
Contracts are versioned in git; re-running is idempotent and domain-scoped.


## How this was enforced
The agent graph paused before this tool ran and could not proceed without a human
decision. The evidence above was re-derived by the tool itself, not taken from the
model's own summary.
