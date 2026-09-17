# Human test guide: Foundation First, 2026-09-17

**Site:** https://jarvis-control-room.vercel.app

**Logins:** use your three accounts: admin (all companies), Star Insurance (insurance), Star Investments (asset_management).

For each objective, try the steps, compare with **You should see**, and score it:

| Score | Meaning |
|---|---|
| 0 | not there |
| 1 | there but not usable |
| 2 | usable with issues |
| 3 | meets the objective |

The limits listed under each objective are already known, so you don't need to report them.

**Tip on speed:** the **duckdb** target is fast but mostly empty on the live server. **databricks** holds the real insurance data; its first load takes ~25 s, then ~15 s.

---

## 1. Light, professional product; Workspace for admins only

**Try**
1. Sign in as Star Insurance.
2. Look through each screen in the left rail.
3. Sign out and sign in as admin.

**You should see**
- A light theme with the layered data mark.
- Star Insurance opens on **Connect & explore**, with no Workspace step and only the insurance domain.
- Admin sees the extra **Workspace** step and both domains.

---

## 2. Company isolation

**Try**
1. As Star Investments, look at every screen and the team panel.

**You should see**
- Only asset_management data: estate, landing zone, sources, proposals, memories and advice.
- No legacy `bronze` / `silver` / `gold` schemas in its estate scan (those are admin-only).

---

## 3. Discovery at enterprise scale: estate scan and report

**Try**
1. As Star Insurance, go to Connect & explore, then **Estate** (target: databricks).
2. Click **Run scan** (tier 3 is quicker; tier 4 adds model descriptions and takes a few minutes).
3. Open **Findings**.

**You should see**
- Coverage, and each of the four tiers with what it found.
- Open questions phrased for a person to answer, such as "8.0% of policy.agent_id values reference no agent … originates at source".
- Findings: references (intact / orphans), sensitive columns, quality hotspots, mirrored tables, readiness against intent, and what each table appears to be.

**Known limits:** row counts use `count(*)` per table, which is fine at this scale but not for 40,000 tables.

---

## 4. Landing zone by company, then kind of source

**Try**
1. Go to Connect & explore, then **Connect a source**, then the **Landing zone** panel.

**You should see**
- Four columns: structured, unstructured, api, database. Each source shows its file count and size.
- The path pattern `landing/<company>/<category>/<source>/`.

**Known limits:** API and database folders only fill when a pipeline pull runs. Uploads appear under their source.

---

## 5. Upload, preview, re-upload, review

**Try**
1. Under **Upload a file**, pick a source id such as `my_claims`, upload a CSV or Excel file, and add a note.
2. Upload a changed version under the same id.
3. In the review panel, try **Reject** without a comment, then with one. Then **Accept** a version.

**You should see**
- **Preview:** the first rows as read, plus the column profile.
- **Diff on v2:** rows, columns added or removed, type changes, empty-value shifts, uniqueness.
- **Effect on intent:** which required data points the new version finds or loses.
- **Reject:** refused without a comment; with a comment, the source rolls back to the previous version.
- **Discovery status:** counts accepted and awaiting-review sources.

**Known limits:** supports CSV, TSV, XLSX (first sheet), TXT, MD and HTML. The diff is computed from a sample of up to 500 rows.

---

## 6. Intent changes and regeneration

**Try**
1. Go to Catalogue & intent and save an intent whose report needs a few data points.
2. Edit it (add one, remove one) and save again.
3. Sign off the current version, then try to sign an older one.

**You should see**
- **Version history:** each save is a version.
- **What changed:** the data points added and removed.
- **What it did to gaps:** now open, now found, no longer needed. Estate matches appear for open points, or "not in the estate scan either".
- **Now out of date:** the architecture record, if it predates the change.
- **Sign-off:** only the current version can be signed.

---

## 7. Gap analysis

**Try**
1. Look at the **Gap report** panel on Catalogue & intent (with an intent captured).

**You should see**
- Tiles showing ready / high / medium counts, and a readiness bar per intent.
- Filter chips for the 8 gap types.
- For each data point: where it was found (contract, profile, estate), each gap with severity, next action and the owning agent.

---

## 8. Reference architecture advice with citations

**Try**
1. On Catalogue & intent, go to **Reference architecture advice**.
2. Pick Azure, ask e.g. "Claims needs a 1 hour RPO, what should we change?", and click **Get advice** (20–40 s).
3. Click **Sources**.
4. On one recommendation, click **Propose…** / **Send to the team**.

**You should see**
- **Recommendations:** each one quotes a passage word for word, with a link to the Microsoft Learn page and section.
- **Why for your company:** citing your records.
- **Confidence:** a level, with a warning listing any product names the sources don't mention.
- **Verification line:** "N of M quotes verified" and how many drafts were dropped.
- **Sources:** the list of published pages.
- **Sending one to the team:** it lands in Approvals.

**Known limits:**
- Sources are a fixed list of 22 published pages, not open web search.
- Quotes are verified; the reasoning isn't. Use confidence and approval.

---

## 9. Agents with a brain, context and memory

**Try**
1. Click **Talk to the team** and pick an agent.
2. Ask e.g. the Data Detective "Which sources are awaiting review?", or the Chief Architect "What does Microsoft recommend for our DR?".
3. Expand **How I answered** on the reply.
4. Tell one agent a decision ("We've decided claims are reported monthly"), then open a new conversation with a different agent and ask about it.
5. Check the **What it sees** and **What it remembers** tabs.

**You should see**
- **Replies:** grounded in your records, with citation ids like [E1] [G1] [R1].
- **How I answered:** model, tokens, time, context used, tools called, sources with links, memories recalled and saved.
- **Team memory:** the decision is recalled by the other agent.
- **What it sees:** the exact context pack.
- **What it remembers:** the conversation summary, plus long-term memories you can delete.

**Known limits:**
- Replies take ~5–20 s.
- There's no delete for conversations yet.
- Agents only read and propose; they never change anything themselves.

---

## 10. Agents propose, people approve

**Try**
1. Ask an agent to make a change, e.g. "Please add paid_amount to the Claims by month report", or "propose assigning ticket 6".
2. Open **Approvals** (from the banner or the team panel).
3. Approve one proposal; decline another with a reason.

**You should see**
- **The proposal:** a card saying exactly what approving would do, why, and which records it relies on.
- **Banner:** "N changes proposed by the team" appears on every screen.
- **Approve:** the change is made under your name.
- **Decline:** needs a reason, and the agent remembers it.
- **Re-deciding:** refused.

**Known limits:** only six kinds of change can be proposed; everything else is a recommendation.

---

## 11. The rest of the journey (built earlier, still working)

- **Data engineering:** medallion flow with live table and row counts per layer, and agent runs.
- **Test & visualise:** per-layer test pack (can take ~2 minutes on Databricks) and a dashboard from live gold data.
- **Operations:** alerts, daily digest, and the incident ticket loop.

---

## What the automated checks already covered

`harness/smoke_live.py` ran 65 checks against this deployment (report: `evidence/runs/smoke-live-2026-09-17.md`), and the unit suite passes (90 tests) on both DuckDB and Databricks. The UI was rendered screen by screen and tab by tab locally, with no script errors.

What automation can't judge, and why your testing matters:
- whether the advice and answers are actually good
- whether the screens make sense to a data leader
- whether this is enough to count as meeting each objective
