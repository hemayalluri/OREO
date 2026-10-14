"""
Model adapter: the one place Oreo talks to an LLM.

Everything else calls `ModelAdapter.create(...)`, so other providers (a local
Ollama model for the general path, for example) can be added here later
without touching the router or orchestrator.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic

log = logging.getLogger("oreo.models")

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
# Haiku has no server-side fallback, so a refusal there is reported as a polite failure.
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ModelError(Exception):
    """A model call failed; `spoken` is a sentence Oreo can say out loud."""

    def __init__(self, spoken: str, detail: str = ""):
        super().__init__(detail or spoken)
        self.spoken = spoken


class ModelAdapter:
    def __init__(self, client: Any | None = None):
        # The default client reads ANTHROPIC_API_KEY (or an `ant auth login` profile).
        self.client = client or anthropic.AsyncAnthropic()

    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        effort: str | None = None,
        max_tokens: int = 16000,
        output_schema: dict[str, Any] | None = None,
        cache: bool = True,
    ) -> Any:
        """One Messages API call. Returns the SDK message object."""
        params: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            params["system"] = system
        if tools:
            params["tools"] = tools
        output_config: dict[str, Any] = {}
        if effort:
            output_config["effort"] = effort
        if output_schema:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        if output_config:
            params["output_config"] = output_config
        if cache:
            # Caches the stable prefix (tools + system prompt + earlier turns).
            params["cache_control"] = {"type": "ephemeral"}
        betas: list[str] = []
        if model in FALLBACK_MODELS:
            params["fallbacks"] = "default"
            betas.append(FALLBACK_BETA)

        try:
            if betas:
                return await self.client.beta.messages.create(betas=betas, **params)
            return await self.client.messages.create(**params)
        except anthropic.AuthenticationError as exc:
            raise ModelError("My API key isn't working. Please check the settings.", str(exc)) from exc
        except anthropic.RateLimitError as exc:
            raise ModelError("I'm a bit overloaded right now. Please ask me again in a minute.", str(exc)) from exc
        except anthropic.BadRequestError as exc:
            log.error("Bad request to %s: %s", model, exc)
            raise ModelError("Something went wrong with that request. Please try asking another way.", str(exc)) from exc
        except anthropic.APIStatusError as exc:
            raise ModelError("My brain service is having trouble right now. Please try again shortly.", str(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise ModelError("I can't reach the internet right now.", str(exc)) from exc


def text_of(message: Any) -> str:
    """Join the text blocks of a response."""
    return "".join(b.text for b in message.content if getattr(b, "type", None) == "text").strip()
