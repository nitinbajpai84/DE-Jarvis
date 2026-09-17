"""Synthetic insurance data, adapted from the ACORD-style schema at
D:\\Projects\\InsurancePocGenAI\\act-as-an-enterprise-insurance-data\\001_insurance_analytics_mvp_schema.sql
(core Party/Policy/Coverage/Premium/Payment/Claim entities only -- not that project's
marketing/ML extensions). Backs the 10 sources compiled from
docs/templates/jarvis_data_catalogue_template.xlsx.

ROW COUNTS -- not a uniform 5000 everywhere, and why: customers.party_id and agents.party_id
are each unique per the original schema (one party plays at most one of those roles), so a
5000-row parties table can back AT MOST 5000 customers OR 5000 agents, not both. Rather than
either reuse the same identities for both roles (confusing) or silently inflate every table to
force a round number, the party pool is sized to its two disjoint subsets and dimension-shaped
tables (agents, products) are sized realistically:
  parties 5500 (5000 customer-role + 500 agent-role) | customers 5000 | addresses 5000
  agents 500 | products 50 | policies 5000 | policy_coverages 5000 | premiums 5000
  payments 5000 | claims 5000

INJECTED ISSUES -- deliberate, not hidden, so there's something real to test. Each DQC-target
file is sized to 100+ rows (file_checks.min_rows=100 for every table) so it clears FQC and the
issue is actually caught at DQC, not accidentally rejected earlier for the wrong reason:
  - policies_2.csv:  100 rows, 30 share one duplicate policy_number -> unique/error -> whole
                      batch (100 rows) quarantined
  - claims_2.csv:    100 rows, 25 have negative paid_amount -> range/error -> whole batch
                      quarantined
  - customers_2.csv: 100 rows, 15 have an invalid lifecycle_stage value -> accepted_values/error
                      -> whole batch quarantined
  - payments_dip.csv: 40 rows only (file_checks min_rows=100) -> FQC reject, never staged
  - agents_drift.csv: one column dropped -> FQC schema-drift reject, never staged
Everything else lands clean.
"""
from __future__ import annotations

import csv
import pathlib
import random
from datetime import date, timedelta

from faker import Faker

random.seed(42)
fake = Faker()
Faker.seed(42)

# landing/<domain>/<category>/<source_id>/ -- see emitters/landing.py
LANDING = pathlib.Path(__file__).resolve().parents[1] / "landing" / "insurance" / "structured"
TODAY = date(2026, 9, 15)

N_CUSTOMERS = 5000
N_AGENTS = 500
N_PARTIES = N_CUSTOMERS + N_AGENTS
N_PRODUCTS = 50
N_ADDRESSES = 5000
N_POLICIES = 5000
N_COVERAGES = 5000
N_PREMIUMS = 5000
N_PAYMENTS = 5000
N_CLAIMS = 5000


def _write(name: str, cols: list[str], rows: list[list], filename: str | None = None) -> None:
    out_dir = LANDING / name
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (filename or f"{name}_{TODAY:%Y%m%d}.csv")
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    rel = str(path.relative_to(LANDING.parents[3]))
    print(f"{rel:<42} rows={len(rows):<6} cols={len(cols)}")


def _rand_date(start: date, end: date) -> date:
    return start + timedelta(days=random.randint(0, max(0, (end - start).days)))


# ---------------------------------------------------------------- parties
PARTY_TYPES = ["person"] * 8 + ["organization"] * 2  # 80/20 split, matches typical retail mix
CONTACT_METHODS = ["email", "phone", "sms", "mail", "app"]

party_ids = [f"PTY{i:06d}" for i in range(1, N_PARTIES + 1)]
parties_rows = []
for pid in party_ids:
    ptype = random.choice(PARTY_TYPES)
    if ptype == "organization":
        first = last = ""
        org = fake.company()
        display = org
    else:
        first, last = fake.first_name(), fake.last_name()
        org = ""
        display = f"{first} {last}"
    dob = fake.date_of_birth(minimum_age=18, maximum_age=85) if ptype == "person" else None
    parties_rows.append([
        pid, ptype, display, first, last, org,
        dob.isoformat() if dob else "", fake.unique.email(), fake.phone_number(),
        random.choice(CONTACT_METHODS),
    ])
