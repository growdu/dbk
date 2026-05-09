"""DBK Agent CLI command — delegates to cli_agent module."""
from __future__ import annotations

import argparse
from typing import Any, Callable

# Lazy import to avoid loading LLM packages until the agent command is used.
_agent_cli_main: Callable[..., Any] | None = None


def _get_agent_main() -> Callable[..., Any]:
    global _agent_cli_main
    if _agent_cli_main is None:
        from dbk import cli_agent
        _agent_cli_main = cli_agent.main
    return _agent_cli_main


def _do_agent(args: argparse.Namespace) -> int:
    """Forward to cli_agent.main with the remaining args."""
    from dbk.agent.state import WorkflowStage

    extra = []
    if getattr(args, "session", None):
        extra += ["--session", args.session]
    if getattr(args, "model", None):
        extra += ["--model", args.model]
    if getattr(args, "provider", None):
        extra += ["--provider", args.provider]
    if getattr(args, "interactive", False):
        extra += ["--interactive"]
    if getattr(args, "info", False):
        extra += ["--info"]
    if getattr(args, "no_stream", False):
        extra += ["--no-stream"]
    if getattr(args, "subcmd", None):
        extra = [args.subcmd] + extra

    return _get_agent_main()(extra)


def _do_sessions(args: argparse.Namespace) -> int:
    """Handle dbk agent sessions subcommand.

    Delegates to cli_agent 'session-clear' (which has --session/--all args)
    rather than 'session-list' (which has no extra args).
    """
    if args.clear:
        # Use session-clear for --clear operations
        extra = ["session-clear"]
        if args.session:
            extra += ["--session", args.session]
        if args.all:
            extra.append("--all")
    else:
        # Use session-list for read-only list
        extra = ["session-list"]
    return _get_agent_main()(extra)


def _do_workflow(args: argparse.Namespace) -> int:
    """Handle dbk agent workflow subcommand."""
    extra = ["workflow-advance", "--session", args.session]
    if args.stage:
        extra += ["--stage", args.stage]
    return _get_agent_main()(extra)


class AgentCommand:
    """Register the 'agent' top-level subcommand."""

    def configure(self, subparsers) -> None:
        from dbk.agent.state import WorkflowStage
        p_agent = subparsers.add_parser(
            "agent",
            help="DBK AI Agent — LLM-powered observability assistant",
        )
        agent_sub = p_agent.add_subparsers(dest="agent_cmd", required=False)

        # agent interactive
        p_interactive = agent_sub.add_parser(
            "interactive",
            help="Start interactive REPL mode",
        )
        p_interactive.add_argument("--session")
        p_interactive.add_argument("--model")
        p_interactive.add_argument(
            "--provider",
            choices=["openai", "anthropic", "mock"],
        )
        p_interactive.add_argument("--no-stream", action="store_true")
        p_interactive.set_defaults(
            func=lambda a: _do_agent(a) or _get_agent_main()(
                ["--interactive", "--session", a.session or "", "--model", a.model or "",
                 "--provider", a.provider or "",
                 "--no-stream" if a.no_stream else ""]
            ),
        )

        # agent info
        p_info = agent_sub.add_parser("info", help="Show agent configuration")
        p_info.set_defaults(func=lambda _a: _get_agent_main()(["--info"]))

        # agent sessions
        p_sessions = agent_sub.add_parser(
            "sessions",
            help="List/clear agent sessions",
        )
        p_sessions.add_argument("--clear", action="store_true")
        p_sessions.add_argument("--session")
        p_sessions.add_argument("--all", action="store_true")
        p_sessions.set_defaults(func=_do_sessions)

        # session-list (alias for "sessions")
        p_session_list = agent_sub.add_parser(
            "session-list",
            help="Alias for 'sessions'",
        )
        p_session_list.add_argument("--clear", action="store_true")
        p_session_list.add_argument("--session")
        p_session_list.add_argument("--all", action="store_true")
        p_session_list.set_defaults(func=_do_sessions)

        # session-clear (alias)
        p_session_clear = agent_sub.add_parser("session-clear", help="Clear session(s)")
        p_session_clear.add_argument("--session")
        p_session_clear.add_argument("--all", action="store_true")
        p_session_clear.set_defaults(func=lambda a: _do_sessions(a))

        # agent tools
        p_tools = agent_sub.add_parser("tools", help="Show registered agent tools")
        p_tools.set_defaults(func=lambda _a: _get_agent_main()(["tools-list"]))

        # agent workflow
        p_workflow = agent_sub.add_parser("workflow", help="Manage workflow stage")
        p_workflow.add_argument("--session", required=True)
        p_workflow.add_argument("--stage", choices=[s.value for s in WorkflowStage])
        p_workflow.set_defaults(func=_do_workflow)

        # Default: fall back to interactive mode
        p_agent.set_defaults(func=_do_agent)
