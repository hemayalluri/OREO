"""
Talk to Oreo in the terminal (step 2: text only).

    python -m backend.chat            # from the oreo/ folder
    python -m backend.chat --debug    # also show routing and tool logs

Commands: /reset clears the conversation, /quit (or Ctrl+D) exits.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import has_anthropic_credentials, load_settings
from .mcp_gateway import McpGateway
from .models import ModelAdapter
from .orchestrator import Orchestrator

DIM, RESET, BOLD = "\033[2m", "\033[0m", "\033[1m"


async def main(debug: bool) -> int:
    logging.basicConfig(level=logging.INFO if debug else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if not has_anthropic_credentials():
        print("No ANTHROPIC_API_KEY found. Copy .env.example to .env and add your key.", file=sys.stderr)
        return 1

    name = settings.oreo.get("name", "Oreo")
    async with McpGateway(settings.mcp_servers, settings.log_dir / "audit.jsonl") as gateway:
        if not gateway.available:
            print("Warning: no company tools available (is data/sales.db created?).", file=sys.stderr)
        oreo = Orchestrator(settings, ModelAdapter(), gateway)
        hello = (settings.oreo.get("greetings") or ["Hey boss! What's up?"])[0]
        print(f"{BOLD}{name}{RESET}: {hello}")
        while True:
            try:
                question = await asyncio.to_thread(input, "\nYou: ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not question.strip():
                continue
            if question.strip() in ("/quit", "/exit"):
                break
            if question.strip() == "/reset":
                oreo.reset()
                print(f"{DIM}(conversation cleared){RESET}")
                continue
            answer = await oreo.ask(question)
            print(f"{BOLD}{name}{RESET}: {answer.text}")
            details = [f"route: {answer.route}", f"model: {answer.model or '-'}"]
            if answer.tools_used:
                details.append("tools: " + ", ".join(answer.tools_used))
            if answer.as_of:
                details.append(f"data as of {answer.as_of}")
            print(f"{DIM}({'; '.join(details)}){RESET}")
    print(f"{name}: Bye!")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chat with Oreo in the terminal.")
    parser.add_argument("--debug", action="store_true", help="show routing and tool logs")
    sys.exit(asyncio.run(main(parser.parse_args().debug)))