_write("parties", ["party_id", "party_type", "display_name", "first_name", "last_name",
                    "organization_name", "date_of_birth", "email", "phone",
                    "preferred_contact_method"], parties_rows)

customer_party_ids = party_ids[:N_CUSTOMERS]
agent_party_ids = party_ids[N_CUSTOMERS:]

# ---------------------------------------------------------------- customers
LIFECYCLE = ["prospect", "active", "active", "active", "inactive", "lapsed", "former"]
RISK_TIER = ["low", "medium", "high", "very_high"]

customer_ids = [f"CUST{i:06d}" for i in range(1, N_CUSTOMERS + 1)]
customers_rows, customers_bad_rows = [], []
BAD_FILE_SIZE = 100  # >= file_checks.min_rows so the issue is caught at DQC, not FQC
N_CUSTOMER_BAD = 15
for i, (cid, pid) in enumerate(zip(customer_ids, customer_party_ids)):
    acq = _rand_date(date(2018, 1, 1), TODAY)
    row = [cid, pid, f"CN-{i+1:07d}", random.choice(["retail", "small_business", "affluent"]),
           random.choice(LIFECYCLE), acq.isoformat(), random.choice(RISK_TIER),
           round(random.uniform(0, 100), 2)]
    (customers_bad_rows if i < BAD_FILE_SIZE else customers_rows).append(row)
for row in customers_bad_rows[:N_CUSTOMER_BAD]:  # injected issue: invalid lifecycle_stage
    row[4] = "unknown_status"
CUST_COLS = ["customer_id", "party_id", "customer_number", "customer_segment", "lifecycle_stage",
             "acquisition_date", "risk_tier", "engagement_score"]
_write("customers", CUST_COLS, customers_rows, filename="customers_1.csv")
_write("customers", CUST_COLS, customers_bad_rows, filename="customers_2.csv")

# ---------------------------------------------------------------- addresses
ADDR_TYPES = ["primary", "mailing", "billing", "risk_location", "business"]
addresses_rows = []
for i in range(N_ADDRESSES):
    pid = customer_party_ids[i % N_CUSTOMERS]
    addresses_rows.append([
        f"ADDR{i+1:06d}", pid, random.choice(ADDR_TYPES), fake.street_address(),
        fake.city(), fake.state_abbr(), fake.postcode(), "US", True,
    ])
_write("addresses", ["address_id", "party_id", "address_type", "line1", "city", "state_code",
                      "postal_code", "country_code", "is_current"], addresses_rows)

# ---------------------------------------------------------------- agents
CHANNELS = ["exclusive", "independent", "broker", "direct", "partner"]
agent_ids = [f"AGT{i:05d}" for i in range(1, N_AGENTS + 1)]
agents_rows_full, agents_rows_drift = [], []
for i, (aid, pid) in enumerate(zip(agent_ids, agent_party_ids)):
    appt = _rand_date(date(2015, 1, 1), TODAY)
    row = [aid, pid, f"AN-{i+1:06d}", fake.state_abbr(), random.choice(CHANNELS),
           f"TERR-{random.randint(1, 40):02d}", appt.isoformat(),
           random.choice(["active", "active", "active", "inactive", "terminated"])]
    (agents_rows_drift if i < 40 else agents_rows_full).append(row)
AGT_COLS = ["agent_id", "party_id", "agent_number", "license_state", "channel", "territory_code",
            "appointment_date", "status"]
_write("agents", AGT_COLS, agents_rows_full, filename="agents_1.csv")
# injected issue: schema drift -- territory_code column dropped entirely
drift_cols = [c for c in AGT_COLS if c != "territory_code"]
drift_idx = AGT_COLS.index("territory_code")
drift_rows = [[v for j, v in enumerate(r) if j != drift_idx] for r in agents_rows_drift]
_write("agents", drift_cols, drift_rows, filename="agents_drift.csv")

