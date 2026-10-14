"""
Create Oreo's mock sales database (SQLite).

Generates about 2 years of realistic, entirely fake B2B orders for a fictional
company, "Lumen Desk Co.", which sells desks, chairs, lighting, tech and
accessories to businesses. The data has growth, seasonality (busy Q4 and
back-to-school, Black Friday spike, quiet weekends), a mid-way price increase,
a product launch and a discontinued product, so trends and comparisons look
believable in a demo.

Usage (from the oreo/ folder):
    python data/create_mock_data.py                 # writes data/sales.db, ending today
    python data/create_mock_data.py --end-date 2026-09-30 --seed 7
    python data/create_mock_data.py --out /tmp/test.db --days 365

Only the Python standard library is used. Re-running overwrites the database.
"""

from __future__ import annotations

import argparse
import math
import random
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

COMPANY_NAME = "Lumen Desk Co."
CURRENCY = "USD"
DEFAULT_DB = Path(__file__).resolve().parent / "sales.db"

# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #

# (sku, name, category, list price, unit cost, popularity weight, launch offset days, end offset days)
# Offsets are days after the first day of data; None means "whole period".
PRODUCTS = [
    ("DSK-100", "Aspen Standing Desk", "Desks", 649.00, 310.00, 9, None, None),
    ("DSK-110", "Aspen Standing Desk XL", "Desks", 849.00, 405.00, 5, None, None),
    ("DSK-200", "Cedar Writing Desk", "Desks", 399.00, 180.00, 6, None, None),
    ("DSK-300", "Birch Corner Desk", "Desks", 529.00, 250.00, 4, None, None),
    ("DSK-400", "Summit Executive Desk", "Desks", 1299.00, 640.00, 2, None, None),
    ("CHR-100", "Orbit Ergonomic Chair", "Chairs", 459.00, 205.00, 10, None, None),
    ("CHR-150", "Orbit Pro Chair", "Chairs", 729.00, 330.00, 5, None, None),
    ("CHR-200", "Breeze Mesh Task Chair", "Chairs", 249.00, 105.00, 12, None, None),
    ("CHR-300", "Perch Drafting Stool", "Chairs", 189.00, 80.00, 4, None, None),
    ("CHR-400", "Lounge Guest Chair", "Chairs", 329.00, 150.00, 3, None, None),
    ("LGT-100", "Glow Desk Lamp", "Lighting", 79.00, 28.00, 14, None, None),
    ("LGT-150", "Glow Desk Lamp Mini", "Lighting", 49.00, 17.00, 10, None, 420),  # discontinued
    ("LGT-200", "Halo Smart Lamp", "Lighting", 149.00, 52.00, 13, 300, None),  # launched mid-way
    ("LGT-300", "Beam Monitor Light Bar", "Lighting", 99.00, 34.00, 11, None, None),
    ("LGT-400", "Lantern Floor Lamp", "Lighting", 189.00, 72.00, 3, None, None),
    ("TEC-100", "Pulse Wireless Keyboard", "Tech", 89.00, 31.00, 15, None, None),
    ("TEC-110", "Pulse Wireless Mouse", "Tech", 49.00, 15.00, 16, None, None),
    ("TEC-200", "Dock One USB-C Hub", "Tech", 129.00, 48.00, 12, None, None),
    ("TEC-300", "ClearView 27in Monitor", "Tech", 329.00, 210.00, 8, None, None),
    ("TEC-310", "ClearView 34in Ultrawide", "Tech", 599.00, 380.00, 4, None, None),
    ("TEC-400", "Echo Conference Speaker", "Tech", 219.00, 95.00, 5, None, None),
    ("TEC-500", "Focus Noise-Cancel Headset", "Tech", 179.00, 70.00, 9, None, None),
    ("ACC-100", "Dual Monitor Arm", "Accessories", 139.00, 50.00, 10, None, None),
    ("ACC-200", "Felt Desk Mat", "Accessories", 39.00, 11.00, 14, None, None),
    ("ACC-300", "Cable Tidy Kit", "Accessories", 25.00, 6.00, 12, None, None),
    ("ACC-400", "Bamboo Laptop Stand", "Accessories", 59.00, 19.00, 11, None, None),
    ("ACC-500", "Footrest Plus", "Accessories", 69.00, 24.00, 6, None, None),
    ("ACC-600", "Under-Desk Drawer", "Accessories", 119.00, 45.00, 5, None, None),
    ("ACC-700", "Acoustic Desk Divider", "Accessories", 159.00, 62.00, 4, None, None),
    ("ACC-800", "Whiteboard Easel", "Accessories", 199.00, 80.00, 3, None, None),
]

