"""
MCP gateway: the only door between Oreo's models and company data.

  * Connects to each configured MCP server (stdio) with its own MCP client.
  * Exposes only allowlisted tools; with read_only on, a tool must also declare
    itself read-only (MCP readOnlyHint) or it is refused.
  * Writes every tool call to logs/audit.jsonl (time, session, tool, arguments,
    outcome, duration).
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from .config import McpServerConfig

log = logging.getLogger("oreo.mcp_gateway")


@dataclass
class ToolResult:
    text: str                    # what goes back to the model
    is_error: bool
    data: dict[str, Any] | None  # structured result, if the tool returned one


class McpGateway:
    def __init__(self, servers: list[McpServerConfig], audit_path: Path):
        self.servers = servers
        self.audit_path = audit_path
        self._stack: AsyncExitStack | None = None
        self._clients: dict[str, Client] = {}
        self._tool_owner: dict[str, str] = {}      # tool name -> server name
        self._tool_defs: list[dict[str, Any]] = []  # Anthropic tool definitions

    async def __aenter__(self) -> "McpGateway":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        for server in self.servers:
            command = sys.executable if server.command in ("python", "python3") else server.command
            params = StdioServerParameters(command=command, args=server.args, env=server.env or None)
            try:
                client = await self._stack.enter_async_context(Client(params))
            except Exception as exc:  # a missing server shouldn't stop Oreo from answering general questions
                log.error("Could not start MCP server %s: %s", server.name, exc)
                continue
            self._clients[server.name] = client
            listed = await client.list_tools()
            for tool in sorted(listed.tools, key=lambda t: t.name):  # stable order keeps the prompt cache warm
                reason = self._refusal_reason(server, tool)
                if reason:
                    log.info("Not exposing %s.%s: %s", server.name, tool.name, reason)
                    continue
                if tool.name in self._tool_owner:
                    log.warning("Tool %s is offered by two servers; keeping %s", tool.name, self._tool_owner[tool.name])
                    continue
                self._tool_owner[tool.name] = server.name
                self._tool_defs.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.input_schema,
                })
        log.info("MCP gateway ready with tools: %s", ", ".join(self._tool_owner) or "none")

    async def close(self) -> None:
        if self._stack:
            await self._stack.aclose()
            self._stack = None

    @staticmethod
    def _refusal_reason(server: McpServerConfig, tool: Any) -> str | None:
        if tool.name not in server.allowed_tools:
            return "not in allowed_tools"
        if server.read_only:
            hint = getattr(getattr(tool, "annotations", None), "read_only_hint", None)
            if hint is not True:
                return "server is read-only and the tool does not declare readOnlyHint"
        return None

    @property
    def tools(self) -> list[dict[str, Any]]:
        return list(self._tool_defs)

    @property
    def available(self) -> bool:
        return bool(self._tool_defs)

    async def call(self, name: str, arguments: dict[str, Any], session_id: str = "") -> ToolResult:
        started = time.monotonic()
        server = self._tool_owner.get(name)
        if server is None:
            result = ToolResult(f"Tool {name!r} is not available.", True, None)
        else:
            try:
                raw = await self._clients[server].call_tool(name, arguments)
                text = " ".join(getattr(c, "text", "") for c in raw.content).strip()
                data = raw.structured_content
                if data is not None and not raw.is_error:
                    text = json.dumps(data)
                result = ToolResult(text or "(no output)", bool(raw.is_error), data)
            except Exception as exc:
                log.exception("MCP call %s failed", name)
                result = ToolResult(f"The {server} system could not be reached: {exc}", True, None)
        self._audit(session_id, server, name, arguments, result, time.monotonic() - started)
        return result

    def _audit(self, session_id: str, server: str | None, name: str, arguments: dict[str, Any],
               result: ToolResult, seconds: float) -> None:
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session": session_id,
            "server": server,
            "tool": name,
            "arguments": arguments,
            "ok": not result.is_error,
            "error": result.text[:300] if result.is_error else None,
            "result_chars": len(result.text),
            "ms": round(seconds * 1000),
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.audit_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
