"""Load config.yaml and .env into one settings object."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


@dataclass
class McpServerConfig:
    name: str
    command: str
    args: list[str]
    allowed_tools: list[str]
    read_only: bool = True
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Settings:
    raw: dict[str, Any]
    root: Path

    @property
    def oreo(self) -> dict[str, Any]:
        return self.raw["oreo"]

    @property
    def routing(self) -> dict[str, Any]:
        return self.raw["routing"]

    @property
    def log_dir(self) -> Path:
        return self.root / self.raw.get("logging", {}).get("dir", "logs")

    @property
    def mcp_servers(self) -> list[McpServerConfig]:
        servers = []
        for s in self.raw.get("mcp", {}).get("servers", []):
            if s.get("transport", "stdio") != "stdio":
                raise ValueError(f"MCP server {s['name']}: only stdio transport is supported for now")
            # Relative script paths are relative to the config file's folder.
            args = [str(self.root / a) if a.endswith(".py") and not Path(a).is_absolute() else a
                    for a in s.get("args", [])]
            servers.append(McpServerConfig(
                name=s["name"], command=s.get("command", "python"), args=args,
                allowed_tools=list(s.get("allowed_tools", [])), read_only=s.get("read_only", True),
                env={k: str(v) for k, v in (s.get("env") or {}).items()},
            ))
        return servers


def load_settings(path: Path | str | None = None) -> Settings:
    """Read config.yaml and load secrets from .env (next to it) into the environment."""
    path = Path(path) if path else DEFAULT_CONFIG
    load_dotenv(path.parent / ".env")
    with open(path) as f:
        raw = yaml.safe_load(f)
    for section in ("oreo", "routing"):
        if section not in raw:
            raise ValueError(f"{path} is missing the '{section}' section")
    return Settings(raw=raw, root=path.parent.resolve())


def has_anthropic_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
