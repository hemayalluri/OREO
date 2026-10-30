"""
Oreo's two sub-agents. The coordinator (backend/orchestrator.py) decides which one handles a request.

  CompanyDataAgent  the internal sales database (read-only MCP tools), plus reports and email
  WebAgent          the live internet (web search) and general knowledge; never sees company data
"""

from .base import AgentReply
from .company import CompanyDataAgent
from .web import WebAgent

__all__ = ["AgentReply", "CompanyDataAgent", "WebAgent"]
