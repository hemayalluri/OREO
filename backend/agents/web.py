"""
Web agent: the live internet (web search) and general knowledge.

It never receives company data: its conversation history has earlier company answers
replaced by "[withheld]" (unless config allows sharing), and it has no sales tools.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from ..models import ModelAdapter, text_of
from ..prompts import general_system, live_system
from .base import AgentReply

log = logging.getLogger("oreo.agents.web")

# The basic search variant works on Haiku (about 1 cent per search).
SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}


class WebAgent:
    name = "web"

    def __init__(self, settings: Any, models: ModelAdapter):
        self.settings = settings
        self.models = models
        self.persona = (settings.oreo.get("name", "Oreo"), settings.oreo.get("boss_name", "boss"))

    async def answer(self, question: str, history: list[dict[str, Any]], live: bool,
                     part_only: bool = False) -> AgentReply:
        """live: search the internet for current information; otherwise answer from general knowledge.
        part_only: the company-data agent handles the company part of the question."""
        r = self.settings.routing
        effort = r.get("effort", {}).get("general")
        max_tokens = r.get("max_answer_tokens", 4000)
        messages = history + [{"role": "user", "content": question}]

        if not live or not r.get("web_search_live", True):
            model = r["general_model"]
            msg = await self.models.create(model=model, system=general_system(*self.persona),
                                           messages=messages, effort=effort, max_tokens=max_tokens)
            if msg.stop_reason == "refusal":
                return AgentReply("Sorry, that's not something I can help with.", self.name, model)
            return AgentReply(text_of(msg) or "Hmm, I'm not sure what to say to that.", self.name, model)

        model = r.get("live_model", r["general_model"])
        tool = {**SEARCH_TOOL, "max_uses": int(r.get("web_search_max_uses", 1))}
        system = live_system(*self.persona, datetime.now().strftime("%A, %B %-d, %Y"), tool["max_uses"], part_only)
        for _ in range(3):  # a long search can pause the turn; resume it a couple of times at most
            msg = await self.models.create(model=model, system=system, tools=[tool], messages=messages,
                                           effort=effort, max_tokens=max_tokens)
            if msg.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": msg.content})
        if msg.stop_reason == "refusal":
            return AgentReply("Sorry, that's not something I can help with.", self.name, model)
        sources: list[str] = []
        for block in msg.content:
            if getattr(block, "type", None) == "web_search_tool_result" and isinstance(block.content, list):
                for result in block.content[:2]:
                    host = urlparse(getattr(result, "url", "") or "").netloc.removeprefix("www.")
                    if host and host not in sources:
                        sources.append(host)
        text = text_of(msg) or "I tried looking that up, but couldn't find a clear answer."
        return AgentReply(text, self.name, model, sources, None, ["web_search"] if sources else [])
