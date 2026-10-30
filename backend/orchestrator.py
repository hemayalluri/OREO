"""
Coordinator: the layer on top that takes each request and routes it to Oreo's sub-agents.

                      +-- CompanyDataAgent  internal sales database (read-only MCP tools), reports, email
  request -> router --+
                      +-- WebAgent          live internet (web search) and general knowledge

  company  -> company-data agent          general -> web agent (no search)
  action   -> company-data agent (reports) live    -> web agent (with search)
  mixed    -> both agents in parallel, each answering only its part; the coordinator combines them

Conversation history is shared across turns so follow-ups work, with one rule: the web agent
sees earlier company answers as "[withheld]" unless config allows it. History resets after
`session_timeout_seconds` of idle time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .agents import AgentReply, CompanyDataAgent, WebAgent
from .config import Settings
from .mcp_gateway import McpGateway
from .models import ModelAdapter, ModelError, text_of
from .prompts import combine_system
from .router import RouteDecision, Router

log = logging.getLogger("oreo.orchestrator")

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
    agents: list[str] = field(default_factory=list)  # which sub-agents worked on it ("company", "web")


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
    """The coordinator: one per conversation."""
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
        self.company = CompanyDataAgent(settings, models, gateway)
        self.web = WebAgent(settings, models)

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #

    def reset(self) -> None:
        self.history = []
        self.session_id = uuid.uuid4().hex[:12]
        if hasattr(self, "company"):
            self.company.reset()

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
        """Answer one request. `on_route` is called once the route is known (the face page
        uses it to show the Thinking face and say a filler line before the answer is ready)."""
        question = question.strip()
        self._touch()
        # An email waiting for a yes: settled by code before anything else (no model call).
        pending = await self.company.resolve_pending(question)
        if pending is not None:
            answer = self._answer(pending, "action")
            self._finish(question, answer)
            return answer
        decision = await self.router.route(question, [t.question for t in self.history])
        log.info("Route: %s (%s)", decision.route, decision.reason)
        if on_route:
            await on_route(decision)
        try:
            answer = await self._dispatch(question, decision)
        except ModelError as exc:
            log.error("Model error: %s", exc)
            answer = Answer(exc.spoken, decision.route, error=True)
        answer.route_reason = decision.reason
        self._finish(question, answer)
        return answer

    async def _dispatch(self, question: str, decision: RouteDecision) -> Answer:
        route = decision.route
        if route == "action":
            reply = await self.company.act(question, [t.question for t in self.history], self.session_id)
            return self._answer(reply, route)
        if route == "company":
            reply = await self.company.answer(question, self._history_messages("company"), self.session_id,
                                              simple=decision.complexity == "simple")
            return self._answer(reply, route)
        if route == "mixed":
            return await self._both(question)
        reply = await self.web.answer(question, self._history_messages("general"), live=route == "live")
        return self._answer(reply, route)

    async def _both(self, question: str) -> Answer:
        """Company data plus outside information: both agents at once, each on its own part."""
        company, web = await asyncio.gather(
            self.company.answer(question, self._history_messages("company"), self.session_id, part_only=True),
            self.web.answer(question, self._history_messages("general"), live=True, part_only=True))
        r = self.settings.routing
        model = r.get("combine_model", r["general_model"])
        msg = await self.models.create(
            model=model, system=combine_system(self.name, self.boss),
            messages=[{"role": "user", "content":
                       f"Question: {question}\n\nCompany-data agent found: {company.text}\n\n"
                       f"Web agent found: {web.text}"}],
            effort=r.get("effort", {}).get("general"), max_tokens=r.get("max_answer_tokens", 4000))
        text = text_of(msg) or f"{company.text} {web.text}"
        return Answer(text, "mixed", model, company.sources + [s for s in web.sources if s not in company.sources],
                      company.as_of, company.tools_used + web.tools_used,
                      error=company.error and web.error, agents=["company", "web"])

    def _answer(self, reply: AgentReply, route: str) -> Answer:
        return Answer(reply.text, route, reply.model, reply.sources, reply.as_of, reply.tools_used,
                      reply.error, files=reply.files, agents=[reply.agent])

    def _finish(self, question: str, answer: Answer) -> None:
        if not answer.error:
            self.history.append(Turn(question, answer.text, answer.route, list(answer.tools_used),
                                     answer.as_of, list(answer.sources)))
        self._log(question, answer)

    # ------------------------------------------------------------------ #

    def _log(self, question: str, answer: Answer) -> None:
        path = self.settings.log_dir / "conversations.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "session": self.session_id, "question": question, **asdict(answer)}
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")


Coordinator = Orchestrator
