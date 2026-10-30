"""
Orchestrator: Oreo's brain for one conversation.

question -> router -> (company path: Claude + MCP tools through the gateway)
                   -> (general path: general model, no tools, no company data)
                   -> answer with route, model, source and "as of" date

Conversation history is shared across routes so follow-ups work, with one rule:
the general path sees company answers as "[withheld]" unless config allows it.
History resets after `session_timeout_seconds` of idle time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from urllib.parse import urlparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .config import Settings
from .mcp_gateway import McpGateway
from .models import ModelAdapter, ModelError, text_of
from .actions import Actions, ActionResult
from .prompts import company_system, general_system, live_system
from .router import RouteDecision, Router

log = logging.getLogger("oreo.orchestrator")

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
# Live questions use the basic search variant: it works on Haiku and needs just one search (about 1 cent).
LIVE_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}
WITHHELD = "[withheld: this answer came from company data]"


@dataclass
class Answer:
    text: str
    route: str
    model: str | None = None
    sources: list[str] = field(default_factory=list)
    as_of: str | None = None
    tools_used: list[str] = field(default_factory=list)
    error: bool = False
    route_reason: str = ""
    files: list[str] = field(default_factory=list)   # report files made for this answer (names in reports/)


SOURCE_NOTE = "\n\n[Internal note, not spoken: the figures above came from the sales database tools ({tools}), data as of {as_of}.]"
WEB_NOTE = "\n\n[Internal note, not spoken: the answer above came from a live web search just now (sources: {sources}).]"

@dataclass
class Turn:
    question: str
    answer: str
    route: str
    tools_used: list[str] = field(default_factory=list)
    as_of: str | None = None
    sources: list[str] = field(default_factory=list)


class Orchestrator:
    def __init__(self, settings: Settings, models: ModelAdapter, gateway: McpGateway, clock=time.monotonic):
        self.settings = settings
        self.models = models
        self.gateway = gateway
        self.clock = clock
        r = settings.routing
        self.router = Router(models, r["router_model"], r.get("effort", {}).get("router", "low"),
                             r.get("default_when_unsure", "company"))
        self.name = settings.oreo.get("name", "Oreo")
        self.boss = settings.oreo.get("boss_name", "boss")
        self.timeout = settings.oreo.get("session_timeout_seconds", 120)
        self.history: list[Turn] = []
        self.session_id = uuid.uuid4().hex[:12]
        self._last_activity = self.clock()
        self.actions = Actions(settings, models, gateway)

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #

    def reset(self) -> None:
        self.history = []
        self.session_id = uuid.uuid4().hex[:12]
        if hasattr(self, "actions"):
            self.actions.reset()

    def _touch(self) -> None:
        now = self.clock()
        if self.history and now - self._last_activity > self.timeout:
            log.info("Session idle for over %ss; starting fresh", self.timeout)
            self.reset()
        self._last_activity = now

    def _history_messages(self, for_route: str) -> list[dict[str, Any]]:
        share = self.settings.routing.get("share_company_answers_with_general", False)
        messages = []
        for turn in self.history:
            answer = turn.answer
            if for_route == "general" and turn.route in ("company", "mixed") and not share:
                answer = WITHHELD
            elif turn.route == "live" and turn.tools_used:
                answer += WEB_NOTE.format(sources=", ".join(turn.sources) or "the web")
            elif turn.tools_used:
                # History keeps only the spoken answers, so note where their numbers came from;
                # otherwise the model may later decide it "guessed" them and retract correct figures.
                answer += SOURCE_NOTE.format(tools=", ".join(turn.tools_used), as_of=turn.as_of or "latest data")
            messages.append({"role": "user", "content": turn.question})
            messages.append({"role": "assistant", "content": answer})
        return messages

    # ------------------------------------------------------------------ #
    # Main entry point
    # ------------------------------------------------------------------ #

    async def ask(self, question: str, on_route: Callable[[RouteDecision], Awaitable[None]] | None = None) -> Answer:
        """Answer one question. `on_route` is called once the route is known (the face page
        uses it to show the Thinking face and say a filler line before the answer is ready)."""
        question = question.strip()
        self._touch()
        # An email waiting for a yes: settled by code before anything else (no model call).
        pending = await self.actions.resolve_pending(question)
        if pending is not None:
            answer = self._from_action(pending)
            self._finish(question, answer)
            return answer
        decision = await self.router.route(question, [t.question for t in self.history])
        log.info("Route: %s (%s)", decision.route, decision.reason)
        if on_route:
            await on_route(decision)
        try:
            if decision.route == "action":
                if not self.gateway.available:
                    answer = Answer("I can't reach the sales system right now.", "action", error=True)
                else:
                    answer = self._from_action(await self.actions.handle(
                        question, [t.question for t in self.history], self.session_id))
            elif decision.route in ("company", "mixed"):
                answer = await self._company(question, decision)
            elif decision.route == "live":
                answer = await self._live(question)
            else:
                answer = await self._general(question)
        except ModelError as exc:
            log.error("Model error: %s", exc)
            answer = Answer(exc.spoken, decision.route, error=True)
        answer.route_reason = decision.reason
        self._finish(question, answer)
        return answer

    def _finish(self, question: str, answer: Answer) -> None:
        if not answer.error:
            self.history.append(Turn(question, answer.text, answer.route, list(answer.tools_used),
                                     answer.as_of, list(answer.sources)))
        self._log(question, answer)

    def _from_action(self, result: ActionResult) -> Answer:
        model = self.settings.routing.get("action_model", self.settings.routing["general_model"])
        return Answer(result.text, "action", model, result.sources, result.as_of, result.tools_used,
                      result.error, files=[f.name for f in result.files])

    # ------------------------------------------------------------------ #
    # Paths
    # ------------------------------------------------------------------ #

    async def _general(self, question: str) -> Answer:
        r = self.settings.routing
        model = r["general_model"]
        msg = await self.models.create(
            model=model,
            system=general_system(self.name, self.boss),
            messages=self._history_messages("general") + [{"role": "user", "content": question}],
            effort=r.get("effort", {}).get("general"),
            max_tokens=r.get("max_answer_tokens", 4000),
        )
        if msg.stop_reason == "refusal":
            return Answer("Sorry, that's not something I can help with.", "general", model)
        return Answer(text_of(msg) or "Hmm, I'm not sure what to say to that.", "general", model)

    async def _live(self, question: str) -> Answer:
        """Current-events questions: the general model plus one web search. Never sees company data."""
        r = self.settings.routing
        if not r.get("web_search_live", True):
            answer = await self._general(question)
            answer.route = "live"
            return answer
        model = r.get("live_model", r["general_model"])
        tool = {**LIVE_SEARCH_TOOL, "max_uses": int(r.get("web_search_max_uses", 1))}
        today = datetime.now().strftime("%A, %B %-d, %Y")
        messages = self._history_messages("general") + [{"role": "user", "content": question}]
        for _ in range(3):  # a long search can pause the turn; resume it a couple of times at most
            msg = await self.models.create(model=model, system=live_system(self.name, self.boss, today, tool["max_uses"]),
                                           tools=[tool], messages=messages,
                                           effort=r.get("effort", {}).get("general"),
                                           max_tokens=r.get("max_answer_tokens", 4000))
            if msg.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": msg.content})
        if msg.stop_reason == "refusal":
            return Answer("Sorry, that's not something I can help with.", "live", model)
        sources = []
        for block in msg.content:
            if getattr(block, "type", None) == "web_search_tool_result" and isinstance(block.content, list):
                for result in block.content[:2]:
                    host = urlparse(getattr(result, "url", "") or "").netloc.removeprefix("www.")
                    if host and host not in sources:
                        sources.append(host)
        text = text_of(msg) or "I tried looking that up, but couldn't find a clear answer."
        return Answer(text, "live", model, sources, None, ["web_search"] if sources else [])

    async def _company(self, question: str, decision: RouteDecision) -> Answer:
        r = self.settings.routing
        if not self.gateway.available:
            return Answer("I can't reach the sales system right now.", decision.route, error=True)

        simple = decision.route == "company" and decision.complexity == "simple"
        model = r["company_simple_model"] if simple else r["company_model"]
        effort = r.get("effort", {}).get("company_simple" if simple else "company")
        web = decision.route == "mixed" and r.get("web_search_for_mixed", True)
        tools = self.gateway.tools + ([WEB_SEARCH_TOOL] if web else [])
        system = company_system(self.name, self.boss, "the company's sales database", web=web)
        messages = self._history_messages("company") + [{"role": "user", "content": question}]

        sources: list[str] = []
        as_of: str | None = None
        used: list[str] = []
        for _ in range(r.get("max_tool_rounds", 8)):
            msg = await self.models.create(model=model, system=system, tools=tools, messages=messages,
                                           effort=effort, max_tokens=r.get("max_answer_tokens", 4000))
            if msg.stop_reason == "refusal":
                return Answer("Sorry, I can't help with that one.", decision.route, model)
            # Append the full response (thinking, tool calls, server-tool results) unchanged.
            messages.append({"role": "assistant", "content": msg.content})
            if msg.stop_reason == "pause_turn":
                continue
            calls = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
            if msg.stop_reason != "tool_use" or not calls:
                break
            results = await asyncio.gather(
                *(self.gateway.call(c.name, dict(c.input or {}), self.session_id) for c in calls))
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
            return Answer("That question needed more lookups than I'm allowed. Could you narrow it down?",
                          decision.route, model, sources, as_of, used, error=True)

        text = text_of(msg) or "I looked, but I couldn't put together an answer. Could you ask another way?"
        return Answer(text, decision.route, model, sources, as_of, used)

    # ------------------------------------------------------------------ #

    def _log(self, question: str, answer: Answer) -> None:
        path = self.settings.log_dir / "conversations.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "session": self.session_id, "question": question, **asdict(answer)}
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