# (region name, share of customers)
REGIONS = [
    ("North America East", 0.27),
    ("North America West", 0.23),
    ("Europe", 0.24),
    ("Asia Pacific", 0.16),
    ("Latin America", 0.10),
]

CHANNELS = ["Direct Sales", "Online Store", "Reseller Partners", "Marketplace"]

# Segment settings: share of customers, how often they order, basket shape, discount range,
# and which channels they tend to use (weights in CHANNELS order).
SEGMENTS = {
    "Enterprise": dict(share=0.08, activity=6.0, lines=(2, 6), qty=(5, 40), discount=(0.10, 0.20),
                       channels=[60, 10, 25, 5]),
    "Mid-Market": dict(share=0.30, activity=2.0, lines=(1, 4), qty=(2, 15), discount=(0.04, 0.10),
                       channels=[30, 30, 30, 10]),
    "Small Business": dict(share=0.62, activity=1.0, lines=(1, 3), qty=(1, 5), discount=(0.0, 0.05),
                           channels=[5, 50, 15, 30]),
}

CUSTOMER_COUNT = 1200
BASE_ORDERS_PER_DAY = 34          # at the start of the period, before seasonality
ANNUAL_GROWTH = 0.18              # ~18% year-over-year growth in order volume
PRICE_INCREASE = 0.04             # list prices rise 4% at the start of year 2

# Month seasonality (Jan..Dec): quiet January, back-to-school August, strong Q4.
MONTH_FACTOR = [0.82, 0.86, 0.97, 0.95, 1.0, 0.94, 0.92, 1.10, 1.05, 1.02, 1.28, 1.32]
WEEKDAY_FACTOR = [1.08, 1.12, 1.12, 1.08, 1.0, 0.45, 0.35]  # Mon..Sun

NAME_PREFIX = [
    "Acme", "Bluebird", "Copperline", "Driftwood", "Evergreen", "Foxglove", "Granite", "Harbor",
    "Ironwood", "Juniper", "Keystone", "Lakeside", "Meridian", "Northstar", "Oakridge", "Pinecrest",
    "Quartz", "Riverside", "Silverline", "Tidewater", "Upland", "Vantage", "Westbrook", "Yellowfin",
    "Zenith", "Brightpath", "Cobalt", "Daybreak", "Emberly", "Fairview", "Goldleaf", "Highland",
]
NAME_CORE = [
    "Analytics", "Architects", "Bakery", "Biotech", "Capital", "Clinics", "Consulting", "Design",
    "Dynamics", "Energy", "Engineering", "Foods", "Health", "Insurance", "Labs", "Law Group",
    "Logistics", "Media", "Partners", "Robotics", "Software", "Studios", "Systems", "Ventures",
]
NAME_SUFFIX = ["Inc.", "LLC", "Ltd.", "Group", "Co.", "GmbH", "Pty Ltd", "S.A."]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def black_friday(year: int) -> date:
    """Fourth Thursday of November plus one day."""
    nov1 = date(year, 11, 1)
    first_thu = nov1 + timedelta(days=(3 - nov1.weekday()) % 7)
    return first_thu + timedelta(days=22)


def bf_week(d: date) -> bool:
    """True from Black Friday through Cyber Monday."""
    bf = black_friday(d.year)
    return bf <= d <= bf + timedelta(days=3)


