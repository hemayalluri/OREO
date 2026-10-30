"""
Company data agent: the only part of Oreo connected to the internal sales database.

It answers with Claude plus the read-only MCP sales tools (through the gateway, which
enforces the tool allowlist and writes the audit log), and it owns the actions built on
company data: sales reports and emailing them (backend/actions.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..actions import Actions
from ..mcp_gateway import McpGateway
from ..models import ModelAdapter, text_of
from ..prompts import company_system
from .base import AgentReply

log = logging.getLogger("oreo.agents.company")


class CompanyDataAgent:
    name = "company"

    def __init__(self, settings: Any, models: ModelAdapter, gateway: McpGateway):
        self.settings = settings
        self.models = models
        self.gateway = gateway
        self.actions = Actions(settings, models, gateway)
        self.persona = (settings.oreo.get("name", "Oreo"), settings.oreo.get("boss_name", "boss"))

    def reset(self) -> None:
        self.actions.reset()

    @property
    def available(self) -> bool:
        return self.gateway.available

    async def answer(self, question: str, history: list[dict[str, Any]], session_id: str,
                     simple: bool = False, part_only: bool = False) -> AgentReply:
        """Answer from the sales data. part_only: the web agent handles the rest of the question."""
        r = self.settings.routing
        if not self.available:
            return AgentReply("I can't reach the sales system right now.", self.name, error=True)
        model = r["company_simple_model"] if simple else r["company_model"]
        effort = r.get("effort", {}).get("company_simple" if simple else "company")
        system = company_system(*self.persona, "the company's sales database", part_only=part_only)
        messages = history + [{"role": "user", "content": question}]

        sources: list[str] = []
        as_of: str | None = None
        used: list[str] = []
        for _ in range(r.get("max_tool_rounds", 8)):
            msg = await self.models.create(model=model, system=system, tools=self.gateway.tools,
                                           messages=messages, effort=effort,
                                           max_tokens=r.get("max_answer_tokens", 4000))
            if msg.stop_reason == "refusal":
                return AgentReply("Sorry, I can't help with that one.", self.name, model)
            # Append the full response (thinking, tool calls) unchanged.
            messages.append({"role": "assistant", "content": msg.content})
            if msg.stop_reason == "pause_turn":
                continue
            calls = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
            if msg.stop_reason != "tool_use" or not calls:
                break
            results = await asyncio.gather(
                *(self.gateway.call(c.name, dict(c.input or {}), session_id) for c in calls))
            tool_results = []
            for call, res in zip(calls, results):
                used.append(call.name)
                if res.data:
                    if res.data.get("source") and res.data["source"] not in sources:
                        sources.append(res.data["source"])
                    as_of = res.data.get("as_of") or as_of
                tool_results.append({"type": "tool_result", "tool_use_id": call.id,
                                     "content": res.text, "is_error": res.is_error})
            # All results for one assistant turn go back in a single user message.
            messages.append({"role": "user", "content": tool_results})
        else:
            return AgentReply("That question needed more lookups than I'm allowed. Could you narrow it down?",
                              self.name, model, sources, as_of, used, error=True)

        text = text_of(msg) or "I looked, but I couldn't put together an answer. Could you ask another way?"
        return AgentReply(text, self.name, model, sources, as_of, used)

    async def act(self, question: str, recent: list[str], session_id: str) -> AgentReply:
        """Reports and email (built only from company data)."""
        if not self.available:
            return AgentReply("I can't reach the sales system right now.", self.name, error=True)
        return self._from_action(await self.actions.handle(question, recent, session_id))

    async def resolve_pending(self, question: str) -> AgentReply | None:
        """An email waiting for a clear yes (checked by code, no model call)."""
        result = await self.actions.resolve_pending(question)
        return None if result is None else self._from_action(result)

    def _from_action(self, result: Any) -> AgentReply:
        r = self.settings.routing
        return AgentReply(result.text, self.name, r.get("action_model", r["general_model"]), result.sources,
                          result.as_of, result.tools_used, [f.name for f in result.files], result.error)
