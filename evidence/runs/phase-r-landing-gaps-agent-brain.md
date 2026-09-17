# Phase R — landing zone by company, estate-backed gap report, agents with a brain and memory

Date: 2026-09-17

## 1. Landing zone: company first, then kind of source

**Layout:** `harness/landing/<domain>/{structured,unstructured,api,database}/<source_id>/`, with uploads under `<source>/uploads/<upload_id>/`, outside any contract's glob. Quarantine follows the same split: `harness/quarantine/<domain>/<source_id>/`.

**Land, then load:** API pulls now land the raw response JSON, and database pulls a CSV extract, in their landing folder before bronze reads them. Previously both went straight from the source into bronze.

**Excel:** structured sources can be `.xlsx`, read from the first sheet and rendered like its CSV export. Uploads accept CSV, TSV, XLSX, TXT, MD and HTML.

**Migration:** `emitters/landing.py migrate`, run on every container start.
- A folder is moved only when a source contract names the company that owns it. Unowned folders are left in place and reported.
- It rewrites contract `path` and `quarantine_path` values and live profile paths, keeping comments.
- It is idempotent: a second run moves nothing.

Results:

| Where | Result |
|---|---|
| Local | 13 folders moved, 13 contracts + 1 profile rewritten, 13 quarantine folders moved |
| Local, not moved | `vi_customers`, `vi_products` quarantine folders (no contract owns them) |
| Seed landing (ships in the image) | Restructured |
| Railway volume (from the container log) | 13 folders + 2 sandbox uploads moved, 13 contracts + 1 profile rewritten, 13 quarantine paths rewritten |

Live checks:
- `/api/landing` shows each company its own zone. Star Investments requesting insurance's zone gets 403.
- A test connection against the new claims path matched 2 files.
- A bronze run against the moved files found them (files seen, and skipped because already loaded).

**Incident caught before it mattered:** Python's `write_text` on Windows gave `docker-entrypoint.sh` CRLF line endings, and `railway up` uploads the working copy, not git. It was found from a git warning. The file was converted to LF and redeployed within the minute, and the CRLF build never went live.

## 2. Estate-backed gap report (`emitters/gap_report.py`, `/api/gaps`)

Every intent data point is checked against three sources: approved contracts (including column-level `pii`), live discovery profiles (with review state), and the latest estate scan the login may read. It uses eight gap types:

| Type | Owner |
|---|---|
| missing | The Data Detective |
| not_onboarded | The Data Detective |
| unreviewed | The Delivery Lead |
| quality | The Quality Guardian |
| integrity | The Superstar Data Engineer |
| conflicting_copies | The Chief Architect |
| sensitive_uncontrolled | The Chief Architect |
| definition_conflict | The Delivery Lead |

Each gap carries its evidence, severity, owning agent and next action. The G1 gap analysis is unchanged.

**Precision fix found by its own test:** the estate scan now records which business columns differ between diverged copies (`mirror_diff_columns`, migrated). Before, a table-level "diverged" verdict flagged every column of the table.

**On real local data**, with a temporary intent that was removed afterwards:
- `agent_id` showed integrity, high: 8.0% of values reference nothing.
- `broker_code` showed missing.
- `email` and `date_of_birth` were ready, because their contracts mark them `pii`.

## 3. Agents with a Gemini brain, context layer and memory

The **Talk to the team** panel holds all seven personas. Each reply goes through these steps:

| Layer | What it does | Module |
|---|---|---|
| Short-term memory | Conversation stored per login, company and agent; last 8 messages verbatim, older ones summarised | `emitters/agent_sessions.py` |
| Context | Rebuilt every turn from the company's records, per agent, budgeted, with citation ids | `emitters/agent_context.py` |
| Long-term memory | Facts, decisions and preferences shared by the team; corrections kept per agent; recalled by embedding similarity | `emitters/agent_memory.py` |
| Brain | `gemini-2.5-flash`, 5 read-only tools, at most 4 tool rounds | `emitters/agent_brain.py` |
| Trace | Model, tokens, timings, context ids, citations (unverified flagged), tools, memories recalled and saved | returned and stored with each reply |

**Recall cut-off (0.6), calibrated on real `gemini-embedding-001` vectors:** related questions scored 0.62–0.85 against their memory, unrelated ones 0.41–0.58.

### Problems found by running against real Gemini, each fixed and covered by a test

1. **Stale "facts":** the agent remembered its own finding ("no estate scan has been run") as a company fact.
   *Fix:* a memory must carry a quote of the person's words, checked mechanically against their message.
2. **Document-drafting voice:** its build-time charter made it draft "FR-001" requirements in markdown and claim it would "capture" them.
   *Fix:* the charter's deliverable sections are excluded, and the prompt forbids claiming writes.
3. **Memory siloed per agent:** a decision told to the Data Detective was unknown to the Delivery Lead.
   *Fix:* facts, decisions and preferences are team memories.
4. **Hallucinated citation:** the Delivery Lead cited [M1] while saying it remembered nothing.
   *Fix:* every cited id is checked against the context, recalled memories and tool results, and unverified ones are shown.
5. **Thinking-token overhead:** a memory extraction call spent 494 of 569 output tokens thinking.
   *Fix:* utility calls run with thinking budget 0 (74 tokens).
6. **Markdown shown raw:** Gemini writes it despite instructions.
   *Fix:* the panel renders a safe subset from escaped text.
7. **Memory timestamps:** memories showed "8h ago" (timezone).
   *Fix:* sent with an explicit Z.

### Live on Railway

| Check | Result |
|---|---|
| Star Insurance asks the Data Detective about the landing zone and missing data points | 4.8 s; grounded [L1]; says honestly that no intents are captured [G1][I1]; no unverified citations |
| Star Investments reads or chats as insurance | 403 |
| Admin reads Star Insurance's conversation | 404 (conversations belong to one login) |
| Admin tells the Chief Architect a decision; the Night Watch is asked in a new conversation | Saved as team memory M1; recalled and cited [M1] |
| Cleanup | Test memory deleted |

## Tests

| File | New tests |
|---|---|
| `tests/test_landing.py` | 4 |
| `tests/test_gap_report.py` | 3 |
| `tests/test_agent_brain.py` | 7 (fake model and deterministic embeddings, so they test the machinery, not the prose) |

Full suite: 76 passed on DuckDB, 76 passed on Databricks.

## Honest limits

- **Agents only advise.** They read and remember; they cannot change contracts, approve gates or start runs. The gated SDLC agent graph (`agents_loader.py`) is still separate and still starts only from an intake workbook or a validation run.
- **Model and latency:** conversation uses Gemini 2.5 Flash. Turns take roughly 5–13 s, most of it model time.
- **No conversation delete:** there is no API to delete a conversation yet. Memories can be deleted.
- **Legacy letters not on the live volume:** the underwriting letters were added to seed landing. The live volume only seeds when empty, so its unstructured zone is still empty until someone uploads.
- **Web scraping:** there is no scraper. The unstructured category is where scraped content will land.
