"""Step 2 tests: router, MCP gateway and orchestrator, with a scripted fake model.

These run without an API key. The MCP server is the real one, on a small test database.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import anthropic
import httpx2

from backend.mcp_gateway import McpGateway
from backend.models import ModelAdapter
from backend.orchestrator import WITHHELD, Orchestrator
from conftest import FakeClient, is_router, response, route_json, text_block, tool_use


def run(coro):
    return asyncio.run(coro)


async def ask_all(settings, script, questions, clock=None):
    client = FakeClient(script)
    async with McpGateway(settings.mcp_servers, settings.log_dir / "audit.jsonl") as gw:
        kwargs = {"clock": clock} if clock else {}
        oreo = Orchestrator(settings, ModelAdapter(client), gw, **kwargs)
        answers = [await oreo.ask(q) for q in questions]
    return answers, client


# --------------------------------------------------------------------------- #
# Gateway
# --------------------------------------------------------------------------- #

def test_gateway_exposes_only_allowlisted_tools_and_audits(settings):
    settings.mcp_servers  # noqa: B018 - make sure config parses
    settings.raw["mcp"]["servers"][0]["allowed_tools"] = ["data_freshness", "top_items"]

    async def go():
        async with McpGateway(settings.mcp_servers, settings.log_dir / "audit.jsonl") as gw:
            names = [t["name"] for t in gw.tools]
            ok = await gw.call("data_freshness", {}, "s1")
            blocked = await gw.call("get_sales_summary", {}, "s1")
            return names, ok, blocked

    names, ok, blocked = run(go())
    assert names == ["data_freshness", "top_items"]
    assert not ok.is_error and ok.data["company"] == "Lumen Desk Co."
    assert blocked.is_error
    audit = [json.loads(line) for line in (settings.log_dir / "audit.jsonl").read_text().splitlines()]
    assert [(a["tool"], a["ok"]) for a in audit] == [("data_freshness", True), ("get_sales_summary", False)]
    assert audit[0]["session"] == "s1"


def test_gateway_refuses_tools_without_read_only_hint(settings, monkeypatch):
    from backend import mcp_gateway

    class NoHint:
        read_only_hint = None

    tool = type("T", (), {"name": "data_freshness", "annotations": NoHint()})()
    server = settings.mcp_servers[0]
    assert mcp_gateway.McpGateway._refusal_reason(server, tool) is not None


# --------------------------------------------------------------------------- #
# Orchestrator: company path
# --------------------------------------------------------------------------- #

def test_company_question_uses_tools_and_reports_as_of(settings):
    settings.raw["routing"]["company_model"] = "claude-sonnet-5-5"
    def script(kw):
        if is_router(kw):
            return route_json("company")
        last = kw["messages"][-1]
        if isinstance(last["content"], str):  # first call: ask for a tool
            return response(tool_use("top_items", {"dimension": "product", "period": "last_quarter", "limit": 3}),
                            stop_reason="tool_use")
        result = json.loads(last["content"][0]["content"])
        top = result["items"][0]["name"]
        return response(text_block(f"According to the sales database, your top product was the {top}."))

    (answer,), client = run(ask_all(settings, script, ["What were our top products last quarter?"]))
    assert answer.route == "company" and not answer.error
    assert answer.model == "claude-sonnet-5-5"
    assert answer.tools_used == ["top_items"]
    assert answer.as_of == "2026-10-14"
    assert "Lumen Desk Co." in answer.sources[0]
    assert "top product was the" in answer.text
    company_call = client.calls[1]
    assert {t["name"] for t in company_call["tools"]} >= {"get_sales_summary", "sales_trend"}
    assert company_call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in company_call["betas"]


def test_simple_company_question_uses_cheaper_model(settings):
    def script(kw):
        if is_router(kw):
            return route_json("company", complexity="simple")
        return response(text_block("About 19 million dollars."))

    settings.raw["routing"]["company_model"] = "claude-sonnet-5-5"
    (answer,), client = run(ask_all(settings, script, ["Revenue last quarter?"]))
    assert answer.model == "claude-haiku-5-5"
    assert "fallbacks" not in client.calls[1]  # Haiku has no server-side fallback


def test_tool_errors_are_returned_to_the_model(settings):
    seen = {}

    def script(kw):
        if is_router(kw):
            return route_json("company")
        last = kw["messages"][-1]
        if isinstance(last["content"], str):
            return response(tool_use("get_sales_summary", {"region": "Mars"}), stop_reason="tool_use")
        seen.update(last["content"][0])
        return response(text_block("There's no region called Mars."))

    (answer,), _ = run(ask_all(settings, script, ["Sales on Mars?"]))
    assert seen["is_error"] is True and "Unknown region" in seen["content"]
    assert "Mars" in answer.text


def test_mixed_question_adds_web_search(settings):
    settings.raw["routing"]["web_search_for_mixed"] = True
    def script(kw):
        if is_router(kw):
            return route_json("mixed")
        return response(text_block("Our chairs grew faster than the market."))

    (answer,), client = run(ask_all(settings, script, ["Are our chair sales beating the industry?"]))
    assert answer.route == "mixed"
    assert any(t.get("type") == "web_search_20260209" for t in client.calls[1]["tools"])


def test_tool_round_cap(settings):
    settings.raw["routing"]["max_tool_rounds"] = 2

    def script(kw):
        if is_router(kw):
            return route_json("company")
        return response(tool_use("data_freshness", {}), stop_reason="tool_use")

    (answer,), _ = run(ask_all(settings, script, ["Loop forever"]))
    assert answer.error and "narrow it down" in answer.text


# --------------------------------------------------------------------------- #
# Routing, history and failures
# --------------------------------------------------------------------------- #

def test_general_path_has_no_tools_and_never_sees_company_answers(settings):
    def script(kw):
        if is_router(kw):
            latest = kw["messages"][-1]["content"].split("Latest message:")[-1]
            return route_json("company" if "revenue" in latest else "general")
        if kw.get("tools"):
            return response(text_block("Revenue was 19 million dollars."))
        return response(text_block("Why did the robot cross the road?"))

    answers, client = run(ask_all(settings, script, ["What was revenue last quarter?", "Tell me a joke"]))
    assert [a.route for a in answers] == ["company", "general"]
    general_call = client.calls[-1]
    assert "tools" not in general_call
    history = json.dumps(general_call["messages"])
    assert "19 million" not in history and WITHHELD in history


def test_follow_up_keeps_company_history(settings):
    def script(kw):
        if is_router(kw):
            return route_json("company")
        return response(text_block("Revenue was 19 million dollars." if len(kw["messages"]) == 1 else "Up 25 percent."))

    answers, client = run(ask_all(settings, script, ["Revenue last quarter?", "And versus last year?"]))
    second = client.calls[-1]["messages"]
    assert second[0]["content"] == "Revenue last quarter?" and "19 million" in second[1]["content"]
    # The router also sees earlier questions, so follow-ups stay on the company path.
    assert "Revenue last quarter?" in client.calls[2]["messages"][0]["content"]


def test_unsure_router_takes_company_path(settings):
    def script(kw):
        if is_router(kw):
            return route_json("general", confident=False)
        return response(text_block("ok"))

    (answer,), client = run(ask_all(settings, script, ["How are we doing?"]))
    assert answer.route == "company" and client.calls[1].get("tools")


def test_router_failure_takes_company_path(settings):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    def script(kw):
        if is_router(kw):
            return anthropic.APIConnectionError(request=req)
        return response(text_block("ok"))

    (answer,), _ = run(ask_all(settings, script, ["How are we doing?"]))
    assert answer.route == "company"


def test_model_outage_gives_a_spoken_message(settings):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    def script(kw):
        if is_router(kw):
            return route_json("general")
        return anthropic.APIConnectionError(request=req)

    (answer,), _ = run(ask_all(settings, script, ["Hi!"]))
    assert answer.error and answer.text == "I can't reach the internet right now."


def test_session_resets_after_idle_timeout(settings):
    now = [0.0]

    def clock():
        return now[0]

    def script(kw):
        if is_router(kw):
            return route_json("general")
        now[0] += 500  # each answer "takes" longer than the 120s timeout
        return response(text_block(f"seen {len(kw['messages'])} messages"))

    answers, _ = run(ask_all(settings, script, ["Hi", "Hi again"], clock=clock))
    assert answers[1].text == "seen 1 messages"


def test_conversation_log_written(settings):
    run(ask_all(settings, lambda kw: route_json("action"), ["Email me"]))
    entry = json.loads((settings.log_dir / "conversations.jsonl").read_text().splitlines()[0])
    assert entry["question"] == "Email me" and entry["route"] == "action"


def test_live_question_gets_one_web_search_and_no_company_data(settings):
    def script(kw):
        if is_router(kw):
            latest = kw["messages"][-1]["content"].split("Latest message:")[-1]
            return route_json("company" if "revenue" in latest else "live")
        if kw.get("tools") and kw["tools"][0].get("type") != "web_search_20250305":
            return response(text_block("Revenue was 19 million dollars."))
        result = SimpleNamespace(type="web_search_tool_result", content=[
            SimpleNamespace(type="web_search_result", url="https://www.weather.gov/okx", title="NWS")])
        return response(result, text_block("Per the Weather Service, it's 68 and sunny in New York."))

    answers, client = run(ask_all(settings, script, ["What was revenue last quarter?", "Weather in NYC today?"]))
    live = answers[-1]
    assert live.route == "live" and live.sources == ["weather.gov"] and "68" in live.text
    call = client.calls[-1]
    assert call["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}]
    assert "19 million" not in json.dumps(call["messages"])
    assert "Today's date is" in json.dumps(call["system"])
