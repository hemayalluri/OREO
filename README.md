# Oreo — Step 1: mock sales database + read-only MCP server

This is the data layer for Oreo. It creates a fake sales database for a fictional
company, **Lumen Desk Co.** (a B2B seller of desks, chairs, lighting, tech and
accessories), and serves it to Claude through a **read-only MCP server**.
Everything here is mock data. No real company systems are involved.

```
oreo/
├── README.md
├── requirements.txt
├── .gitignore
├── data/
│   ├── create_mock_data.py   # builds data/sales.db (standard library only)
│   └── sales.db              # generated, git-ignored
└── mcp_server/
    ├── sales_server.py       # the read-only MCP server (6 tools)
    └── smoke_test.py         # starts the server like Claude Desktop does and calls every tool
```

## What's in the data

About 2 years of daily orders ending today: ~26,000 orders, ~70,000 order lines,
1,200 business customers, 30 products in 5 categories, 5 regions, 4 channels and
3 customer segments (Enterprise, Mid-Market, Small Business). It includes ~18%
yearly growth, seasonality (quiet January, back-to-school August, strong Q4, a
Black Friday to Cyber Monday spike, quiet weekends), a 4% price increase in year 2,
a lamp launched mid-way (Halo Smart Lamp) and one discontinued (Glow Desk Lamp Mini).

Revenue means net of discounts, completed orders only (returns and cancellations
are left out). Gross profit is revenue minus unit cost.

## The six tools

| Tool | What it answers |
| --- | --- |
| `data_freshness` | Which company, the date range covered, when the data was generated, row counts |
| `list_dimensions` | Valid regions, channels, categories, products, segments, metrics and period names |
| `get_sales_summary` | Revenue, orders, units, customers, average order value, gross profit and margin for a period |
| `compare_periods` | Two periods side by side with the change and % change (e.g. this year vs same period last year) |
| `top_items` | Top products, categories, regions, channels, customers or segments by any metric |
| `sales_trend` | A metric by day, week, month, quarter or year, ready for a chart |

Periods can be named (`last_quarter`, `this_year`, `same_period_last_year`,
`last_30_days`, … see `list_dimensions`) or given as `start_date` / `end_date`
(`YYYY-MM-DD`). Named periods count back from the latest date in the data. Every
tool takes optional filters: `region`, `channel`, `category`, `product`, `segment`.
Every answer includes `source` and `as_of`, so Oreo can always say where a number
came from and how current it is.

**Read-only and validated:** the database is opened with SQLite's read-only mode
plus `PRAGMA query_only`; filter values must match real names (case doesn't matter);
metrics and groupings come from fixed allowlists; all values are bound SQL
parameters; date ranges, `limit` (1–25) and chart size (400 points) are capped.
Bad input gets a clear error that lists the valid choices.

## Setup on the MacBook (macOS)

You need Python 3.11 or newer. macOS's built-in `python3` may be older, so check
first and install a newer one with Homebrew if needed:

```bash
python3 --version              # want 3.11+
brew install python@3.12       # only if it's older; then use python3.12 below
```

Then, from the `oreo` folder:

```bash
cd ~/oreo                                  # wherever you put this folder
python3 -m venv .venv                      # create a virtual environment (once)
source .venv/bin/activate                  # activate it (each new terminal)
pip install -r requirements.txt            # installs the MCP Python SDK
```

### 1. Create the database

```bash
python data/create_mock_data.py
```

Prints the date range and row counts and writes `data/sales.db` (~6 MB, takes about
a second). Options: `--end-date 2026-09-30`, `--days 365`, `--seed 7`, `--out path`.
The same seed and end date always give the same data. Re-run it any time to roll the
data forward to today.

### 2. Run the smoke test

```bash
python mcp_server/smoke_test.py
```

This starts the server the same way Claude Desktop will, lists the tools, calls
each one with sample questions, and checks that bad input (an invalid date, an
unknown region, a made-up grouping, an oversized request) is rejected. It should
end with `All checks passed.`

You can also start the server by itself with `python mcp_server/sales_server.py`.
It will log one line and then sit silently waiting for a client; that's normal.
Press Ctrl+C to stop it.

## Test it in Claude Desktop

1. Get the two absolute paths you'll need (with the virtual environment active):

   ```bash
   which python                             # e.g. /Users/you/oreo/.venv/bin/python
   realpath mcp_server/sales_server.py      # e.g. /Users/you/oreo/mcp_server/sales_server.py
   ```

2. In Claude Desktop open **Settings → Developer → Edit Config**. That opens
   `~/Library/Application Support/Claude/claude_desktop_config.json`. Add the
   server (keep any other servers already in `mcpServers`):

   ```json
   {
     "mcpServers": {
       "oreo-sales": {
         "command": "/Users/you/oreo/.venv/bin/python",
         "args": ["/Users/you/oreo/mcp_server/sales_server.py"]
       }
     }
   }
   ```

   Use the venv's Python (not plain `python3`) so the MCP SDK is found. If you keep
   the database somewhere else, add `"env": {"OREO_SALES_DB": "/full/path/sales.db"}`.

3. Quit Claude Desktop completely (Cmd+Q) and reopen it. In a new chat, open the
   tools/connectors menu below the message box; `oreo-sales` should be listed with
   its six tools. Claude asks for permission the first time it uses each tool.

4. Try these questions:
   - "How current is the sales data, and whose data is it?"
   - "What were our top 5 products last quarter?"
   - "How are we doing this year compared to the same period last year?"
   - "Show monthly revenue for the last year." (Claude may draw a chart)
   - "Which region has the best gross margin this quarter?"
   - "How did the Halo Smart Lamp do since it launched?"
   - "Who are our top 3 Enterprise customers by revenue this year?"
   - "What were sales in Antarctica?" (should politely say there's no such region)

**If the server doesn't appear:** check the paths in the config are absolute and
correct, and look at the server log at `~/Library/Logs/Claude/mcp-server-oreo-sales.log`.
"Database not found" means step 1 above hasn't been run (or `OREO_SALES_DB` is wrong).

## What's next

Step 2 (Oreo's brain) builds the Python orchestrator that connects to this same
server through its own MCP client, with the router and the Oreo personality.
`config.yaml` and `.env.example` from the plan's layout are added in that step,
when there's something to configure.
