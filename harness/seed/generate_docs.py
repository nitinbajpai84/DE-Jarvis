"""Generate synthetic unstructured documents for the policy_docs source.

Mirrors generate.py: same idea as the orders seeder, but for the unstructured
contract (contracts/sources/policy_docs.source.yaml). Entities are fabricated —
this is the ONLY document set that is ever allowed to touch Databricks (Phase 5).
Real confidential documents stay local and are never promoted past the DuckDB harness.
"""
from __future__ import annotations

import pathlib, random
from datetime import date, timedelta

random.seed(7)
OUT = pathlib.Path(__file__).resolve().parents[1] / "landing" / "demo" / "unstructured" / "policy_docs"
OUT.mkdir(parents=True, exist_ok=True)

COUNTRIES = ["Singapore", "United Kingdom", "Germany", "India"]
NAMES = ["Northwind Trading Pte Ltd", "Blue Harbor Holdings", "Vantage Re Group", "Solstice Partners"]

TEMPLATE = """POLICY SCHEDULE (SYNTHETIC TEST DOCUMENT - NOT REAL DATA)

Policy Number: {policy_no}
Effective Date: {eff_date}
Counterparty: {counterparty}
Counterparty Country: {country}

This is a synthetic fixture generated for Jarvis Phase 5 platform validation.
No real counterparty, policy, or financial data is contained in this document.

Coverage Summary
-----------------
Line of business: General Liability
Limit: {limit} SGD
Deductible: {deductible} SGD

Notes
-----
Synthetic document {idx} of the test corpus. Safe to upload to any environment.
"""


def make(idx: int) -> None:
    d = date(2026, 1, 1) + timedelta(days=random.randint(0, 300))
    text = TEMPLATE.format(
        policy_no=f"POL-{idx:08d}",
        eff_date=d.isoformat(),
        counterparty=random.choice(NAMES),
        country=random.choice(COUNTRIES),
        limit=f"{random.randint(1, 50)}0,000",
        deductible=f"{random.randint(1, 20)},000",
        idx=idx,
    )
    (OUT / f"policy_{idx:04d}.txt").write_text(text)


for i in range(1, 31):
    make(i)

# One deliberately broken fixture: near-empty file -> should trip char_count warn rule
(OUT / "policy_broken_0001.txt").write_text("n/a")
print(f"Wrote {len(list(OUT.glob('*.txt')))} synthetic documents to {OUT}")
