"""Day-3 landing batch -- a third snapshot for the same 6 dimension-bearing sources
generate_insurance_day2.py covers, written under a new date-stamp so bronze's idempotency
check treats it as genuinely new work (not a re-run of an already-loaded file). Exists purely
to demonstrate a live bronze -> silver -> gold run against the Jarvis Control Room: a fresh,
smaller set of attribute changes per source, same shape as day-2's, different random sample.
"""
from __future__ import annotations

import csv
import pathlib
import random
from datetime import date

random.seed(317)
# landing/<domain>/<category>/<source_id>/ -- see emitters/landing.py
LANDING = pathlib.Path(__file__).resolve().parents[1] / "landing" / "insurance" / "structured"
TODAY = date(2026, 9, 17)


def _read_clean_rows(name: str, filename_prefix: str) -> tuple[list[str], list[dict]]:
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
changed = random.sample(rows, 150)
for r in changed:
    r["email"] = r["email"].split("@")[0] + ".d3@" + r["email"].split("@")[1]
_write("parties", header, changed)

# ---------------------------------------------------------------- dim_customer (scd2)
header, rows = _read_clean_rows("customers", "customers_1")
changed = random.sample(rows, 120)
RISK = ["low", "medium", "high", "very_high"]
LIFECYCLE = ["prospect", "active", "inactive", "lapsed", "former"]
for r in changed:
    r["risk_tier"] = random.choice(RISK)
    r["lifecycle_stage"] = random.choice(LIFECYCLE)
_write("customers", header, changed)

# ---------------------------------------------------------------- dim_policy (scd2)
header, rows = _read_clean_rows("policies", "policies_1")
changed = random.sample(rows, 120)
for r in changed:
    r["policy_status"] = random.choice(["renewed", "cancelled", "expired", "active"])
_write("policies", header, changed)

print("\ndone -- run harness/run_bronze.py for parties/customers/policies to land the day-3 batch.")
