"""Shared test fixtures: a small generated database and a scripted fake Claude client."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))

import create_mock_data  # noqa: E402
from backend.config import load_settings  # noqa: E402


@pytest.fixture(scope="session")
def sales_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("db") / "sales.db"
    create_mock_data.build(path, end=date(2026, 10, 14), days=400, seed=1)
    return path


@pytest.fixture
def settings(sales_db, tmp_path):
    s = load_settings(ROOT / "config.yaml")
    for server in s.raw["mcp"]["servers"]:
        server["env"] = {"OREO_SALES_DB": str(sales_db)}
    s.raw["logging"] = {"dir": str(tmp_path / "logs")}
    return s


# --------------------------------------------------------------------------- #
# Fake Anthropic client
# --------------------------------------------------------------------------- #

def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_use(name: str, args: dict, id: str = "toolu_1"):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=args)


def response(*blocks, stop_reason: str = "end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


class FakeClient:
    """Mimics AsyncAnthropic: `script(call_kwargs)` returns each response."""

    def __init__(self, script: Callable[[dict[str, Any]], Any]):
        self.script = script
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        # Snapshot the message list: the orchestrator keeps appending to it.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        result = self.script(kwargs)
        if isinstance(result, Exception):
            raise result
        return result


def is_router(kwargs: dict) -> bool:
    return "format" in kwargs.get("output_config", {})


def route_json(route: str, complexity: str = "complex", confident: bool = True) -> Any:
    import json
    return response(text_block(json.dumps(
        {"route": route, "complexity": complexity, "confident": confident, "reason": "test"})))
