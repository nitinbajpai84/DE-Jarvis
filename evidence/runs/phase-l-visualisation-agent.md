# Evidence: Phase L -- Agent 6, visualisation

**Date:** 2026-09-16
**Goal:** Agent 6's real capability -- the piece of the blueprint that had zero capability
behind it as of Phase K's summary.

## What was actually there already, found by reading the code, not assumed

The gold contract schema (`$schema: jarvis/gold/v1`) has carried a `dashboards:` field since
before this session started. `contracts/semantics/insurance.gold.yaml` already has one real,
hand-authored dashboard: 4 tiles (two bars, a KPI, a table), left over from the
`gold_model_compiler.py` this project's Phase A migration replaced. Grepped `emitters/`,
`webapp/`, `agents/` for every reference to `dashboards`: **nothing anywhere renders it.**
`asset_management`'s contract carries `dashboards: []` because the current
`intake_compiler.py` always emits an empty list -- no workbook sheet feeds it. So this phase is
two things, not one: complete the rendering wiring this project already half-built, and give
Agent 6 a real way to propose a dashboard for a domain that has none.

## Design: reuse the existing schema, invent nothing

`emitters/dashboard.py`'s `propose_dashboard()` derives tiles mechanically from a domain's own
declared metrics and marts -- 0 grain dims -> kpi, 1 dim -> bar, 2+ -> table, plus one full-mart
table per mart -- in the *exact same tile shape* insurance's hand-authored dashboard already
uses. A proposal is structurally identical to a saved dashboard; nothing about "what a tile
looks like" was invented for this phase.

The one place real judgement was needed: rolling a **non-additive** metric (a ratio like
`loss_ratio = claims_paid / NULLIF(earned_premium, 0)`) up to a single KPI number. Summing
per-group ratios is mathematically wrong. Rather than invent a different formula, the fix
substitutes `SUM(dep)` for each dependency name *inside the metric's own declared expression
string* -- the same formula the contract already asserts is correct, evaluated at zero grain
instead of the mart's grain. Verified this is safe by reading `gold_transform.py`'s own
mart-builder: a derived metric's dependency columns are guaranteed present in the built mart
table even when the contract's own `mart.metrics` list doesn't enumerate them (confirmed
against the real table: `mart_loss_ratio_by_line` carries `earned_premium` as a live column
though the contract's `metrics: [claims_paid, loss_ratio]` never names it). If a dependency
genuinely isn't in the mart, the tile returns an honest `error` field -- never a fabricated
number.

## Two real bugs caught by looking at actual output, not trusting the first render

**1. Naive selection instead of aggregation.** The first render of insurance's own hand-authored
bar tile (`written_premium` by `line_of_business`) showed "auto" three times with three
different values -- because the backing mart (`mart_premium_by_product`) is grained by
`line_of_business` AND `transaction_type`, two dimensions, while the tile only wants one. Fixed
by GROUP BY-ing the tile's chosen dimensions (with the same non-additive substitution rule
where needed) rather than trusting the mart was already at exactly the tile's grain.

**2. An orphan-FK corroboration, not fabricated.** Both `written_premium` and `claim_count`
bar charts show a real `(unmapped)` bucket -- 6 rows in `mart_premium_by_product` with a NULL
`line_of_business`, worth $145,726 in premium and dozens of claims. Verified this isn't a
rendering artifact: it's the exact downstream consequence of the orphan-FK finding from Phase J
(quarantined `policies_2.csv` policies) propagating through the `fact -> dim_policy ->
dim_product` join chain that supplies `line_of_business`. The dashboard makes a Phase J finding
*visible* to a human in a way a test-pack pass/fail count doesn't -- which is the actual point
of Step 04 visualisation.

## What was built

