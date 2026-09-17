# Phase Q — admin-only legacy schemas, Step 01 review loop, intent-change regeneration

Date: 2026-09-17

## 1. Unclassified schemas are admin-only

**Decision (user):** schemas that belong to no domain are visible to admins only.

**Before:** they appeared in both tenants' scans. Star Investments could see the legacy insurance `bronze`, `silver` and `gold` table names, row counts and column profiles.

**Now:**
- `estate_scan` has a `scope` column: `domain`, or `domain+unclassified`.
- Scope comes from the logged-in account, never from the request.
- A company's scan covers only its own schemas. The two scopes are stored as separate scans, so a company never reads an admin scan, whether as the latest scan or by id.
- The migration is check-then-alter (Databricks has no `ADD COLUMN IF NOT EXISTS`). It was applied to real Databricks, and existing scans were backfilled as `domain+unclassified`.

**Closed in passing:** a report requested by `scan_id` was looked up by id alone. Any login could read another domain's scan by id. The lookup now requires the domain, target and scope to match.

**Live checks:**

| Login | Action | Result |
|---|---|---|
| Star Investments | new scan | 3 own schemas, 7 tables, no legacy schemas |
| Star Investments | before scanning | sees no scans |
| Star Insurance | request admin scan `9b449500a1e0` by id | `scan: null` |
| Admin | list scans | both scopes |

**Found in live testing, fixed:** the first Discovery load after a restart returned a 502 on `/api/estate/scans`.
- *Cause:* two parallel requests ran the schema setup and migration together, and DuckDB raised "Catalog write-write conflict on alter".
- *Reproduced:* 9 of 15 trials with 6 threads.
- *Fix:* a lock with a re-check brought it to 0 of 15, and a regression test was added.

## 2. Step 01 review loop

Every source profile is now a version (`emitters/versions.py`, `contracts/discovery/<domain>/.history/<source>/vNNNN.json`); nothing is overwritten.

- **Upload:** CSV, TSV, TXT or MD, into `harness/landing/uploads/<domain>/<source>/<upload_id>/`. Each version keeps its own file. Other types are refused with a reason.
- **Preview:** the first 20 rows exactly as sampled, stored with the profile.
- **Diff:** compared against what was on record, meaning the last version that wasn't rejected. It covers rows, columns added or removed, type changes, empty-value shifts of 5 points or more, uniqueness gained or lost, and extracted fields for documents.
- **Intent impact:** gap analysis runs in memory with the old and new columns. It reports data points each version newly satisfies or breaks, and ignores any still satisfied elsewhere.
- **Decide:**
  - Accept signs the version off.
  - Reject needs a reason, and the source rolls back to the last version that wasn't rejected.
  - Rejecting every version removes the live profile, so gap analysis stops counting the source.
- **Status:** Discovery status counts accepted and awaiting-review sources.
- **Existing profiles:** a profile from before versioning becomes v1, labelled "baseline", on its next change.

Tenancy gaps closed:
- A company could profile or test another tenant's landing files by typing their path. File paths are now confined to the company's own uploads, or to landing paths its own source contracts name.
- Source and intent ids were used as file paths unchecked. They are now validated against traversal.

## 3. Intent change

Every intent save is a version. The change view shows:
- **What changed:** fields, SLA, reports and per-report data points added or removed, and definitions added, removed or reworded.
- **What it did to gaps:** newly open, newly found, no longer needed and still open, with open counts before and after.
- **Estate leads:** for each open data point, exact-name matches in the latest estate scan the viewer may read, or an explicit "not in the estate scan either".
- **Now out of date:** the architecture record, if it was captured before the change.
- **Sign-off:** only the current version can be signed. An older one returns 409.

"Saved by" now comes from the login rather than from the page.

## Verified

| Check | Result |
|---|---|
| New tests: `tests/test_review_loop.py` | 8 |
| New tests: `tests/test_estate.py` | 5 (company scope, cross-domain id, migration, concurrency) |
| Full suite, DuckDB target | 62 passed |
| Full suite, Databricks target | 62 passed |
| Mutation check | Comparing against a rejected version, instead of the last one on record, fails `test_reject_rolls_back_and_accept_signs_off` |

**Local UI check:**
- Two uploads showed their diff and preview.
- Reject without a comment was refused. Reject with a comment rolled back to v1, and accept v1 then succeeded.
- An intent saved twice showed its diff and gap delta (`broker_code` "not in the estate scan either"), and sign-off worked.
- The only console errors were the deliberate 400 and 409.
- The test data was removed afterwards.

**Live on Railway:**
- **Guards:** a cross-tenant path returned 403, a test without a domain 400, an upload into another tenant 403, `.xlsx` 400, and an `../x` id 400.
- **Parallel first load:** 200 on all three requests.
- **Full loop as admin in sandbox domain `zz_smoke`:**
  - Upload v1, then v2. The diff showed `claim_amount` and `loss_date` added and `amount` removed, rows 3 → 4, and no false "location changed". The impact showed `loss_date` found and `amount` missing.
  - Reject without a reason was refused. With a reason, the source rolled back to v1. Accept v1 succeeded.
  - An intent revised to v2 gave the diff and gap delta, plus a note that no Databricks estate scan exists for the domain. Signing v2 succeeded.
  - Cleanup: v1 was withdrawn (no live version remains) and the intent was deleted. The version history stays as the audit trail.

## Honest limits

- Uploads profile CSV, TSV, TXT and MD only. Excel must be saved as CSV first.
- Row counts and uniqueness in the diff come from the sample of up to 500 rows, not the whole file.
- Intent regeneration is mechanical: exact-name gap analysis, estate lookup and a staleness flag. It does not rewrite contracts or the architecture record itself. It tells a human what to re-check and records the re-sign.
- History files are append-only and are never pruned.
