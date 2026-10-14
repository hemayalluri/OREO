"""
Oreo mock sales MCP server (read-only).

Exposes six tools over MCP (stdio transport) on top of the SQLite database that
data/create_mock_data.py generates:

    data_freshness     what data exists and how current it is
    list_dimensions    valid regions, channels, categories, products, segments, periods
    get_sales_summary  revenue, orders, units, profit, margin for a period + filters
    compare_periods    the same summary for two periods, with changes
    top_items          top products / categories / regions / channels / customers / segments
    sales_trend        a metric over time by day, week, month, quarter or year

Safety:
  * The database is opened read-only (SQLite URI mode=ro plus PRAGMA query_only).
  * Every input is validated; filter values must match known names and column
    names come only from fixed allowlists. All SQL values are bound parameters.
  * Every result carries its source and "as of" time.

Run it directly to check it starts (it then waits for an MCP client on stdin):
    python mcp_server/sales_server.py
Point Claude Desktop at it as described in README.md. Override the database
location with the OREO_SALES_DB environment variable.
"""

from __future__ import annotations

import calendar
import logging
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

# stdout is the MCP channel in stdio mode, so all logging goes to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oreo.sales_server")

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "sales.db"
DB_PATH = Path(os.environ.get("OREO_SALES_DB", DEFAULT_DB)).expanduser().resolve()

MAX_RANGE_DAYS = 366 * 3   # longest date range a single request may cover
MAX_TREND_POINTS = 400     # e.g. a year of daily points
MAX_TOP_LIMIT = 25

# Allowlisted SQL expressions. User input never becomes part of the SQL text.
METRICS = {
    "revenue": "ROUND(SUM(oi.net_amount), 2)",
    "gross_profit": "ROUND(SUM(oi.net_amount - oi.cost_amount), 2)",
    "units": "SUM(oi.quantity)",
    "orders": "COUNT(DISTINCT o.order_id)",
    "customers": "COUNT(DISTINCT o.customer_id)",
}
DIMENSIONS = {
    "product": "p.name",
    "category": "p.category",
    "region": "r.name",
    "channel": "ch.name",
    "customer": "c.name",
    "segment": "c.segment",
}
FILTER_COLUMNS = {  # filter argument -> (SQL column, lookup query for valid values)
    "region": ("r.name", "SELECT name FROM regions ORDER BY region_id"),
    "channel": ("ch.name", "SELECT name FROM channels ORDER BY channel_id"),
    "category": ("p.category", "SELECT DISTINCT category FROM products ORDER BY category"),
    "product": ("p.name", "SELECT name FROM products ORDER BY sku"),
    "segment": ("c.segment", "SELECT DISTINCT segment FROM customers ORDER BY segment"),
}
GRANULARITIES = ("day", "week", "month", "quarter", "year")
PERIODS = {
    "last_7_days": "the 7 days ending on the latest data date",
    "last_30_days": "the 30 days ending on the latest data date",
    "last_90_days": "the 90 days ending on the latest data date",
    "this_month": "month to date",
    "last_month": "the previous full calendar month",
    "this_quarter": "quarter to date",
    "last_quarter": "the previous full calendar quarter",
    "this_year": "year to date",
    "last_year": "the previous full calendar year",
    "same_period_last_year": "year to date, shifted back one year (pairs with this_year)",
    "all_time": "every date in the database",
}

