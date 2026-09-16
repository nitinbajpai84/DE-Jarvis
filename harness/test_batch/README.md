# Manual test batch -- "the third set of data"

A small, hand-crafted incremental batch for testing the platform yourself, separate from the
real historical landing files. **Not auto-loaded by anything** -- these sit in
`harness/test_batch/<source>/`, not `harness/landing/<source>/`, so no pipeline run picks them
up until you deliberately copy them over.

## What's in here

Six clean files (new parties, customers, policies, and three asset_management sources) that
reference real, already-loaded IDs where they need to (e.g. every new policy uses a real,
existing `product_id`/`agent_id`), so they should load and flow through silver/gold cleanly.

Two files are **deliberately broken**, named so you can't miss it:

| File | What's wrong | What you should see |
|---|---|---|
| `policies/policies_test_batch_SCHEMA_DRIFT.csv` | Missing the last column (`written_premium`) | Rejected at the file-quality check *before* staging -- quarantined, not loaded |
| `claims/claims_test_batch_ORPHAN_FK.csv` | Every claim references `policy_id = POL999999`, which doesn't exist anywhere | Loads into bronze fine (bronze doesn't check foreign keys), but the **silver-layer test pack** catches it as a real orphan-FK failure -- the same class of check that already found insurance's genuine historical issue |

## How to use it

1. **Copy what you want to test into the real landing zone.** For example, to test the clean
   customer/policy batch:
   ```bash
   cp harness/test_batch/customers/customers_test_batch.csv harness/landing/customers/
   cp harness/test_batch/parties/parties_test_batch.csv harness/landing/parties/
   cp harness/test_batch/policies/policies_test_batch.csv harness/landing/policies/
   ```
2. **Run the bronze loader** for the source(s) you copied in (via the Control Room's "Run via
   Agent" on Step 03, or directly: `python harness/run_bronze.py --source customers`).
3. **Check what happened** on the Operations screen, or by re-running the test pack on Step 04.
4. **To see a real quarantine:** copy in `policies_test_batch_SCHEMA_DRIFT.csv` and reload
   `policies` -- it should be rejected, not silently loaded.
5. **To see a real ticket get raised:** copy in `claims_test_batch_ORPHAN_FK.csv`, reload
   `claims`, rebuild silver, then click "Scan for issues" on the Operations screen -- a new
   ticket should appear for the orphan foreign key, ready to walk through assign -> propose fix
   -> approve (G5) yourself.

## Cleaning up afterward

These are copies -- removing them from `harness/landing/<source>/` and re-running the loader
won't undo an already-successful load (bronze is append-only by design). If you want to fully
reset back to the state this test kit started from, ask for the same wipe-and-reload the
platform verification pass used (see `evidence/runs/phase-n-data-reset.md`).