# ---------------------------------------------------------------- products
LOB = ["auto", "home", "life", "health", "commercial"]
product_ids = [f"PROD{i:04d}" for i in range(1, N_PRODUCTS + 1)]
products_rows = []
for i, prid in enumerate(product_ids):
    lob = LOB[i % len(LOB)]
    products_rows.append([
        prid, f"{lob.upper()}-{i+1:03d}", f"{lob.title()} {random.choice(['Standard', 'Plus', 'Premier'])}",
        lob, f"{lob}_family", random.choice(["base", "base", "base", "rider"]), True,
    ])
_write("products", ["product_id", "product_code", "product_name", "line_of_business",
                     "product_family", "product_component_type", "active_flag"], products_rows)

# ---------------------------------------------------------------- policies
POLICY_STATUS = ["quoted", "issued", "active", "active", "active", "cancelled", "expired", "renewed", "lapsed"]
policy_ids = [f"POL{i:06d}" for i in range(1, N_POLICIES + 1)]
policies_rows, policies_bad_rows = [], []
policy_customer = {}
policy_dates = {}
for i, plid in enumerate(policy_ids):
    cid = random.choice(customer_ids)
    aid = random.choice(agent_ids)
    prid = random.choice(product_ids)
    eff = _rand_date(date(2023, 1, 1), TODAY)
    exp = eff + timedelta(days=365)
    annual_prem = round(random.uniform(300, 15000), 2)
    policy_customer[plid], policy_dates[plid] = cid, (eff, exp)
    row = [plid, f"PN-{i+1:08d}", cid, aid, prid, random.choice(POLICY_STATUS),
           eff.isoformat(), exp.isoformat(), annual_prem, round(annual_prem * random.uniform(0.9, 1.0), 2)]
    (policies_bad_rows if i < BAD_FILE_SIZE else policies_rows).append(row)
POL_COLS = ["policy_id", "policy_number", "customer_id", "agent_id", "product_id", "policy_status",
            "effective_date", "expiration_date", "annual_premium", "written_premium"]
_write("policies", POL_COLS, policies_rows, filename="policies_1.csv")
# injected issue: 30 of these 100 rows share one duplicate policy_number
dup_number = policies_bad_rows[0][1]
for row in policies_bad_rows[:30]:
    row[1] = dup_number
_write("policies", POL_COLS, policies_bad_rows, filename="policies_2.csv")
all_policy_ids = [r[0] for r in policies_rows + policies_bad_rows]

# ---------------------------------------------------------------- policy_coverages
COVERAGES = [("BI", "Bodily Injury"), ("PD", "Property Damage"), ("COMP", "Comprehensive"),
             ("COLL", "Collision"), ("MED", "Medical Payments")]
coverage_rows = []
coverage_ids_by_policy: dict[str, list[str]] = {}
for i in range(N_COVERAGES):
    plid = all_policy_ids[i % len(all_policy_ids)]
    code, cname = random.choice(COVERAGES)
    cov_id = f"COV{i+1:06d}"
    eff, exp = policy_dates.get(plid, (TODAY, TODAY + timedelta(days=365)))
    coverage_rows.append([
        cov_id, plid, code, cname, "active",
        round(random.uniform(25000, 500000), 2), round(random.uniform(250, 5000), 2),
        eff.isoformat(), exp.isoformat(),
    ])
    coverage_ids_by_policy.setdefault(plid, []).append(cov_id)
_write("policy_coverages", ["policy_coverage_id", "policy_id", "coverage_code", "coverage_name",
                             "coverage_status", "limit_amount", "deductible_amount",
                             "effective_date", "expiration_date"], coverage_rows)

# ---------------------------------------------------------------- premiums
TXN_TYPES = ["new_business", "renewal", "endorsement", "cancellation", "reinstatement", "audit"]
premium_rows = []
for i in range(N_PREMIUMS):
    plid = all_policy_ids[i % len(all_policy_ids)]
    covs = coverage_ids_by_policy.get(plid, [])
    cov_id = random.choice(covs) if covs else ""
    eff, exp = policy_dates.get(plid, (TODAY, TODAY + timedelta(days=365)))
    written = round(random.uniform(50, 3000), 2)
    premium_rows.append([
        f"PREM{i+1:06d}", plid, cov_id, eff.isoformat(), exp.isoformat(),
        random.choice(TXN_TYPES), written, round(written * random.uniform(0.5, 1.0), 2),
    ])
