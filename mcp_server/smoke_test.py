"""
Smoke test for the Oreo mock sales MCP server.

Starts sales_server.py exactly the way Claude Desktop does (as a subprocess over
stdio), lists its tools, calls each one, and checks that bad input is rejected.
Run it from the oreo/ folder after generating the database:

    python mcp_server/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

SERVER = Path(__file__).resolve().parent / "sales_server.py"

GOOD_CALLS = [
    ("data_freshness", {}),
    ("list_dimensions", {}),
    ("get_sales_summary", {"period": "last_quarter"}),
    ("get_sales_summary", {"start_date": "2025-01-01", "end_date": "2025-03-31", "region": "europe"}),
    ("compare_periods", {"period_a": "this_year", "period_b": "same_period_last_year"}),
    ("top_items", {"dimension": "product", "period": "last_quarter", "limit": 5}),
    ("top_items", {"dimension": "customer", "metric": "gross_profit", "period": "this_year", "limit": 3}),
    ("sales_trend", {"granularity": "month", "period": "last_year"}),
    ("sales_trend", {"granularity": "quarter", "period": "all_time", "category": "Lighting"}),
]

BAD_CALLS = [
    ("get_sales_summary", {"start_date": "2025-13-01", "end_date": "2025-12-31"}),
    ("get_sales_summary", {"period": "last_quarter", "region": "Mars"}),
    ("top_items", {"dimension": "password", "period": "last_quarter"}),
    ("top_items", {"limit": 500}),
    ("sales_trend", {"granularity": "day", "period": "all_time"}),
    ("compare_periods", {"period_a": "this_year"}),
]


def show(result) -> str:
    data = result.structured_content
    if data is None:
        data = " ".join(getattr(c, "text", "") for c in result.content)
    return json.dumps(data, indent=2)[:900]


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    failures = 0
    async with Client(params) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools.tools)
        print("Tools:", ", ".join(names))
        expected = sorted(["data_freshness", "list_dimensions", "get_sales_summary",
                           "compare_periods", "top_items", "sales_trend"])
        if names != expected:
            print("  FAIL: unexpected tool list")
            failures += 1

        for name, args in GOOD_CALLS:
            result = await client.call_tool(name, args)
            status = "FAIL" if result.is_error else "ok"
            failures += result.is_error
            print(f"\n[{status}] {name} {args}\n{show(result)}")

        print("\n--- invalid input should be rejected ---")
        for name, args in BAD_CALLS:
            result = await client.call_tool(name, args)
            status = "ok (rejected)" if result.is_error else "FAIL (accepted)"
            failures += not result.is_error
            print(f"[{status}] {name} {args}: {show(result)[:200]}")

    print(f"\n{'All checks passed.' if not failures else f'{failures} check(s) failed.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
