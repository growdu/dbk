"""CLI entry point for the DBK Agent TUI."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dbk.agent.core import Agent
from dbk.providers.anthropic import AnthropicProvider
from dbk.providers.mock import MockProvider
from dbk.providers.openai import OpenAIProvider
from typing import Any


def _build_tui_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbk tui",
        description="Start the DBK Agent Textual TUI.",
    )
    parser.add_argument(
        "--session",
        dest="session_id",
        metavar="ID",
        help="Resume an existing session by ID",
    )
    parser.add_argument(
        "--provider",
        choices=["openai", "anthropic", "mock"],
        default="mock",
        help="LLM provider to use (default: mock)",
    )
    parser.add_argument(
        "--model",
        dest="model",
        metavar="NAME",
        help="Model name (provider-specific; e.g. gpt-4o for OpenAI, claude-3-5-sonnet for Anthropic)",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming token display",
    )
    return parser


def _create_agent(provider_name: str, model: str | None) -> Agent:
    """Create an Agent with the specified provider."""
    if provider_name == "openai":
        api_key = None
        try:
            from openai import OpenAI
            # Try loading from env automatically.
        except ImportError:
            print("Error: openai package not installed. Install with: pip install openai", file=sys.stderr)
            sys.exit(1)
        provider: Any = OpenAIProvider(api_key=api_key, model=model)
    elif provider_name == "anthropic":
        api_key = None
        try:
            from anthropic import Anthropic
        except ImportError:
            print("Error: anthropic package not installed. Install with: pip install anthropic", file=sys.stderr)
            sys.exit(1)
        provider = AnthropicProvider(api_key=api_key, model=model)
    else:
        provider = MockProvider()
    return Agent(provider=provider)


def main(argv: list[str] | None = None) -> int:
    parser = _build_tui_parser()
    args = parser.parse_args(argv)

    agent = _create_agent(args.provider, args.model)

    # Import TUI here (lazy) to avoid loading textual until needed.
    from dbk.tui import DBKTUI

    app = DBKTUI(agent=agent, initial_session_id=args.session_id)

    # Configure streaming preference.
    if args.no_stream:
        app._typewriter_delay = 0.0  # Will be set later via settings if needed.

    try:
        app.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