BASE_FROM = """
    FROM order_items oi
    JOIN orders o     ON o.order_id = oi.order_id
    JOIN products p   ON p.product_id = oi.product_id
    JOIN customers c  ON c.customer_id = o.customer_id
    JOIN regions r    ON r.region_id = o.region_id
    JOIN channels ch  ON ch.channel_id = o.channel_id
    WHERE o.status = 'completed' AND o.order_date BETWEEN ? AND ?
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

mcp = MCPServer(
    name="oreo-mock-sales",
    instructions=(
        "Read-only access to the fictional Lumen Desk Co. mock sales database used by the Oreo demo. "
        "Revenue is net of discounts and counts completed orders only (returns and cancellations are excluded). "
        "Call data_freshness or list_dimensions first if you need valid names or the latest data date. "
        "Always tell the user the source and the 'as of' date that each result includes."
    ),
)


# --------------------------------------------------------------------------- #
# Database access
# --------------------------------------------------------------------------- #

def connect() -> sqlite3.Connection:
    """Open the database strictly read-only."""
    if not DB_PATH.exists():
        raise ToolError(
            f"The sales database was not found at {DB_PATH}. Run 'python data/create_mock_data.py' first."
        )
    con = sqlite3.connect(f"{DB_PATH.as_uri()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    con.row_factory = sqlite3.Row
    return con


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        with connect() as con:
            return con.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        log.exception("Database error")
        raise ToolError("I can't reach the sales database right now.") from exc


def metadata() -> dict[str, str]:
    return {row["key"]: row["value"] for row in query("SELECT key, value FROM metadata")}


def source_info(meta: dict[str, str] | None = None) -> dict[str, str]:
    """The source / as-of block attached to every answer."""
    meta = meta or metadata()
    return {
        "source": f"{meta['company_name']} mock sales database (SQLite, fictional data)",
        "as_of": meta["data_end"],
        "generated_at": meta["generated_at"],
        "currency": meta["currency"],
    }


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #

def parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        raise ToolError(f"{field} must be a date in YYYY-MM-DD format, got {value!r}.") from None


def quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def shift_year(d: date, years: int) -> date:
    """Same day in another year; Feb 29 becomes Feb 28."""
    day = min(d.day, calendar.monthrange(d.year + years, d.month)[1])
    return date(d.year + years, d.month, day)


def named_period(name: str, meta: dict[str, str]) -> tuple[date, date]:
    """Turn a period name into dates, relative to the latest date in the data."""
    anchor = date.fromisoformat(meta["data_end"])
    first = date.fromisoformat(meta["data_start"])
    if name.startswith("last_") and name.endswith("_days"):
        n = int(name.split("_")[1])
        return anchor - timedelta(days=n - 1), anchor
    if name == "this_month":
        return anchor.replace(day=1), anchor
    if name == "last_month":
        end = anchor.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if name == "this_quarter":
        return quarter_start(anchor), anchor
    if name == "last_quarter":
        end = quarter_start(anchor) - timedelta(days=1)
        return quarter_start(end), end
    if name == "this_year":
        return date(anchor.year, 1, 1), anchor
    if name == "last_year":
        return date(anchor.year - 1, 1, 1), date(anchor.year - 1, 12, 31)
    if name == "same_period_last_year":
        return date(anchor.year - 1, 1, 1), shift_year(anchor, -1)
    if name == "all_time":
        return first, anchor
    raise AssertionError(name)


def resolve_period(period: str | None, start_date: str | None, end_date: str | None,
                   meta: dict[str, str], label: str = "period") -> dict[str, str]:
    """Accept either a named period or an explicit start_date + end_date."""
    if period and (start_date or end_date):
        raise ToolError(f"Give either a named {label} or start/end dates, not both.")
    if period:
        key = period.strip().lower()
        if key not in PERIODS:
            raise ToolError(f"Unknown {label} {period!r}. Valid names: {', '.join(PERIODS)}.")
        start, end = named_period(key, meta)
        name = key
    elif start_date and end_date:
        start, end = parse_date(start_date, "start_date"), parse_date(end_date, "end_date")
        name = "custom"
    elif start_date or end_date:
        raise ToolError("Give both start_date and end_date, or use a named period instead.")
    else:
        start, end = named_period("last_30_days", meta)
        name = "last_30_days (default)"
    if start > end:
        raise ToolError(f"The {label} start ({start}) is after its end ({end}).")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ToolError(f"The {label} is too long; the maximum is {MAX_RANGE_DAYS} days.")

    out = {"name": name, "start": start.isoformat(), "end": end.isoformat()}
    data_start, data_end = meta["data_start"], meta["data_end"]
    if out["end"] < data_start or out["start"] > data_end:
        out["note"] = f"No data in this range; the database covers {data_start} to {data_end}."
    elif out["start"] < data_start or out["end"] > data_end:
        out["note"] = f"Partly outside the data range ({data_start} to {data_end}); only that part is counted."
    return out


def resolve_filters(**raw: str | None) -> dict[str, str]:
    """Match each filter case-insensitively against the real values in the database."""
    clean = {}
    for key, value in raw.items():
        if value is None or not str(value).strip():
            continue
        valid = [row[0] for row in query(FILTER_COLUMNS[key][1])]
        match = next((v for v in valid if v.lower() == value.strip().lower()), None)
        if match is None:
            raise ToolError(f"Unknown {key} {value!r}. Valid values: {', '.join(valid)}.")
        clean[key] = match
    return clean


def where_clause(period: dict[str, str], filters: dict[str, str]) -> tuple[str, list[Any]]:
    sql, params = BASE_FROM, [period["start"], period["end"]]
    for key, value in filters.items():
        sql += f" AND {FILTER_COLUMNS[key][0]} = ?"
        params.append(value)
    return sql, params


def check_choice(value: str, valid, field: str) -> str:
    key = (value or "").strip().lower()
    if key not in valid:
        raise ToolError(f"{field} must be one of: {', '.join(valid)}; got {value!r}.")
    return key


def summarize(period: dict[str, str], filters: dict[str, str]) -> dict[str, Any]:
    from_sql, params = where_clause(period, filters)
    row = query(
        f"""SELECT {METRICS['revenue']} AS revenue, {METRICS['gross_profit']} AS gross_profit,
                   {METRICS['units']} AS units, {METRICS['orders']} AS orders,
                   {METRICS['customers']} AS customers {from_sql}""",
        tuple(params),
    )[0]
    revenue = row["revenue"] or 0.0
    orders = row["orders"] or 0
    profit = row["gross_profit"] or 0.0
    return {
        "revenue": revenue,
        "orders": orders,
        "units": row["units"] or 0,
        "customers": row["customers"] or 0,
        "avg_order_value": round(revenue / orders, 2) if orders else 0.0,
        "gross_profit": profit,
        "gross_margin_pct": round(100 * profit / revenue, 1) if revenue else 0.0,
    }


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=READ_ONLY)
def data_freshness() -> dict[str, Any]:
    """Report which company the data belongs to, the date range it covers, when it was
    generated, and row counts. Use this to answer 'how current is the data?' and to learn
    the latest date before working out periods like 'last quarter'."""
    meta = metadata()
    counts = query(
        """SELECT (SELECT COUNT(*) FROM orders) AS orders,
                  (SELECT COUNT(*) FROM order_items) AS order_lines,
                  (SELECT COUNT(*) FROM customers) AS customers,
                  (SELECT COUNT(*) FROM products) AS products"""
    )[0]
    return {
        "company": meta["company_name"],
        "data_start": meta["data_start"],
        "data_end": meta["data_end"],
        "row_counts": dict(counts),
        "note": meta.get("description", ""),
        **source_info(meta),
    }


@mcp.tool(annotations=READ_ONLY)
def list_dimensions() -> dict[str, Any]:
    """List every valid value for filters and groupings: regions, channels, product
    categories, products, customer segments, plus the metrics, period names and trend
    granularities the other tools accept."""
    products = query("SELECT sku, name, category, list_price, launch_date, discontinued_date FROM products ORDER BY sku")
    return {
        "regions": [r[0] for r in query(FILTER_COLUMNS["region"][1])],
        "channels": [r[0] for r in query(FILTER_COLUMNS["channel"][1])],
        "categories": [r[0] for r in query(FILTER_COLUMNS["category"][1])],
        "segments": [r[0] for r in query(FILTER_COLUMNS["segment"][1])],
        "products": [{k: p[k] for k in p.keys() if p[k] is not None} for p in products],
        "metrics": list(METRICS),
        "top_items_dimensions": list(DIMENSIONS),
        "periods": PERIODS,
        "trend_granularities": list(GRANULARITIES),
        **source_info(),
    }


@mcp.tool(annotations=READ_ONLY)
def get_sales_summary(
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    region: str | None = None,
    channel: str | None = None,
    category: str | None = None,
    product: str | None = None,
    segment: str | None = None,
) -> dict[str, Any]:
    """Total revenue, orders, units, customers, average order value, gross profit and
    margin for one period. Choose the period with a name (e.g. 'last_quarter', 'this_year';
    see list_dimensions) or with start_date and end_date (YYYY-MM-DD). Defaults to the last
    30 days. Optional filters narrow the data: region, channel, category, product, segment."""
    meta = metadata()
    p = resolve_period(period, start_date, end_date, meta)
    f = resolve_filters(region=region, channel=channel, category=category, product=product, segment=segment)
    return {"period": p, "filters": f, "summary": summarize(p, f), **source_info(meta)}


@mcp.tool(annotations=READ_ONLY)
def compare_periods(
    period_a: str | None = None,
    period_b: str | None = None,
    a_start_date: str | None = None,
    a_end_date: str | None = None,
    b_start_date: str | None = None,
    b_end_date: str | None = None,
    region: str | None = None,
    channel: str | None = None,
    category: str | None = None,
    product: str | None = None,
    segment: str | None = None,
) -> dict[str, Any]:
    """Compare two periods side by side, with the absolute and percentage change for each
    metric (B is the baseline: change = A minus B). Give each period as a name
    (e.g. period_a='this_quarter', period_b='last_quarter', or 'this_year' vs
    'same_period_last_year') or as start/end dates. The same filters apply to both."""
    meta = metadata()
    pa = resolve_period(period_a, a_start_date, a_end_date, meta, "period_a")
    pb = resolve_period(period_b, b_start_date, b_end_date, meta, "period_b")
    if pa["name"].endswith("(default)") or pb["name"].endswith("(default)"):
        raise ToolError("compare_periods needs both periods: give period_a and period_b (names or dates).")
    f = resolve_filters(region=region, channel=channel, category=category, product=product, segment=segment)
    a, b = summarize(pa, f), summarize(pb, f)
    change = {}
    for key in a:
        diff = a[key] - b[key]
        change[key] = {
            "change": round(diff, 2),
            "change_pct": round(100 * diff / b[key], 1) if b[key] else None,
        }
    return {"period_a": {**pa, "summary": a}, "period_b": {**pb, "summary": b},
            "change_a_vs_b": change, "filters": f, **source_info(meta)}


@mcp.tool(annotations=READ_ONLY)
def top_items(
    dimension: str = "product",
    metric: str = "revenue",
    limit: int = 5,
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    region: str | None = None,
    channel: str | None = None,
    category: str | None = None,
    product: str | None = None,
    segment: str | None = None,
) -> dict[str, Any]:
    """Rank the top items for a period. dimension: product, category, region, channel,
    customer or segment. metric: revenue, gross_profit, units, orders or customers.
    limit: 1-25 (default 5). Each item includes its share of the total. Period and filters
    work as in get_sales_summary (default: last 30 days)."""
    dim = check_choice(dimension, DIMENSIONS, "dimension")
    met = check_choice(metric, METRICS, "metric")
    if not isinstance(limit, int) or not 1 <= limit <= MAX_TOP_LIMIT:
        raise ToolError(f"limit must be a whole number from 1 to {MAX_TOP_LIMIT}.")
    meta = metadata()
    p = resolve_period(period, start_date, end_date, meta)
    f = resolve_filters(region=region, channel=channel, category=category, product=product, segment=segment)
    from_sql, params = where_clause(p, f)

    total = query(f"SELECT {METRICS[met]} AS v {from_sql}", tuple(params))[0]["v"] or 0
    rows = query(
        f"""SELECT {DIMENSIONS[dim]} AS name, {METRICS[met]} AS value {from_sql}
            GROUP BY {DIMENSIONS[dim]} ORDER BY value DESC, name LIMIT ?""",
        tuple(params) + (limit,),
    )
    items = [
        {"rank": i, "name": r["name"], met: r["value"],
         "share_pct": round(100 * r["value"] / total, 1) if total else 0.0}
        for i, r in enumerate(rows, start=1)
    ]
    return {"dimension": dim, "metric": met, "period": p, "filters": f,
            "total": total, "items": items, **source_info(meta)}


@mcp.tool(annotations=READ_ONLY)
def sales_trend(
    granularity: str = "month",
    metric: str = "revenue",
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    region: str | None = None,
    channel: str | None = None,
    category: str | None = None,
    product: str | None = None,
    segment: str | None = None,
) -> dict[str, Any]:
    """A metric over time, one point per day, week (starting Monday), month, quarter or
    year, with empty periods filled in as zero. Good for charts and 'how are sales
    trending?' questions. metric: revenue, gross_profit, units, orders or customers.
    Period and filters work as in get_sales_summary (default: last 30 days)."""
    gran = check_choice(granularity, GRANULARITIES, "granularity")
    met = check_choice(metric, METRICS, "metric")
    meta = metadata()
    p = resolve_period(period, start_date, end_date, meta)
    f = resolve_filters(region=region, channel=channel, category=category, product=product, segment=segment)

    keys = bucket_starts(date.fromisoformat(p["start"]), date.fromisoformat(p["end"]), gran)
    if len(keys) > MAX_TREND_POINTS:
        raise ToolError(f"That would be more than {MAX_TREND_POINTS} points. "
                        "Use a coarser granularity or a shorter period.")
    bucket_sql = {
        "day": "o.order_date",
        "week": "date(o.order_date, '-6 days', 'weekday 1')",
        "month": "strftime('%Y-%m-01', o.order_date)",
        "quarter": "printf('%s-%02d-01', strftime('%Y', o.order_date), "
                   "((CAST(strftime('%m', o.order_date) AS INTEGER) - 1) / 3) * 3 + 1)",
        "year": "strftime('%Y-01-01', o.order_date)",
    }[gran]
    from_sql, params = where_clause(p, f)
    rows = query(f"SELECT {bucket_sql} AS bucket, {METRICS[met]} AS value {from_sql} GROUP BY bucket", tuple(params))
    values = {r["bucket"]: r["value"] for r in rows}
    points = [{"period_start": k, "label": bucket_label(k, gran), met: values.get(k, 0)} for k in keys]
    return {"granularity": gran, "metric": met, "period": p, "filters": f,
            "points": points, **source_info(meta)}


def bucket_starts(start: date, end: date, gran: str) -> list[str]:
    """Start date of every bucket that overlaps [start, end], as ISO strings."""
    if gran == "day":
        cur = start
    elif gran == "week":
        cur = start - timedelta(days=start.weekday())
    elif gran == "month":
        cur = start.replace(day=1)
    elif gran == "quarter":
        cur = quarter_start(start)
    else:
        cur = date(start.year, 1, 1)
    out = []
    while cur <= end and len(out) <= MAX_TREND_POINTS:
        out.append(cur.isoformat())
        if gran == "day":
            cur += timedelta(days=1)
        elif gran == "week":
            cur += timedelta(days=7)
        elif gran == "month":
            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        elif gran == "quarter":
            cur = date(cur.year + (cur.month + 3 > 12), (cur.month + 2) % 12 + 1, 1)
        else:
            cur = date(cur.year + 1, 1, 1)
    return out


def bucket_label(key: str, gran: str) -> str:
    d = date.fromisoformat(key)
    if gran == "month":
        return d.strftime("%b %Y")
    if gran == "quarter":
        return f"Q{(d.month - 1) // 3 + 1} {d.year}"
    if gran == "year":
        return str(d.year)
    if gran == "week":
        return f"Week of {d.isoformat()}"
    return d.isoformat()


if __name__ == "__main__":
    log.info("Starting Oreo mock sales MCP server (read-only), database: %s", DB_PATH)
    if not DB_PATH.exists():
        log.warning("Database not found yet; run 'python data/create_mock_data.py' first.")
    mcp.run()