_write("premiums", ["premium_id", "policy_id", "policy_coverage_id", "premium_period_start",
                     "premium_period_end", "transaction_type", "written_premium_amount",
                     "earned_premium_amount"], premium_rows)

# ---------------------------------------------------------------- payments
PAYMENT_STATUS = ["scheduled", "paid", "paid", "paid", "failed", "reversed", "refunded", "past_due"]
PAYMENT_METHOD = ["card", "ach", "check", "cash", "wire", "payroll", "other"]
payments_rows = []
for i in range(N_PAYMENTS):
    plid = all_policy_ids[i % len(all_policy_ids)]
    cid = policy_customer.get(plid, random.choice(customer_ids))
    billed = round(random.uniform(50, 2000), 2)
    payments_rows.append([
        f"PAY{i+1:06d}", plid, cid, _rand_date(date(2024, 1, 1), TODAY).isoformat(),
        random.choice(PAYMENT_STATUS), random.choice(PAYMENT_METHOD), billed,
        round(billed * random.choice([1.0, 1.0, 1.0, 0.0]), 2),
    ])
PAY_COLS = ["payment_id", "policy_id", "customer_id", "payment_date", "payment_status",
            "payment_method", "billed_amount", "paid_amount"]
_write("payments", PAY_COLS, payments_rows, filename="payments_1.csv")
# injected issue: row-count dip -- far fewer rows than file_checks.min_rows=100 expects
dip_rows = payments_rows[:40]
_write("payments", PAY_COLS, dip_rows, filename="payments_dip.csv")

# ---------------------------------------------------------------- claims
CLAIM_STATUS = ["open", "closed", "closed", "closed", "reopened", "denied", "subrogation"]
LOSS_CAUSES = ["collision", "fire", "theft", "weather", "water_damage", "liability", "other"]
claims_rows, claims_bad_rows = [], []
for i in range(N_CLAIMS):
    plid = all_policy_ids[i % len(all_policy_ids)]
    cid = policy_customer.get(plid, random.choice(customer_ids))
    covs = coverage_ids_by_policy.get(plid, [])
    cov_id = random.choice(covs) if covs else ""
    loss = _rand_date(date(2024, 1, 1), TODAY)
    report = loss + timedelta(days=random.randint(0, 14))
    # Realistic severity mix, not a flat uniform(200, 40000): that range made aggregate paid
    # claims run ~13x earned premium (a 1,700%+ "loss ratio"), because generating claims and
    # premiums independently with no cross-reference to a plausible ratio produced a number
    # that was arithmetically correct but not a believable KPI. 95% attritional (small,
    # everyday claims), 5% large losses (the fat tail every P&C book actually has) -- tuned so
    # the aggregate lands in the realistic 60-70% P&C loss-ratio range against this dataset's
    # earned premium, not just "some smaller number."
    if random.random() < 0.05:
        paid = round(random.uniform(3000, 15000), 2)
    else:
        paid = round(random.uniform(50, 1000), 2)
    row = [f"CLM{i+1:06d}", f"CN-{i+1:08d}", plid, cid, cov_id, loss.isoformat(), report.isoformat(),
           random.choice(CLAIM_STATUS), random.choice(LOSS_CAUSES), paid,
           round(paid * random.uniform(0, 0.3), 2)]
    (claims_bad_rows if i < BAD_FILE_SIZE else claims_rows).append(row)
CLM_COLS = ["claim_id", "claim_number", "policy_id", "customer_id", "policy_coverage_id",
            "loss_date", "report_date", "claim_status", "loss_cause", "paid_amount", "reserve_amount"]
_write("claims", CLM_COLS, claims_rows, filename="claims_1.csv")
# injected issue: 25 of these 100 rows have negative paid_amount (data entry / refund errors,
# same shape as the real nyctaxi fare_amount finding earlier this session)
for row in claims_bad_rows[:25]:
    row[9] = -abs(row[9])
_write("claims", CLM_COLS, claims_bad_rows, filename="claims_2.csv")

print("\ndone.")
