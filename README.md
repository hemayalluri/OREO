# Oreo

Oreo is a desk robot that answers sales questions from company data, answers general questions, looks up live info online, and makes and emails reports. This repo is built in steps; each one ends in something runnable.

- **Step 1:** mock sales database + read-only MCP server (below)
- **Step 2:** Oreo's brain, a text chat in the terminal ([jump to it](#step-2-oreos-brain-text-chat))
- **Steps 4–5 (first part):** Oreo's face page and tap-to-talk voice ([jump to it](#steps-45-oreos-face-and-voice))
- **Step 3:** sales reports (PDF, Word, Excel) and email after a clear "yes" ([jump to it](#step-3-reports-and-email))

# Step 1: mock sales database + read-only MCP server

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

# Step 2: Oreo's brain (text chat)

Ask Oreo questions in the terminal. Each question is routed, answered by the
right model, and comes back with its source and "as of" date.

```
backend/
├── config.py        # loads config.yaml + .env
├── models.py        # model adapter: the only place that calls an LLM
├── router.py        # company / general / mixed / action classification
├── mcp_gateway.py   # MCP client, tool allowlist, read-only check, audit log
├── prompts.py       # Oreo's personality and per-path rules
├── orchestrator.py  # routing, tool loop, shared history, idle timeout
└── chat.py          # terminal chat
config.yaml          # models, routing, MCP servers, timeouts
.env.example         # copy to .env and add your API key
tests/               # runs without an API key (scripted fake model)
```

**How a question flows**

1. The **router** (Claude Haiku 5.5) labels it `company`, `general`, `mixed` or `action`.
   If it's unsure, or the router call fails, the question takes the company path,
   because that's the secure one.
2. **Company:** Claude gets the six sales tools through the **MCP gateway**. Budget
   mode uses Haiku 5.5 for everything; with `company_model` set to Sonnet 5.5, only
   simple one-number lookups stay on Haiku. The gateway only
   exposes allowlisted tools that declare themselves read-only, and logs every call to
   `logs/audit.jsonl`.
3. **Mixed:** the company path, plus web search when `web_search_for_mixed` is on.
4. **General:** Haiku 5.5 with no tools. It never sees company data: earlier company
   answers in the history are replaced with `[withheld]`
   (`share_company_answers_with_general` in `config.yaml`).
5. **Action** (reports and email) replies that the skill is coming; step 3 builds it.

History is shared across routes so follow-ups ("and last year?") work, and resets after
2 minutes idle (`session_timeout_seconds`). Errors become short spoken-style messages,
such as "I can't reach the sales system right now." Every exchange is logged to
`logs/conversations.jsonl`.

**Cost controls (budget mode is the default):** every route uses Claude Haiku 5.5
($0.10 / $0.50 per million input/output tokens) at low effort; web search is off;
each answer is capped at 4,000 tokens and 5 tool rounds per question; the system
prompt and tool definitions are cached. A typical question costs well under a cent.
For harder analysis, set `company_model: claude-sonnet-5-5` in `config.yaml`
(about 20x the price per token). Always set a monthly spend limit in the Claude Console.

**Refusal fallback:** if you switch to Sonnet 5.5, its requests opt into the API's server-side fallback
(`fallbacks: "default"`), so if a safety check declines a harmless request, another
model answers instead of Oreo going silent. Haiku has no such fallback; Oreo says
politely that it can't help.

## Run it

```bash
source .venv/bin/activate
pip install -r requirements.txt          # adds the Anthropic SDK, PyYAML, python-dotenv, pytest
cp .env.example .env                     # then paste your API key into .env
python data/create_mock_data.py          # if you haven't yet
python -m backend.chat                   # add --debug to see routing and tool calls
```

Get an API key at https://platform.claude.com (Settings → API keys) and set a
monthly spending limit there before you start.

Things to try:

- "What were our top 3 products last quarter?" → company path, `top_items`
- "And how about in Europe?" → follow-up stays on the company path
- "How is this year going compared with last year?" → `compare_periods`
- "Tell me a fun fact about octopuses." → general path, no tools
- "Are our lighting sales keeping up with the smart-lamp market?" → mixed (web search only if enabled)
- "Email me a sales report." → action (coming in step 3)
- `/reset` clears the conversation, `/quit` exits

Below each answer the chat shows the route, model, tools used and data date.

## Tests

```bash
python -m pytest
```

The tests start the real MCP server on a small generated database and replace
Claude with a scripted fake, so they need no API key and cost nothing. They cover
routing (including the secure fallback), the tool allowlist and read-only checks,
the audit log, withholding company answers from the general path, the idle
timeout, the tool-round cap and friendly error messages.

# Steps 4–5: Oreo's face and voice

Talk to Oreo in a browser: tap its face, ask out loud, and it answers out loud
while the face changes expression.

```
backend/main.py     # web server: face page, /api/transcribe, /ws (live state + answers)
backend/speech.py   # local speech-to-text with faster-whisper
face_app/           # the face page: index.html, face.css, app.js
```

**How it works**

1. **Tap the face.** Oreo wakes up, says hi, and starts listening. The eyes pulse while it records.
2. **Ask your question.** Recording stops by itself after a short pause, or when you tap again.
3. **Speech to text, on your Mac.** The audio goes to `/api/transcribe`, where
   **faster-whisper** turns it into text locally. It's free, works offline, and the
   audio never leaves the Mac.
4. **Thinking.** The question goes to the brain over a WebSocket. As soon as it's
   routed, the face shows *Thinking* and, for sales questions, says
   "Let me check the sales data."
5. **Answering.** The answer is spoken with the device's built-in voice (free,
   offline on macOS and iPadOS) and shown as a caption with its source and date.
6. **Follow-ups.** Tap again to ask a follow-up. After 2 minutes idle, Oreo goes back to sleep.

You can also type questions in the box at the bottom, which is handy when you're in a quiet office.

**Faces:** Sleeping, Listening, Thinking and Speaking (the Authorized face with a moving
mouth) are in use now. Scanning and Rejected are drawn and wait for face verification (step 6).

## Run it

```bash
source .venv/bin/activate
pip install -r requirements.txt     # adds FastAPI, Uvicorn and faster-whisper
python -m backend.main              # then open http://localhost:8000 in Chrome or Safari
```

- The first spoken question downloads the Whisper model (about 150 MB for `base.en`)
  and then it's cached. Change `voice.stt_model` in `config.yaml` to `tiny.en` for
  speed or `small.en` for accuracy.
- Allow microphone access when the browser asks.
- Pick a voice with `voice.tts_voice` (for example `"Samantha"`). Better voices can be
  downloaded for free in macOS System Settings → Accessibility → Spoken Content.
- Each question costs the same as in the text chat: one Haiku routing call plus the
  answer. Speech costs nothing.

**On the iPad (later, step 8):** browsers only allow the microphone on `localhost` or
over HTTPS, so the iPad will need an HTTPS address for the Mac. Step 8 sets that up.

**Still to come for voice:** the "Hey Oreo" wake word (openWakeWord, free and local) so
you don't need to tap, and optionally a Piper voice for a custom Oreo sound.

## Tests

`tests/test_server.py` covers the face page, the transcription endpoint (with a
stand-in for Whisper) and the WebSocket flow (thinking state, filler line, answer,
reset). Like the other tests, it needs no API key and no microphone.

# Step 3: reports and email

Ask Oreo for a report by voice or text, and it builds one from the sales tools:

- "Make me last month's sales report." → a PDF (the default)
- "Europe report for last quarter, as a Word document and an Excel file."
- "Email me the sales report for this quarter." → builds it, then asks "Should I email it to …? Just say yes to send it."
- "Email it to me." right after a report → emails that same report.

**How it stays trustworthy and cheap**

- Every number comes straight from the read-only MCP tools (`get_sales_summary`, `compare_periods`,
  `top_items`, `sales_trend`). The highlights are written by code from those numbers, never by the
  model, so a report can't contain an invented figure. Building one costs nothing; the only model
  call is a small Haiku request that reads what you asked for (period, region, formats, recipient).
- Each report has highlights, a summary table compared with the previous period of the same length,
  a revenue chart, the top 10 products and a breakdown by region (or by channel for a one-region report).
  Files are saved in `reports/`, and the face page shows download links under the answer.

**Email safety (enforced in code, `backend/emailer.py` and `backend/actions.py`)**

- Nothing is sent when a draft is made. Only a clear "yes" / "yes, send it" / "go ahead" as the very
  next message sends it (`phrases.is_confirmation`). "No", "yes, but…", another question, or 3 minutes
  of silence drop the draft unsent. The yes is checked by code, so it needs no model call.
- Recipients must be in `email.allowed_domains`; lookalikes such as `example-test-domain.com.evil.io` are refused.
- `email.mode: outbox` (the default) saves each email as an `.eml` file in `outbox/` instead of sending,
  so the whole flow can be tested with no mail account. To really send, set `email.mode: smtp` and put
  `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` (an app password) and `EMAIL_FROM` in `.env`.
- Set `email.default_recipient` to your address for "email it to me".

## What's next

Connecting Oreo to the real sales system instead of the mock database, then face
verification (step 6) so Oreo knows who it's talking to before sharing company data.
