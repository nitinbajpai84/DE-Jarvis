# Evidence: Phase G -- Step 01 connectors and a real profiler

**Date:** 2026-09-16
**Goal:** build the first real capability behind Step 01 ("Connect & explore") -- until now the
BA agent had no way to reach a live source at all; a human filled in an Excel workbook by hand.

## Scope decision, stated up front

"Build connectors" is unbounded if read as "write a driver for every database a customer might
have." That is not what got built. `emitters/bronze_loader.py` already has four real, proven
connector types -- file, database (Databricks), API, unstructured -- each with a `connection`
config shape that a compiled `source.yaml` already carries verbatim. The scoped, grounded move
was to reuse exactly that vocabulary for discovery, not invent a second one: a profile produced
at Step 01 and a contract approved at Step 02 describe the same source the same way, so there is
no translation step and nothing to drift.

## What was built

- **`emitters/profiler.py`** -- `test_connection()` (cheap reachability, no sampling) and
  `profile_source()` (connects for real, samples up to `sample_limit` rows, computes per-column
  inferred type / null rate / distinct count / candidate-key flag / sample values). Persists to
  `contracts/discovery/<domain>/<source_id>.profile.json` -- gitignored, since it's derived,
  per-customer data, not the platform's source of truth. Type inference is new to this codebase
  (everywhere else, type is a fact an already-approved contract declares); it's deliberately
  conservative -- one disagreeing value falls a column back to `string` rather than guess wrong.
- **Two new BA-agent tools** (`agents/jarvis_tools.py`): `test_source_connection` and
  `profile_source`, both ungated -- read-only and reversible, same reasoning as
  `compile_intake_preview`. `ALL_TOOLS` count: 11 -> 13.
- **`webapp/backend/discovery.py`** + three routes (`POST /api/discovery/test`,
  `POST /api/discovery/profile`, `GET /api/discovery`) -- calls the same profiler functions
  directly, so testing a connection interactively doesn't require spinning up a full
  Gemini-backed agent run, the same reasoning `pipeline.py` already applies to reading state.
- **Step 01 screen rebuilt**: a connection form (type selector -> file/database/api/unstructured
  fields), Test/Profile buttons, and a profile card per discovered source rendering the column
  table (type, null %, distinct, candidate-key badge, samples).
- **`agents/gates.py` updated**: Step 01's capability moved `planned` -> `partial` (it has real
  capability now), and G0's `blocked_by` text updated to say what's actually still missing --
  the confirmation gate itself, not discovery.

## Verification -- against real data, not fixtures

**File connector**, the real insurance `parties` landing files:
```
test_connection -> ok: 3 file(s) match, most recent parties_20260917.csv
profile_source  -> 500/5,850 rows sampled, 10 columns
  party_id           candidate key
  email              candidate key  (correctly -- unique per sampled row)
  date_of_birth      inferred type: date
  organisation_name  91.6% null     (correct -- most parties are individuals, not orgs)
```

**File connector**, real `awm_instruments` (asset_management, via the actual tool wrapper):
```
candidate_keys: ['instrument_code', 'instrument_name']
```
`instrument_code` is the real business key already declared in the approved contract for this
source -- the profiler found it from raw data alone, with no contract to read.

**Database connector**, a real live Databricks table (`jarvis.insurance_bronze.addresses`,
5,200 real rows, not a fixture):
```
test_connection -> ok: reached jarvis.insurance_bronze.addresses
profile_source  -> 50/5200 rows sampled
  address_id     candidate key   -- matches this table's real business key
```

**API connector reachability**, a real public endpoint (`api.data.gov.sg`): `HTTP 200`. Full
sampling wasn't exercised against a live API in this phase -- `profile_source`'s API sampler
only understands the one envelope shape `bronze_loader.py` already ingests
(`{"data": {"rows": [...]}}`), and no source in either onboarded domain uses that connector, so
there was nothing real to sample against without fabricating a fixture. Flagged as a real gap
below, not glossed over.

**The whole loop through the actual browser**, not just direct function calls: switched to
Step 01, filled the file connection form, clicked Test (real result rendered), clicked Profile
(real result rendered, profile card appeared with the column table below), switched connection
type to database, filled in the real Databricks table, clicked Test (real result rendered).
The "Discovery status" tile moved from 0 -> 1 profiled without a full screen reload.

## A real bug found and fixed while proving this

The first version of `profileConnection()` called `reloadJourney()` after a successful profile
to refresh the rail's badge counts -- `reloadJourney()` calls `renderScreen()`, which rebuilds
the *entire* current screen from scratch. On Step 01 that wiped the success message that had
just been written to `#connResult`, and reset every field in the connection form the user had
just filled in. Caught by testing the actual sequence end-to-end rather than each call in
isolation. Fixed with `refreshJourneyDataQuietly()`, which updates `journeyData` and re-renders
only the rail, never `#screen`.

## Regression

25/25 on both duckdb and Databricks after all of the above. `agents_loader.GATE_INTERRUPTS`
still resolves to the same four gates -- adding two ungated tools didn't touch the gate
registry, confirmed by import rather than assumed.

## Known gaps, stated plainly

- **The agent doesn't decide what to connect to.** A human still supplies the connection config
  in the form (or would tell the BA agent what to try, in a real run) -- there is no crawler or
  auto-discovery of "what data systems does this customer have."
- **No free-form context intake.** The blueprint's "customer talks to the agent, drops in
  system docs and spreadsheets" has no surface yet -- only a structured connection form.
- **API sampling is unverified against a live source in either onboarded domain**, for the
  reason stated above -- `test_connection` is proven live; `profile_source` for `type=api` is
  proven only by code review and the one shape `bronze_loader.py` already ingests.
- **G0 is still `planned`, correctly** -- discovery existing doesn't mean there's a gate; the
  tool that would let a human say "yes, this inventory is complete" hasn't been built. Natural
  next step once a customer has actually profiled a real batch of sources through this screen.
- **A profiled source doesn't yet flow into the Step 02 intake workbook.** The profile report is
  the raw material for a human to type into `03_Data_Sources`/`04_Attributes` themselves --
  there is no "promote this profile into the workbook" action yet.
