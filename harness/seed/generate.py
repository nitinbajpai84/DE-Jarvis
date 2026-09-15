"""Generate synthetic landing files for the DuckDB harness.

Produces a clean baseline plus deliberately broken files so the Test Manager agent
has real negative cases: a short file (row-count dip), a dropped column (schema drift),
duplicates, and a bad date format.
"""
from __future__ import annotations

import csv, hashlib, pathlib, random
from datetime import date, datetime, timedelta

random.seed(42)
OUT = pathlib.Path(__file__).resolve().parents[1] / "landing" / "orders"
OUT.mkdir(parents=True, exist_ok=True)

COLS = ["order_id","customer_id","order_date","order_ts","status","currency","gross_amount","country"]
STATUS = ["NEW","PAID","SHIPPED","CANCELLED"]
CCY = ["SGD","USD","EUR","INR"]
COUNTRY = ["SG","US","DE","IN"]


def row(i: int, d: date, bad_date: bool = False) -> list:
    ts = datetime.combine(d, datetime.min.time()) + timedelta(minutes=random.randint(0, 1439))
    od = ts.strftime("%Y-%m-%d") if bad_date else ts.strftime("%d/%m/%Y")  # contract says %d/%m/%Y
    return [
        f"ORD{i:08d}",
        f"CUST{random.randint(1, 500):05d}",
        od,
        ts.isoformat(sep=" "),
        random.choice(STATUS),
        random.choice(CCY),
        f"{random.uniform(5, 2500):.2f}",
        random.choice(COUNTRY),
    ]


def write(name: str, rows: list, cols: list = COLS) -> None:
    p = OUT / name
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"{p.name:<34} rows={len(rows):<6} cols={len(cols)} "
          f"sha={hashlib.sha256(p.read_bytes()).hexdigest()[:12]}")


start, n = date(2026, 9, 1), 0
for day in range(7):                               # 7 clean days
    d = start + timedelta(days=day)
    rows = [row(n + i, d) for i in range(random.randint(400, 600))]
    n += len(rows)
    write(f"orders_{d:%Y%m%d}.csv", rows)

d = start + timedelta(days=7)
write(f"orders_{d:%Y%m%d}.csv", [row(n + i, d) for i in range(60)])          # dip -> file check fail
n += 60

d = start + timedelta(days=8)                                                # schema drift
rows = [row(n + i, d)[:-1] for i in range(450)]
write(f"orders_{d:%Y%m%d}.csv", rows, COLS[:-1]); n += 450

d = start + timedelta(days=9)                                                # dupes + bad dates
rows = [row(n + i, d, bad_date=(i % 10 == 0)) for i in range(400)]
rows += rows[:25]
write(f"orders_{d:%Y%m%d}.csv", rows)
