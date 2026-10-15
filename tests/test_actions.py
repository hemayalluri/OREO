"""Step 3: sales reports (Excel, Word, PDF) and email that only goes out after a clear yes."""

from __future__ import annotations

import asyncio
import email
import email.policy
import json
from pathlib import Path

import pytest

from backend.emailer import Emailer, EmailError
from backend.mcp_gateway import McpGateway
from backend.models import ModelAdapter
from backend.orchestrator import Orchestrator
from backend import reports
from conftest import FakeClient, is_router, response, route_json, text_block


def action(task="report", period="last_month", formats=(), recipient="", region="", start="", end=""):
    return response(text_block(json.dumps({
        "task": task, "period": period, "start_date": start, "end_date": end, "region": region,
        "formats": list(formats), "recipient": recipient})))


def chat(settings, actions, questions):
    """Ask questions in order; `actions` are the scripted action-extraction replies, in order."""
    queue = list(actions)

    def script(kw):
        if is_router(kw):
            return route_json("action")
        return queue.pop(0)

    async def go():
        client = FakeClient(script)
        async with McpGateway(settings.mcp_servers, settings.log_dir / "audit.jsonl") as gw:
            oreo = Orchestrator(settings, ModelAdapter(client), gw)
            return [await oreo.ask(q) for q in questions], client

    return asyncio.run(go())


def outbox(settings) -> list[Path]:
    folder = Path(settings.raw["email"]["outbox_dir"])
    return sorted(folder.glob("*.eml")) if folder.exists() else []


def test_report_in_all_three_formats_uses_only_tool_numbers(settings):
    (answer,), client = chat(settings, [action(formats=["pdf", "docx", "xlsx"])], ["Make me last month's report"])
    assert answer.route == "action" and not answer.error
    assert sorted(Path(f).suffix for f in answer.files) == [".docx", ".pdf", ".xlsx"]
    folder = Path(settings.raw["reports"]["dir"])
    assert all((folder / f).stat().st_size > 1000 for f in answer.files)
    assert "ready as a PDF and a Word document and an Excel file" in answer.text
    assert "Want me to email it?" in answer.text
    # Only the router and the field extraction used the model; the numbers came from MCP tools.
    assert len(client.calls) == 2
    audit = (settings.log_dir / "audit.jsonl").read_text()
    assert "get_sales_summary" in audit and "top_items" in audit and "sales_trend" in audit


def test_report_numbers_match_the_sales_tool(settings):
    async def go():
        async with McpGateway(settings.mcp_servers, settings.log_dir / "audit.jsonl") as gw:
            data = await reports.gather(gw, period="last_quarter", region="europe")
            direct = await gw.call("get_sales_summary", {"period": "last_quarter", "region": "Europe"})
            return data, direct.data
    data, direct = asyncio.run(go())
    assert data.summary == direct["summary"] and data.filters == {"region": "Europe"}
    assert data.breakdown_label == "Channel" and data.highlights[0].startswith("Revenue was $")


def test_unknown_region_is_explained_not_crashed(settings):
    (answer,), _ = chat(settings, [action(region="Mars")], ["Report for Mars"])
    assert answer.error and "Unknown region 'Mars'" in answer.text and not answer.files


def test_email_waits_for_a_clear_yes_then_goes_to_the_outbox(settings):
    answers, client = chat(settings, [action("email_report", recipient="me")],
                           ["Email me last month's report", "Yes, send it"])
    drafted, sent = answers
    assert "Should I email it to boss@example-test-domain.com?" in drafted.text
    assert "outbox" in sent.text and len(client.calls) == 2   # the yes needed no model call
    (eml,) = outbox(settings)
    msg = email.message_from_bytes(eml.read_bytes(), policy=email.policy.default)
    assert msg["To"] == "boss@example-test-domain.com" and "sales report" in msg["Subject"].lower()
    assert [p.get_filename() for p in msg.iter_attachments()][0].endswith(".pdf")


@pytest.mark.parametrize("reply", ["No, don't send it", "What were sales in Europe?", "Yes, but change the subject"])
def test_anything_but_a_clear_yes_sends_nothing(settings, reply):
    chat(settings, [action("email_report", recipient="me"), action("unsupported")],
         ["Email me last month's report", reply])
    assert outbox(settings) == []


def test_email_it_to_me_after_a_report_reuses_that_report(settings):
    answers, _ = chat(settings, [action("report", formats=["xlsx"]), action("email_previous_report", recipient="me")],
                      ["Make last month's report in Excel", "Email it to me", "yes"])
    assert answers[1].files == answers[0].files and "Should I email it" in answers[1].text
    (eml,) = outbox(settings)
    assert [p.get_filename() for p in email.message_from_bytes(eml.read_bytes(), policy=email.policy.default).iter_attachments()] == answers[0].files


def test_other_domains_are_refused(settings):
    answers, _ = chat(settings, [action("email_report", recipient="someone@gmail.com"), action("unsupported")],
                      ["Email the report to someone@gmail.com", "yes"])
    assert "I can only email addresses at example-test-domain.com" in answers[0].text
    assert outbox(settings) == []


def test_emailer_rejects_lookalike_domains(settings, tmp_path):
    mailer = Emailer({"allowed_domains": ["example-test-domain.com"]}, tmp_path)
    assert mailer.check_recipient("Sam@Sales.Example-Test-Domain.com") == "sam@sales.example-test-domain.com"
    for bad in ("sam@example-test-domain.com.evil.io", "sam@notexample-test-domain.com", "not an email"):
        with pytest.raises(EmailError):
            mailer.check_recipient(bad)
