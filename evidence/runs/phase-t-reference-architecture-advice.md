# Phase T — reference architecture advice with verified citations

Date: 2026-09-17

## What it does

On Catalogue & intent, **Reference architecture advice** asks the Chief Architect for recommendations for the company's cloud (Azure, AWS or Google Cloud), optionally answering a specific question. Each recommendation comes with:
- the quoted passage(s) it rests on, with page title, section, publisher, link and fetch date
- why it matters for this company, citing its own records (architecture A1, estate E1, gaps G1, sources S1, landing zone L1)
- a confidence level, lowered when a named product or feature isn't in the cited sources (those names are listed)
- a button to send it to the team as a proposal. It files an architecture update or a recommendation through the existing approval flow, carrying its citations as evidence.

In chat, the Chief Architect has a `search_reference_architectures` tool. The passages it cites are verified and listed with links under "How I answered".

## How it stays honest

| Step | Mechanism |
|---|---|
| Corpus | 22 published pages: Microsoft Learn (Azure Databricks), Databricks docs (AWS and GCP), AWS Well-Architected analytics lens, Google Cloud Architecture Center and Well-Architected Framework. Reduced to the article (page chrome dropped), split by section (about 900 characters), embedded, and stored in the control plane with fetch dates. |
| Cloud scoping | Each page is tagged with the cloud it applies to, and an Azure company only gets Azure pages. Tagging comes from the registry, so re-tagging a page takes effect immediately. |
| Retrieval | One query per topic (layering, history, recovery, governance, quality, ingestion, cost), sharpened with the company's facts (its RTO/RPO, sensitive columns, broken references, source kinds). At most two passages per page per topic. |
| Quotes | A citation counts only if its quote (6+ words) is in the named passage, ignoring case and punctuation. A recommendation with no verified citation is dropped, and the count and reason are shown. |
| Names | Product and feature names the cited pages (text, title, section) and the company's records never mention are flagged, and confidence drops to low. Leading verbs, role names, plurals, generic acronyms and acronym expansions are handled so the flag stays meaningful. |
| Proposals | Must pass the proposal kind's validation and carry no placeholder values ("to be defined"). |
| Corpus upkeep | Admin-only refresh, weekly by default. Per-page failures are recorded and earlier passages kept. Pages removed from the registry are retired. |

## Found by reading what was fetched and generated, then fixed

1. **Google page gone.** Google's "analytics lakehouse" page now only explains how to delete that deployment. Fetching returned 200 and extraction worked; only reading the passages showed it was useless. Replaced with DR scenarios for data, the reliability and privacy pillars, and Databricks-on-GCP pages.
2. **Missing titles.** Databricks page titles were blank because docs put the article `h1` in a `<header>`, which the extractor skipped as chrome. Fixed, and `og:title` is now preferred (Google's `h1` also carries button text).
3. **Stale cloud tag.** A page re-tagged in the registry kept its old cloud in stored rows, so GCP advice could cite an AWS page. Filtering now reads the registry.
4. **Empty advice on the live server.** Advice through the server came back with "drafted 0, parse_error 1". The parser took everything between the first `[` and the last `]`, and a reply mentioning `[E1]` first broke it. It now prefers the fenced JSON block, decodes at each `[`, and retries once.
5. **Quote real, claim not.** A verified quote didn't stop "Auto Loader" and `_rescued_data` being recommended from a passage that never mentions them. The name check was added, then tuned on real output, where it had flagged "Engineering Team", "Implement Lakeflow", "Undefined RTO", "Streaming Tables" and "SQL".
6. **Placeholder proposal.** A suggested proposal set RTO to "To be defined by business". Placeholders are now refused.

## Verified

**Tests:** `tests/test_reference_arch.py` has 7 tests.
- Extraction drops chrome and keeps sections.
- Refresh records failures, doesn't re-fetch fresh pages, scopes by cloud, and retires dropped pages.
- Verification rejects invented, too-short and unknown-reference quotes, drops unknown fact ids, flags unsupported names and refuses placeholders.
- End to end: advice becomes a proposal with its citations as evidence, and one company can't read another's advice.
- Reply parsing survives citations before the JSON.
- The name check ignores verbs, roles, plurals and acronym expansions.

A mutation check confirmed the verification test fails when the name check is removed. Full suite: 90 passed on DuckDB, 90 on Databricks.

**Local, with real Gemini:**
- Two advice runs for insurance on Azure: 13 of 13 and 8 of 9 quotes verified, with links to the Microsoft Learn pages and sections.
- "Propose updating the architecture record" filed a proposal and raised the Approvals badge.
- The Chief Architect in chat searched Azure DR guidance, cited R1 and R2 (both verified), and filed an RPO proposal (retrying quietly after a refused parameter).
- Local test data was removed afterwards.

**Live on Railway:**
- **Refresh:** a Star Insurance login gets 403. As admin, all 22 pages were fetched in 24 s with 0 failures.
- **Advice:** Star Insurance on Databricks target, Azure, question "Claims data needs an RPO of 1 hour": 39 s, 5 recommendations, 5 of 7 quotes verified, 2 drafts dropped for unmatched quotes, 1 placeholder proposal refused. This advice record remains in Star Insurance's panel as real advice.
- **Tenancy:** Star Investments reading insurance advice gets 403.

## Honest limits

- **Fixed source list:** the corpus is a curated registry of 22 pages, not open web search. Adding a source is a registry change, which keeps every citation to a known publisher.
- **What the checks can't prove:** quote and name checks show the source says what is quoted, and flag names it doesn't mention. They can't prove the reasoning connecting the passage to the recommendation is sound. That is what the confidence level and the human approval step are for.
- **Speed:** advice takes 20–40 s. The first advice for a cloud whose pages were never fetched also fetches them (about 25 s more).
