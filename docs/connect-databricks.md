# Connecting to Databricks Free Edition

Do this once. It takes about ten minutes. Put every value in `.env`, never in a contract,
never in a notebook, never in a chat message.

## 1. Account
Sign in at https://login.databricks.com/ . If you had a Community Edition account, it is
gone — Community Edition was retired at end of 2025 and those accounts are inaccessible.
Sign up for Free Edition instead.

## 2. Personal access token  ->  DATABRICKS_TOKEN
User menu (top right) -> **Settings** -> **Developer** (under User) ->
**Access Tokens** -> *Manage* -> **Generate new token**.
Copy it immediately; it is shown once. Starts with `dapi`.

## 3. Workspace host  ->  DATABRICKS_HOST
The domain in your browser address bar, e.g. `dbc-b82471fc-21d3.cloud.databricks.com`.

## 4. SQL warehouse HTTP path  ->  DATABRICKS_HTTP_PATH
**SQL Warehouses** in the left nav -> open **Serverless Starter Warehouse**
(Free Edition does not let you create new warehouses — use the one provided) ->
**Connection details** tab -> copy **HTTP Path**, e.g. `/sql/warehouses/723b706a90da12d4`.
The **Server hostname** on that tab should match step 3.

## 5. Catalog  ->  DATABRICKS_CATALOG
**Catalog** in the left nav -> **+** -> name it `jarvis`, Type = **Standard**,
leave *Use default storage* checked -> Create.
Then create schemas: `bronze`, `silver`, `gold`, `control`.

## 6. Volume (needed for the unstructured source)
Inside `jarvis.bronze`, create a **Volume** named `landing`. Unstructured files live here;
bronze stores metadata plus extracted text, never the binary.

## 7. Verify
    pip install databricks-cli
    databricks configure --token        # or just export DATABRICKS_HOST / DATABRICKS_TOKEN
    databricks catalogs list

    dbt debug --target databricks

## Free Edition constraints to design around
- Serverless compute only. No custom clusters, no init scripts, no compute-scoped libraries.
- No Scala or R in notebooks.
- **Outbound internet is restricted to a limited set of trusted domains** — do not fire
  Slack webhooks from inside a notebook. Alerting runs outside (see `alerting.md`).
- One workspace, one metastore. No account-level APIs.
- Daily fair-use quotas: if you blow the quota, compute stops until reset. Keep test
  volumes small — a few hundred thousand rows is plenty to prove the framework.
- Non-commercial use only.
