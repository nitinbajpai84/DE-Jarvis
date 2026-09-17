# Live smoke test: 2026-09-17

**Command:** `harness/smoke_live.py` (credentials from environment variables).
**Targets:** Railway backend and Vercel frontend.

## Scope

- **Company logins:** read-only, apart from one tier-1 estate scan.
- **Writes:** go to the admin sandbox `zz_smoke` and are cleaned up.
- **Never started:** pipeline runs, background agent runs and ticket scans.

## Run history today

| Run | Result | What it found | Fixed by |
|---|---|---|---|
| 1 | 65/65 | Databricks journey ~60 s per load (125 s for two); flow 23 s; alerts 16 s | `ensure_control_schema` cached per process on remote engines; medallion flow reduced to 2 round trips per layer. Journey now ~12 s, flow ~4 s, alerts ~3 s, with identical results. |
| 2 | 62/65 | Agent refused a proposal because an older approval for a deleted intent of the same id was in its history | Proposals shown as dated history; prompt rule; unique intent per run; new check reproducing the case |
| 3 | 64/66 | Agent used "Claims by month report" for "Claims by month", and approval silently created a second report; agent answered about a look-alike intent | Report names must match an existing report (or `new_report`); agents act on the exact id named |
| 4 | 64/67 | Prompt rule alone didn't hold: agent again read an old approval as current | Each past approval in the context pack is checked against current records ("in effect now" / "NOT in effect now: …") |
| 5 | **67/67** | — | — |
| 6 | **67/67** | Confirmation run (the checks involve a live model, so one clean run isn't enough) | — |

Unit tests: 92 passed on DuckDB, 92 on Databricks.
UI: every screen, Discovery tab and team-panel tab rendered locally with no script errors.

The detailed table below is from run 6.

---


Backend `https://jarvis-backend-production-dfb3.up.railway.app` · frontend `https://jarvis-control-room.vercel.app`

**67 passed, 0 failed** of 67 checks.

| Area | Check | Result | Time | What it saw |
|---|---|---|---|---|
| 1 Platform & sign-in | frontend served by Vercel with this build's features | PASS | 0.5s | all 7 feature markers present; identical to repo index.html: True |
| 1 Platform & sign-in | backend serves the page shell | PASS | 0.4s | ok |
| 1 Platform & sign-in | API refuses a request with no login (401) | PASS | 0.4s | 401 |
| 1 Platform & sign-in | admin sees every domain | PASS | 0.4s | jarvis -> * |
| 1 Platform & sign-in | Star Insurance is scoped to insurance | PASS | 0.4s | starinsurance -> ['insurance'] |
| 1 Platform & sign-in | Star Investments is scoped to asset_management | PASS | 0.4s | starinvestments -> ['asset_management'] |
| 1 Platform & sign-in | targets list duckdb and databricks | PASS | 0.4s | ['databricks', 'duckdb'] |
| 1 Platform & sign-in | company journey hides the admin Workspace step | PASS | 15.7s | admin 6 steps, company 5 |
| 2 Tenancy | Star Investments blocked: /api/estate/report | PASS | 0.5s | 403 |
| 2 Tenancy | Star Investments blocked: /api/landing | PASS | 0.4s | 403 |
| 2 Tenancy | Star Investments blocked: /api/discovery | PASS | 0.4s | 403 |
| 2 Tenancy | Star Investments blocked: /api/gaps | PASS | 0.4s | 403 |
| 2 Tenancy | Star Investments blocked: /api/proposals | PASS | 0.4s | 403 |
| 2 Tenancy | Star Investments blocked: /api/agents/detective/context | PASS | 0.4s | 403 |
| 2 Tenancy | Star Investments blocked: /api/memory | PASS | 0.4s | 403 |
| 2 Tenancy | Star Investments blocked: /api/architecture/advice | PASS | 0.4s | 403 |
| 2 Tenancy | Star Investments blocked: /api/intents | PASS | 0.4s | 403 |
| 2 Tenancy | Star Investments blocked: /api/tickets | PASS | 0.3s | 403 |
| 2 Tenancy | company cannot profile another company's landing files | PASS | 0.4s | 403 |
| 2 Tenancy | company cannot refresh the shared reference corpus | PASS | 0.3s | 403 |
| 3 Estate scan & report | insurance has a completed Databricks scan | PASS | 2.0s | scan 6af616d85df9 tier 4: 24 tables, 16 orphan refs, 10 questions |
| 3 Estate scan & report | company report never shows admin-only legacy schemas | PASS | 2.0s | scope=domain, no unclassified schemas |
| 3 Estate scan & report | tier-1 scan starts, runs and completes (asset_management) | PASS | 18.5s | 482e688f8b9b: 7 tables, tier 1, scope domain |
| 4 Landing zone | insurance landing zone is company/category/source | PASS | 0.4s | 10 structured sources under landing/insurance/structured/ |
| 5 Connect, profile & review | test connection: public API (data.gov.sg) | PASS | 1.1s | HTTP 200 |
| 5 Connect, profile & review | test connection: insurance's own landing files | PASS | 0.4s | 2 file(s) match, most recent claims_2.csv |
| 5 Connect, profile & review | upload CSV creates v1 awaiting review, with preview rows | PASS | 0.4s | v11 pending, 3 preview rows |
| 5 Connect, profile & review | re-upload creates v2 with diff against v1 | PASS | 0.4s | v12 vs v11: +['claim_amount', 'loss_date'] -['amount'] |
| 5 Connect, profile & review | reject needs a reason; with one, rolls back to v1 | PASS | 0.7s | live version back to v11 |
| 5 Connect, profile & review | accept signs a version off | PASS | 0.4s | v11 accepted |
| 5 Connect, profile & review | Excel upload lands as structured and profiles its columns | PASS | 0.6s | columns ['policy_id', 'premium', 'start_date'] |
| 5 Connect, profile & review | unsupported file type refused with a reason | PASS | 0.3s | 400 |
| 5 Connect, profile & review | profile a live API source (data.gov.sg rainfall) | PASS | 0.6s | 4 columns, 89 records at $.data.stations, v6 |
| 6 Intent, changes & gaps | capture an intent | PASS | 0.7s | smoke-claims-20260917114537 |
| 6 Intent, changes & gaps | revise it: diff and gap delta reported | PASS | 0.8s | v2: +['broker_code', 'loss_date'] -['amount']; newly open ['loss_date', 'broker_code'] |
| 6 Intent, changes & gaps | only the current version can be signed | PASS | 0.7s | v1 refused, v2 signed |
| 6 Intent, changes & gaps | G1 gap analysis runs | PASS | 0.4s | 2 open |
| 6 Intent, changes & gaps | gap report classifies data points | PASS | 0.4s | 1/3 ready; {'loss_date': ['missing'], 'broker_code': ['missing'], 'claim_id': []} |
| 6 Intent, changes & gaps | insurance gap report readable by its company | PASS | 0.4s | ok |
| 7 Architecture | capture an architecture record | PASS | 0.7s | rto 4 hours, rpo 1 hour |
| 7 Architecture | high-level diagram from insurance contracts | PASS | 0.4s | 33 nodes |
| 7 Architecture | low-level diagram from insurance contracts | PASS | 0.4s | 10 nodes |
| 7 Architecture | read an architecture sketch image (Gemini vision) | PASS | 3.8s | {"description": "The image displays a conceptual data architecture with four sequential layers: Landing, Bronze, Silver, and Gold, typically representing stages |
| 8 Reference advice | 22 published sources fetched | PASS | 0.4s | 22/22 ok, 636 passages |
| 8 Reference advice | Star Insurance's stored advice is readable | PASS | 0.4s | 1 advice record(s); latest 5 verified citations |
| 8 Reference advice | new advice: quotes verified against source pages | PASS | 12.8s | 4 recs, 4/5 quotes verified, 1 dropped |
| 8 Reference advice | a recommendation becomes a proposal with citations | PASS | 0.5s | update_architecture: Implement an Active-Active Disaster Recovery Strategy (evidence ['R2', 'A1', 'advice:c2688bb193b5']) |
| 9 Agents: brain, context, memory | seven agents on Gemini | PASS | 0.3s | The Data Detective, The Chief Architect, The Delivery Lead, The Superstar Data Engineer, The Quality Guardian, The Insight Artist, The Night Watch |
| 9 Agents: brain, context, memory | context pack for the Data Detective (insurance) | PASS | 3.3s | S1:313 L1:372 E1:1876 G1:72 I1:23 P1:21 |
| 9 Agents: brain, context, memory | conversation turn with trace (short-term memory) | PASS | 2.4s | 1934ms, cited ['S1'], unverified []: We have three sources: - `smoke_claims` (file): v11 accepted [S1] - `smoke_premiums` (file |
| 9 Agents: brain, context, memory | conversation is resumable; other logins can't read it | PASS | 0.8s | 2 messages stored; company login refused (403) |
| 9 Agents: brain, context, memory | a stated decision becomes team memory, recalled by another agent | PASS | 6.4s | saved M1 (team) by Chief Architect; Night Watch recalled it: The team decided that smoke test data is always kept in the zz_smoke sandbox [M1 |
| 9 Agents: brain, context, memory | catalogue chat (intent) replies | PASS | 2.2s | Hi there! Thanks for reaching out. A monthly claims dashboard sounds like a very useful tool.  To st |
| 10 Proposals & approvals | agent files a proposal from conversation | PASS | 4.6s | add_intent_data_points: Add paid_amount to report “Claims by month” of intent “Smoke claims 20260917114537” (existing report). A new intent version is recorded and needs sign-off. |
| 10 Proposals & approvals | approval runs it under the approver's name | PASS | 0.8s | approved by jarvis; report now needs ['claim_id', 'loss_date', 'broker_code', 'paid_amount'] |
| 10 Proposals & approvals | second decision refused (409); decline needs a reason | PASS | 1.5s | 409 on re-decide, 400 without note, declined with note |
| 10 Proposals & approvals | remove this run's intent | PASS | 0.4s | deleted |
| 10 Proposals & approvals | agent trusts current records over past approvals | PASS | 5.1s | filed add_intent_data_points despite earlier approval history |
| 11 Build, test, operate | medallion flow (insurance, Databricks) | PASS | 5.4s | {"target": "databricks", "domain": "insurance", "layers": {"bronze": {"status": "active", "table_count": 10, "columns":  |
| 11 Build, test, operate | alerts feed | PASS | 2.5s | {"target": "databricks", "domain": "insurance", "failures": [{"source_id": "customers", "phase": "bronze", "status": "co |
| 11 Build, test, operate | incident tickets list | PASS | 1.1s | {"domain": "insurance", "tickets": [{"ticket_id": 6, "domain": "insurance", "client": "default", "phase": "silver", "ent |
| 11 Build, test, operate | per-layer test pack runs (insurance, Databricks) | PASS | 83.3s | {"domain": "insurance", "target": "databricks", "test_run_id": "testpack-59188259305e", "by_layer": {"bronze": [{"id": "bronze:addresses:rows", "layer": "bronze |
| 11 Build, test, operate | dashboard renders from live gold data | PASS | 2.4s | 4 tiles, 0 with errors |
| 11 Build, test, operate | SDLC stage status | PASS | 0.8s | {"agents": [{"name": "business-analyst", "model": "google_genai:gemini-3.1-pro-preview", "description": "Turns business  |
| 11 Build, test, operate | workbook intake uploads list (scoped) | PASS | 0.4s | {"uploads": []} |
| 11 Build, test, operate | agent runs list | PASS | 0.4s | {"runs": []} |
| 12 Cleanup | sandbox cleaned (intent, sources, memories) | PASS | 4.6s | intent deleted; smoke_claims withdrawn; smoke_premiums withdrawn; smoke_rainfall withdrawn; 3 memories forgotten |
