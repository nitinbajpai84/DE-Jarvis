"""Day-2 snapshot for the 6 dimension-bearing insurance sources -- a second landing batch with
deliberate changes to a subset of existing business keys, so bronze (append-only) ends up with
two versions of some records. That's the actual precondition for testing SCD conform logic:
without a second, DIFFERENT version of the same key, there's nothing for silver to deduplicate
(SCD1) or version (SCD2) -- a clean single load conforms trivially and proves nothing.

Reads the day-1 landed CSVs, changes tracked_attributes (per
contracts/models/insurance.model.yaml) for a subset of rows, writes a new dated file alongside
the original. Same business key, changed values -- exactly what the SCD1/SCD2 test needs.

  dim_party    (scd1): ~200 of 5500 parties  -- email/phone corrected
  dim_address  (scd1): ~200 of 5000 addresses -- moved (line1/city changed)
  dim_product  (scd1): 10 of 50 products     -- renamed / deactivated
  dim_customer (scd2): ~300 of 5000 customers -- risk_tier/lifecycle_stage reassessed
  dim_agent    (scd2): ~50 of 500 agents     -- status/channel changed
  dim_policy   (scd2): ~300 of 5000 policies -- policy_status changed (renewed/cancelled)
"""
from __future__ import annotations

import csv
import pathlib
import random
from datetime import date

random.seed(99)
# landing/<domain>/<category>/<source_id>/ -- see emitters/landing.py
LANDING = pathlib.Path(__file__).resolve().parents[1] / "landing" / "insurance" / "structured"
TODAY = date(2026, 9, 16)


def _read_clean_rows(name: str, filename_prefix: str) -> tuple[list[str], list[dict]]:
    """Reads only the clean day-1 file(s) (skips the deliberately-bad ones from generate_insurance.py)."""
    rows = []
    header = None
    for path in sorted((LANDING / name).glob(f"{filename_prefix}*.csv")):
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames
            rows.extend(reader)
    return header, rows


def _write(name: str, header: list[str], rows: list[dict]) -> None:
    path = LANDING / name / f"{name}_{TODAY:%Y%m%d}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    print(f"{name}/{path.name:<28} changed_rows={len(rows)}")


# ---------------------------------------------------------------- dim_party (scd1)
header, rows = _read_clean_rows("parties", "parties_2026")
changed = random.sample(rows, 200)
for r in changed:
    r["email"] = r["email"].split("@")[0] + ".updated@" + r["email"].split("@")[1]
    r["phone"] = "+1-555-" + f"{random.randint(1000, 9999)}"
_write("parties", header, changed)

# ---------------------------------------------------------------- dim_address (scd1)
header, rows = _read_clean_rows("addresses", "addresses_2026")
changed = random.sample(rows, 200)
CITIES = ["Springfield", "Fairview", "Riverside", "Georgetown", "Salem", "Madison"]
for r in changed:
    r["line1"] = f"{random.randint(100, 9999)} New Address Ln"
    r["city"] = random.choice(CITIES)
_write("addresses", header, changed)

# ---------------------------------------------------------------- dim_product (scd1)
header, rows = _read_clean_rows("products", "products_2026")
changed = random.sample(rows, 10)
for r in changed:
    r["product_name"] = r["product_name"] + " (Revised)"
    if random.random() < 0.3:
        r["active_flag"] = "False"
_write("products", header, changed)

# ---------------------------------------------------------------- dim_customer (scd2)
header, rows = _read_clean_rows("customers", "customers_1")
changed = random.sample(rows, 300)
RISK = ["low", "medium", "high", "very_high"]
LIFECYCLE = ["prospect", "active", "inactive", "lapsed", "former"]
for r in changed:
    r["risk_tier"] = random.choice(RISK)
    r["lifecycle_stage"] = random.choice(LIFECYCLE)
_write("customers", header, changed)

# ---------------------------------------------------------------- dim_agent (scd2)
header, rows = _read_clean_rows("agents", "agents_1")
changed = random.sample(rows, 50)
for r in changed:
    r["status"] = random.choice(["active", "inactive", "terminated", "suspended"])
_write("agents", header, changed)

# ---------------------------------------------------------------- dim_policy (scd2)
header, rows = _read_clean_rows("policies", "policies_1")
changed = random.sample(rows, 300)
for r in changed:
    r["policy_status"] = random.choice(["renewed", "cancelled", "expired", "active"])
_write("policies", header, changed)

print("\ndone -- re-run harness/run_bronze.py for these 6 sources to land the day-2 batch.")