def day_factor(d: date) -> float:
    """Combined seasonality multiplier for one day."""
    f = MONTH_FACTOR[d.month - 1] * WEEKDAY_FACTOR[d.weekday()]
    if bf_week(d):                                  # Black Friday through Cyber Monday
        f *= 2.4
    if (d.month == 12 and d.day >= 24) or (d.month == 1 and d.day == 1):  # holiday shutdown
        f *= 0.25
    return f


def make_customers(rng: random.Random) -> list[tuple]:
    """Return customer rows: (customer_id, name, segment, region_id, activity weight)."""
    seg_names = list(SEGMENTS)
    seg_weights = [SEGMENTS[s]["share"] for s in seg_names]
    region_weights = [share for _, share in REGIONS]
    used, rows = set(), []
    for cid in range(1, CUSTOMER_COUNT + 1):
        while True:
            name = f"{rng.choice(NAME_PREFIX)} {rng.choice(NAME_CORE)} {rng.choice(NAME_SUFFIX)}"
            if name not in used:
                used.add(name)
                break
        segment = rng.choices(seg_names, seg_weights)[0]
        region_id = rng.choices(range(1, len(REGIONS) + 1), region_weights)[0]
        # Long-tail activity: a few customers order far more than others.
        activity = SEGMENTS[segment]["activity"] * rng.lognormvariate(0, 0.8)
        rows.append((cid, name, segment, region_id, activity))
    return rows


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE regions (
    region_id INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE
);
CREATE TABLE channels (
    channel_id INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE
);
CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    sku        TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL UNIQUE,
    category   TEXT NOT NULL,
    list_price REAL NOT NULL,          -- current list price
    unit_cost  REAL NOT NULL,
    launch_date        TEXT,           -- NULL = sold for the whole period
    discontinued_date  TEXT
);
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    segment     TEXT NOT NULL,         -- Enterprise | Mid-Market | Small Business
    region_id   INTEGER NOT NULL REFERENCES regions(region_id)
);
CREATE TABLE orders (
    order_id    INTEGER PRIMARY KEY,
    order_date  TEXT NOT NULL,         -- ISO date, YYYY-MM-DD
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    channel_id  INTEGER NOT NULL REFERENCES channels(channel_id),
    region_id   INTEGER NOT NULL REFERENCES regions(region_id),
    status      TEXT NOT NULL          -- completed | returned | cancelled
);
CREATE TABLE order_items (
    order_id    INTEGER NOT NULL REFERENCES orders(order_id),
    line_no     INTEGER NOT NULL,
    product_id  INTEGER NOT NULL REFERENCES products(product_id),
    quantity    INTEGER NOT NULL,
    unit_price  REAL NOT NULL,         -- list price at time of order
    discount    REAL NOT NULL,         -- 0.0 - 1.0
    net_amount  REAL NOT NULL,         -- quantity * unit_price * (1 - discount)
    cost_amount REAL NOT NULL,         -- quantity * unit cost
    PRIMARY KEY (order_id, line_no)
);
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_items_product ON order_items(product_id);
"""


def build(out: Path, end: date, days: int, seed: int) -> dict:
    rng = random.Random(seed)
    start = end - timedelta(days=days - 1)
    year2_start = start + timedelta(days=365)

    if out.exists():
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(out)
    con.executescript(SCHEMA)

    con.executemany("INSERT INTO regions VALUES (?, ?)", [(i + 1, n) for i, (n, _) in enumerate(REGIONS)])
    con.executemany("INSERT INTO channels VALUES (?, ?)", [(i + 1, n) for i, n in enumerate(CHANNELS)])

    # Products, with launch / discontinue dates turned into real dates.
    product_rows = []
    for pid, (sku, name, cat, price, cost, _w, launch, stop) in enumerate(PRODUCTS, start=1):
        launch_d = (start + timedelta(days=launch)).isoformat() if launch is not None else None
        stop_d = (start + timedelta(days=stop)).isoformat() if stop is not None else None
        current_price = round(price * (1 + PRICE_INCREASE), 2) if days > 365 else price
        product_rows.append((pid, sku, name, cat, current_price, cost, launch_d, stop_d))
    con.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)", product_rows)

    customers = make_customers(rng)
    con.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", [c[:4] for c in customers])
    cust_ids = [c[0] for c in customers]
    cust_weights = [c[4] for c in customers]
    cust_by_id = {c[0]: c for c in customers}

    orders, items = [], []
    order_id = 0
    d = start
    while d <= end:
        t_years = (d - start).days / 365.0
        expected = BASE_ORDERS_PER_DAY * ((1 + ANNUAL_GROWTH) ** t_years) * day_factor(d)
        # Poisson-ish daily count via a normal approximation, never negative.
        n_orders = max(0, int(round(rng.gauss(expected, math.sqrt(expected)))))

        # Products available on this day, and price level.
        available = [
            (pid, row) for pid, row in enumerate(PRODUCTS, start=1)
            if (row[6] is None or d >= start + timedelta(days=row[6]))
            and (row[7] is None or d < start + timedelta(days=row[7]))
        ]
        # New products ramp up over ~4 months after launch.
        weights = []
        for pid, row in available:
            w = row[5]
            if row[6] is not None:
                age = (d - (start + timedelta(days=row[6]))).days
                w *= min(1.0, 0.2 + age / 120)
            weights.append(w)
        price_mult = 1 + PRICE_INCREASE if d >= year2_start else 1.0

        for cid in rng.choices(cust_ids, cust_weights, k=n_orders):
            order_id += 1
            _, _, segment, region_id, _ = cust_by_id[cid]
            seg = SEGMENTS[segment]
            channel_id = rng.choices(range(1, len(CHANNELS) + 1), seg["channels"])[0]
            status = rng.choices(["completed", "returned", "cancelled"], [96, 2, 2])[0]
            orders.append((order_id, d.isoformat(), cid, channel_id, region_id, status))

            n_lines = min(rng.randint(*seg["lines"]), len(available))
            picks = set()
            while len(picks) < n_lines:
                picks.add(rng.choices(range(len(available)), weights)[0])
            for line_no, idx in enumerate(sorted(picks), start=1):
                pid, row = available[idx]
                price = round(row[3] * price_mult, 2)
                lo, hi = seg["qty"]
                # Expensive items sell in smaller quantities.
                qty = max(1, int(rng.randint(lo, hi) * (0.5 if row[3] > 500 else 1.0)))
                disc = round(rng.uniform(*seg["discount"]), 3)
                if bf_week(d):
                    disc = min(0.35, disc + 0.10)  # holiday promotion
                net = round(qty * price * (1 - disc), 2)
                cost = round(qty * row[4], 2)
                items.append((order_id, line_no, pid, qty, price, disc, net, cost))
        d += timedelta(days=1)

    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", orders)
    con.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)", items)

    meta = {
        "company_name": COMPANY_NAME,
        "currency": CURRENCY,
        "data_start": start.isoformat(),
        "data_end": end.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": str(seed),
        "description": "Fictional mock data for the Oreo demo. Not real company data.",
    }
    con.executemany("INSERT INTO metadata VALUES (?, ?)", list(meta.items()))
    con.commit()
    con.execute("VACUUM")
    con.close()
    return {"orders": len(orders), "order_lines": len(items), "customers": len(customers), **meta}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Oreo's mock sales database.")
    parser.add_argument("--out", type=Path, default=DEFAULT_DB, help=f"output file (default: {DEFAULT_DB})")
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today(),
                        help="last day of data, YYYY-MM-DD (default: today)")
    parser.add_argument("--days", type=int, default=730, help="number of days of history (default: 730)")
    parser.add_argument("--seed", type=int, default=42, help="random seed, for repeatable data (default: 42)")
    args = parser.parse_args()
    if args.days < 30:
        parser.error("--days must be at least 30")

    summary = build(args.out, args.end_date, args.days, args.seed)
    print(f"Created {args.out}")
    print(f"  {summary['company_name']}: {summary['data_start']} to {summary['data_end']}")
    print(f"  {summary['orders']:,} orders, {summary['order_lines']:,} order lines, "
          f"{summary['customers']:,} customers, {len(PRODUCTS)} products")


if __name__ == "__main__":
    main()
