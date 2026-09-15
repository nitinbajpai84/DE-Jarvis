# Data locality — where real data is and isn't allowed to go

## Decision
Real, sensitive/confidential source data (anything with `classification: confidential`
or `pii` in its source contract) **never leaves the local machine**. It is processed only
by the DuckDB harness (`harness/jarvis.duckdb`). It is never uploaded to Databricks Free
Edition — not via managed volume, not via external volume.

## Why not an external volume pointing at local storage
Unity Catalog external locations bind to cloud object storage (S3 / ADLS / GCS) through a
storage credential. There is no external-location type that mounts a local filesystem —
Databricks compute has no path back to your machine. An external volume backed by your
own cloud bucket is a real privacy improvement over a Databricks-managed volume (you hold
the keys), but it is still cloud storage, and standing it up on Free Edition means
configuring your own cloud account and IAM. Not worth it for a $0 test project.

## What actually happens instead
| Phase | Runs on | Data used |
|-------|---------|-----------|
| P1-P4 | DuckDB harness, local machine | Real source data, including confidential/PII sources |
| P5    | Databricks Free Edition       | Synthetic data only — same shape, fake entities |

The Databricks phase exists to prove the **pipeline**, not to process real records. If a
source contract is `classification: confidential` or `pii`, its Phase 5 test fixture MUST
be synthetic. This is enforced by convention here, not by tooling — treat it as a hard rule
at the Architecture Gate for any phase touching Databricks.

## If real data on Databricks ever becomes a genuine requirement
That's a different, larger decision — proper storage credential, workspace network
config, access review — and does not belong in a Free Edition test project. Revisit only
if this moves toward a paid workspace with an actual security review.
