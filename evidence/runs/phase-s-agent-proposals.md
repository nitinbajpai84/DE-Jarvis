# Phase S — agents propose changes, people approve them

Date: 2026-09-17

## What changed

Agents still cannot change anything themselves. The new `propose_change` tool files a proposal (`emitters/proposals.py`). A proposal is a typed record: what would be done, the exact parameters, the rationale, and the evidence ids it relies on. It does nothing until a person approves it. Approval runs it through the same functions the Control Room's own buttons call, under the approver's name.

### Kinds of proposal (a closed list)

| Kind | What approval does |
|---|---|
| `add_intent_data_points` | Adds data points to a report, or creates the intent. A new intent version is recorded and still needs sign-off. |
| `update_architecture` | Sets RTO, RPO, platform binding, layering, volumes, the SCD type for one entity, or adds a risk. |
| `run_estate_scan` | Starts a scan. |
| `review_source_version` | Accepts a source version, or rejects it (a comment is required). |
| `assign_ticket` | Assigns an incident ticket. |
| `recommendation` | Only records agreement. Nothing runs. |

### Safeguards

- **Checked when filed:** unknown kinds, unexpected or missing parameters, wrong types, and targets that don't exist (intent, source version, ticket, SCD type) are refused. The refusal goes back to the model, which is told to correct its parameters.
- **Intents by id or name:** an intent given by name resolves to its id when there is exactly one match. Ids are matched against the intents on record, never opened as paths.
- **Frozen preview:** the preview of what approval would do is stored when the proposal is filed. Rebuilt later, an approved "add loss_date" read "Add nothing new".
- **Decided once:** a second decision returns 409. A failed apply is recorded as `failed`, never `approved`.
- **Declines teach:** declining needs a reason, and the reason becomes a correction the proposing agent recalls.
- **Approver's scope:** scan scope comes from the approver, never the proposal. `include_unclassified` is not a parameter, so an agent in a company's conversation cannot widen a scan to admin-only schemas.
- **Agents see decisions:** recent proposals and decisions are in every agent's context (section P).
- **UI:** a proposal appears inline under the reply that filed it, in an Approvals tab in the team panel, and in the attention bar on every screen, with a count on "Talk to the team".

## Found on real Gemini runs, fixed

1. **Intent ids missing from context.** The Delivery Lead filed `intent_id="ZZ proposal check"`, was refused, then narrated its confusion at length.
   *Fix:* ids are shown in the context, names resolve to ids, and agents are told to retry quietly and never narrate tool errors. On the rerun it filed correctly and replied in one sentence.
2. **No current date.** The Data Detective called a scan from that morning "from the future".
   *Fix:* the date and time are in the prompt.
3. **Wrong things saved to memory.**
   - A person's request ("run a tier 4 scan") was remembered as a team decision.
   - The agent's rebuttal was stored as a "correction" with a genuine quote from the person attached.

   *Fix:* requests are excluded from memory, and a memory's content must mostly be the person's own words, checked mechanically.
4. **Several ids in one bracket.** `[M4, P1]` only recorded M4.
   *Fix:* every id in the bracket is now recorded.

## Verified

**Tests:** 84 passed on DuckDB, 84 on Databricks.
- `tests/test_proposals.py` (6):
  - filing-time checks, including a path-shaped intent id and a smuggled scope parameter
  - approval runs under the approver's name, and the intent still needs sign-off
  - decide-once
  - declines teach the proposing agent, and only that agent
  - a failed apply is recorded as failed
  - scan scope comes from the approver
  - an agent files a proposal from a conversation, and malformed parameters are refused back to it
- `tests/test_agent_brain.py`: added memory-content grounding and multi-id citations.

**Local, with real Gemini and the real UI:**
- The Night Watch proposed assigning ticket 6. The card appeared inline, and the attention bar and badge went from 1 to 2.
- Approve in the Approvals tab assigned the ticket, and the card changed to "approved by local".
- A decline with no note was refused (400). With a note, it was saved as a correction, and the Data Detective raised it when asked again, citing [M4].
- Local test data was removed afterwards, and ticket 6 was restored to open and unassigned.

**Live on Railway**, as admin in the sandbox domain `zz_smoke`:
- The Chief Architect proposed an RTO and RPO (preview "Create the architecture record: set rto = '4 hours', rpo = '1 hour'"). The record was null before approval, and after approval held those values, `captured_by: jarvis`.
- Star Investments listing or deciding `zz_smoke` proposals got 403.
- The Delivery Lead filed a recommendation. Declined with a reason, it became a correction in its memory.

## Honest limits

- Kinds are deliberately few. Anything else can only be a recommendation that a person acts on.
- An approved intent change still needs its own sign-off (the intent-change flow). Approval is not sign-off.
- Proposals can't be withdrawn or edited. They are declined and re-filed instead.
- The gated SDLC agent graph is still separate.