- **`emitters/dashboard.py`** -- `propose_dashboard()`, `get_dashboard()` (existing spec or a
  fresh proposal, never silently prefers one over the other), `render_dashboard()` (resolves
  every tile against live gold data), `save_dashboard()` (writes an accepted/edited proposal
  into the gold contract's `dashboards:` field -- a field the schema has always had).
- **Two new ungated Agent 6 tools**: `render_dashboard_tool`, `save_dashboard_tool`.
  `ALL_TOOLS`: 17 -> 20.
- **`webapp/backend/dashboard.py`** + 2 routes (`GET`/`POST /api/dashboard`) -- same
  call-the-real-function pattern as every other Step 02/04 module.
- **Real SVG bar charts, KPI tiles, and tables** on the Step 04 screen, following the dataviz
  skill: a 6-step categorical palette validated against this app's own chart surface
  (`node validate_palette.js ... --surface "#131A28"` -- all 5 checks pass, not eyeballed), a
  per-mark hover tooltip on every bar, a legend for multi-series charts, tabular-nums in tables.
- **`gates.py`'s stale gap text fixed**: it still said "the visualisation agent do not exist"
  after this phase gave it real capability -- caught by rereading the rendered screen, not by
  remembering to update it.

## Verification -- against real data, both platforms, through the actual browser

Insurance's existing dashboard, all 4 tiles, rendered live: `written_premium` and `claim_count`
bars grouped correctly by `line_of_business` (with the real `(unmapped)` bucket), the
`loss_ratio` KPI showing **81.0%** (a real cross-mart rollup via the substitution formula,
identical value `0.8102278835280529` on duckdb and `0.8102278830495188` on a real Databricks
warehouse -- the tiny float-precision difference is the two engines' own arithmetic, not a bug),
and the `mart_loss_ratio_by_line` table with per-column currency/percent formatting resolved
from each column's own metric declaration (a real formatting bug -- discovered by reading the
actual rendered numbers, not assumed correct because the KPI tile worked -- fixed by attaching a
`formats` list per column instead of only formatting the last one).

Asset_management's proposal: 3 tiles, mechanically derived, zero errors, matching the domain's
real 6-portfolio data. Clicked **Accept & save this proposal** for real in the running browser;
confirmed the write landed in `contracts/semantics/asset_management.gold.yaml`'s `dashboards:`
field (kept as a real deliverable); confirmed the badge flipped to "Saved to contract" and
`render_dashboard_tool` (the agent's own path, called directly) picked up the saved version
instead of re-proposing.

**A UI bug caught and fixed during this testing, same class as a Phase G/H bug already fixed
once**: `saveDashboardUI()` wrote a success message then called the full `loadDashboardUI()`,
which unconditionally cleared `#dashResult` -- wiping the message the instant it appeared.
Fixed by splitting the fetch/render logic so a post-save refresh updates the badge and grid
without touching the message. Verified the fix: the message now persists alongside the updated
"Saved to contract" badge.

## An honest note on stray data found while reviewing `git status` before committing

Reviewing untracked files before staging turned up six files not created by any action
described above: a blank `contracts/intent/insurance/intent.yaml` and
`contracts/architecture/insurance/architecture.yaml` (every field empty, `captured_by: human`),
five redundant `evidence/runs/<run>-validation.json` packs for insurance (all showing the same
real 6-failing result already documented in Phases J/K), and one genuinely new, correct,
valuable artifact: a live-agent G3 **pass** for `asset_management`
(`evidence/gates/ad9a1d44-G3.md`, 12 cases, 0 failing) that was never explicitly narrated in
Phase K's evidence, which focused on insurance.

Investigated rather than guessed: the blank intent/architecture files carry no agent-authored
content and no meaningful values -- almost certainly an empty form accidentally submitted while
navigating between screens during earlier testing in this session, not agent invention and not
real customer input. Deleted both, since keeping them would misrepresent insurance as having
captured intent/architecture when it has none. The five redundant validation packs added no
new information beyond what Phase K already committed -- deleted as clutter, not as wrong data
(every one of them was factually correct). The genuine `asset_management` G3 pass was kept: its
content is real, correct, and additive -- proof the live-agent G3 path also produces a clean
*pass*, not only the refusal Phase K documented for insurance. Confirmed via `evidence/gates/`
that no incorrect *passing* record exists for insurance despite whatever produced these files --
the refusal logic held every time it was actually exercised.

## Regression

25/25 on duckdb and Databricks. `agents_loader.GATE_INTERRUPTS` unchanged.

## Known gaps, stated plainly

- **The `(unmapped)` label is used for two different kinds of null** -- a null dimension value
  (an orphan FK, the real Phase J finding) and a null metric value (e.g. a ratio's
  divide-by-zero-protected NULL) render identically, though they mean different things. Found
  while reviewing the loss-ratio table's last row; not fixed in this phase -- a labeling
  refinement, not a correctness bug (no number is ever fabricated either way).
- **Agent 6 doesn't draft a dashboard conversationally.** It proposes one mechanically from
  declared metrics -- there's no reasoning step where it asks a human what matters most, or
  explains its choices beyond the tile shapes themselves.
- **No React/Node front end**, per the original blueprint's literal wording. This phase built
  real, live, interactive charts as a screen within the existing Control Room instead of a
  separate app stack -- a deliberate scope call (stated, not hidden) to stay consistent with
  everything else built in this project rather than introduce a second, disconnected frontend
  technology.
- **A domain can only have one saved dashboard** (`dashboards:` is always replaced wholesale,
  never appended to) -- a richer multi-dashboard domain is future work.
