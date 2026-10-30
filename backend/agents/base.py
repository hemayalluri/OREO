"""What an agent hands back to the coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentReply:
    text: str
    agent: str                                   # "company" or "web"
    model: str | None = None
    sources: list[str] = field(default_factory=list)
    as_of: str | None = None
    tools_used: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)   # report files made (names in reports/)
    error: bool = False
