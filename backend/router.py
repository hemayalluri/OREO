"""
Router: decides which path a question takes.

  company  sales/business questions -> Claude with the read-only MCP tools
  general  everything else -> general model with no tools and no company data
  mixed    needs company data plus outside knowledge -> company path + web search
  action   create a report or send an email (handled in step 3)

When the router is unsure, or fails, the question takes the company (secure) path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .models import ModelAdapter, ModelError, text_of

log = logging.getLogger("oreo.router")

ROUTES = ("company", "general", "mixed", "action")

ROUTER_SYSTEM = """You route questions for Oreo, a desk assistant for a sales team.
Classify the user's latest message:
- company: about the company's own sales, revenue, orders, products, customers, regions, channels, targets, or data freshness. Follow-ups to a company question ("and last year?", "what about Europe?") are company too.
- general: general knowledge, small talk, jokes, weather, definitions, maths, anything that needs no company data.
- mixed: needs company data AND outside information (e.g. "how do our chair sales compare with the industry trend?").
- action: asks to create a report or document, or to send or email something.
Set complexity to "simple" only for a single-number company lookup (one period, one metric); otherwise "complex".
Set confident to false if the message could reasonably fit more than one route."""

ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": list(ROUTES)},
        "complexity": {"type": "string", "enum": ["simple", "complex"]},
        "confident": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["route", "complexity", "confident", "reason"],
    "additionalProperties": False,
}


@dataclass
class RouteDecision:
    route: str
    complexity: str = "complex"
    reason: str = ""
    fallback: bool = False   # True when the secure default was applied


class Router:
    def __init__(self, models: ModelAdapter, model: str, effort: str = "low", default_when_unsure: str = "company"):
        self.models = models
        self.model = model
        self.effort = effort
        self.default = default_when_unsure

    async def route(self, question: str, recent_questions: list[str] | None = None) -> RouteDecision:
        context = ""
        if recent_questions:
            context = "Earlier questions in this conversation:\n" + "\n".join(f"- {q}" for q in recent_questions[-3:]) + "\n\n"
        try:
            msg = await self.models.create(
                model=self.model,
                system=ROUTER_SYSTEM,
                messages=[{"role": "user", "content": f"{context}Latest message: {question}"}],
                effort=self.effort,
                max_tokens=2000,
                output_schema=ROUTER_SCHEMA,
                cache=False,
            )
            if getattr(msg, "stop_reason", None) == "refusal":
                raise ValueError("router refused")
            data: dict[str, Any] = json.loads(text_of(msg))
        except (ModelError, ValueError, KeyError) as exc:
            log.warning("Router failed (%s); using the %s path", exc, self.default)
            return RouteDecision(self.default, reason=f"router unavailable: {exc}", fallback=True)

        route = data.get("route")
        if route not in ROUTES:
            return RouteDecision(self.default, reason=f"unknown route {route!r}", fallback=True)
        if route == "general" and not data.get("confident", False):
            # Unsure whether company data is involved: take the secure path.
            return RouteDecision(self.default, data.get("complexity", "complex"),
                                 f"unsure ({data.get('reason', '')})", fallback=True)
        return RouteDecision(route, data.get("complexity", "complex"), data.get("reason", ""))
