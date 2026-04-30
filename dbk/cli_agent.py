"""DBK Agent CLI entry point.

Usage:
    dbk agent --session SESSION_ID [--model MODEL] [--provider openai|anthropic|mock]
    dbk agent --interactive [--session SESSION_ID] [--model MODEL]
    dbk agent --info
    dbk agent workflow-advance --session SESSION_ID [--stage STAGE]
    dbk agent session-list
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dbk.agent.core import Agent
from dbk.agent.session_store import SessionStore
from dbk.agent.state import WorkflowStage
from dbk.agent.workflow import WorkflowStateMachine
from dbk.providers import auto_select_provider, get_provider


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbk agent",
        description="DBK AI Agent - LLM-powered database kernel observability assistant",
    )
    parser.add_argument("--session", help="Session ID to resume (auto-generates if omitted)")
    parser.add_argument("--model", help="Model name (overrides DBK_MODEL)")
    parser.add_argument(
        "--provider",
        choices=["openai", "anthropic", "mock"],
        help="Provider (overrides DBK_PROVIDER / auto-detect)",
    )
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive REPL mode")
    parser.add_argument("--info", action="store_true", help="Show agent configuration")
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming responses",
    )

    sub = parser.add_subparsers(dest="subcmd", required=False)

    # workflow-advance subcommand.
    p_workflow = sub.add_parser("workflow-advance", help="Advance workflow stage")
    p_workflow.add_argument("--session", required=True, help="Session ID")
    p_workflow.add_argument(
        "--stage",
        choices=[s.value for s in WorkflowStage],
        help="Target stage (defaults to next stage)",
    )

    # session-list subcommand.
    sub.add_parser("session-list", help="List persisted sessions")

    # session-clear subcommand.
    p_session_clear = sub.add_parser("session-clear", help="Clear/reset session data")
    p_session_clear.add_argument("--session", help="Session ID to delete (deletes all if omitted)")
    p_session_clear.add_argument("--all", action="store_true", help="Delete all sessions")

    # tools-list subcommand.
    sub.add_parser("tools-list", help="List registered agent tools")

    return parser


def cmd_agent_info(agent: Agent) -> int:
    info = agent.info()
    print(json.dumps(info, indent=2, ensure_ascii=True))
    return 0


def cmd_session_list() -> int:
    store = SessionStore()
    sessions = store.list_sessions()
    print(json.dumps({"sessions": sessions, "count": len(sessions)}, indent=2, ensure_ascii=True))
    return 0


def cmd_session_clear(args: argparse.Namespace) -> int:
    store = SessionStore()
    if args.session:
        deleted = store.delete(args.session)
        if deleted:
            print(json.dumps({"deleted": args.session, "count": 1}))
        else:
            print(json.dumps({"deleted": None, "error": f"Session not found: {args.session}"}))
            return 2
    elif args.all:
        sessions = store.list_sessions(limit=10000)
        deleted = 0
        for s in sessions:
            if store.delete(s["session_id"]):
                deleted += 1
        print(json.dumps({"deleted_all": True, "count": deleted}))
    else:
        print("Specify --session ID or --all to clear sessions.", file=sys.stderr)
        return 2
    return 0


def cmd_tools_list() -> int:
    from dbk.agent.tools import ToolRegistry
    registry = ToolRegistry()
    schemas = registry.tool_schemas()
    print(json.dumps({"tools": schemas, "count": len(schemas)}, indent=2, ensure_ascii=True))
    return 0


def cmd_workflow_advance(args: argparse.Namespace) -> int:
    store = SessionStore()
    state = store.load(args.session)
    if state is None:
        print(f"Session not found: {args.session}", file=sys.stderr)
        return 2

    wfm = WorkflowStateMachine(initial=state.workflow_stage)
    if args.stage:
        target = WorkflowStage(args.stage)
        try:
            wfm.goto(target)
        except ValueError as exc:
            print(f"Invalid transition: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            wfm.next()
        except ValueError as exc:
            print(f"Cannot advance: {exc}", file=sys.stderr)
            return 2

    new_state = state.advance_workflow(wfm.current)
    store.save(new_state)
    print(
        json.dumps(
            {
                "session_id": new_state.session_id,
                "workflow_stage": new_state.workflow_stage.value,
                "description": wfm.description,
                "progress": wfm.progress_summary(),
            },
            indent=2,
            ensure_ascii=True,
        )
    )
    return 0


def cmd_interactive(agent: Agent, session_id: str | None) -> int:
    print("DBK Agent REPL (type 'exit' or 'quit' to end session)")
    print(f"Provider: {agent.provider.name} | Model: {getattr(agent.provider, '_default_model', '?')} | Mock: {agent.provider.is_mock}")
    print(f"Type 'info' for agent info, 'workflow' for workflow status, 'exit' to quit.\n")

    # Get or create session.
    if session_id:
        state = agent.get_session(session_id)
        if state:
            print(f"Resumed session: {session_id}")
            print(f"Workflow stage: {state.workflow_stage.value}")
        else:
            print(f"Session not found: {session_id}, creating new session.")
            session_id = None

    if not session_id:
        state = agent.create_session()
        session_id = state.session_id
        print(f"New session: {session_id}")

    print()

    try:
        while True:
            try:
                user_input = input("dbk> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit", "q"):
                print("Goodbye!")
                break

            if user_input.lower() in ("info", "status"):
                print(json.dumps(agent.info(), indent=2))
                continue

            if user_input.lower() == "workflow":
                state = agent.get_session(session_id)
                if state:
                    wfm = WorkflowStateMachine(initial=state.workflow_stage)
                    print(json.dumps(wfm.progress_summary(), indent=2))
                continue

            if user_input.startswith("/"):
                # Shell escape.
                os.system(user_input[1:])
                continue

            # Process message.
            print()
            result = agent.process_message(user_input, session_id=session_id)
            if "error" in result and result.get("error"):
                print(f"[Error] {result['error']}")
            print(result["content"])
            session_id = result["session_id"]
            print()
    finally:
        # Save final state.
        state = agent.get_session(session_id)
        if state:
            agent._session_store.save(state)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.subcmd and not (args.interactive or args.info):
        parser.print_help()
        return 0

    # Build provider.
    provider = None
    if args.provider:
        os.environ["DBK_PROVIDER"] = args.provider
    if args.model:
        os.environ["DBK_MODEL"] = args.model

    # Build agent with auto-selected provider.
    provider = get_provider()
    agent = Agent(provider=provider)

    # Handle subcommands.
    if args.subcmd == "session-list":
        return cmd_session_list()

    if args.subcmd == "session-clear":
        return cmd_session_clear(args)

    if args.subcmd == "tools-list":
        return cmd_tools_list()

    if args.subcmd == "workflow-advance":
        return cmd_workflow_advance(args)

    if args.info:
        return cmd_agent_info(agent)

    if args.interactive:
        return cmd_interactive(agent, session_id=args.session)

    # Default: single-shot mode.
    if not sys.stdin.isatty():
        # Pipe mode: read from stdin.
        message = sys.stdin.read().strip()
        if message:
            result = agent.process_message(message, session_id=args.session)
            print(result["content"])
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
