"""Enhanced REPL for the DBK Agent.

Features:
- Syntax highlighting (ANSI colors)
- Command history (readline-style with persistent history)
- Session management commands
- Tab completion for known commands
- Workflow stage display
- Memory integration
- Streaming token display
- Inline help system
"""
from __future__ import annotations

import atexit
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

from dbk.agent.core import Agent
from dbk.agent.memory import AgentMemory
from dbk.agent.workflow import WorkflowStateMachine

# Initialize plugin system (discovers and registers plugins lazily).
try:
    from dbk.plugins import load_plugins
    load_plugins()
except Exception:  # noqa: BLE001
    pass

# ANSI color codes.
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"


# ----------------------------------------------------------------------
# History management.
# ----------------------------------------------------------------------


class History:
    """Persistent REPL command history."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lines: list[str] = []
        self._max_size = 1000
        self._load()

    def _load(self) -> None:
        if self._path and self._path.exists():
            try:
                self._lines = self._path.read_text().splitlines()
            except OSError:
                self._lines = []

    def append(self, line: str) -> None:
        if line and (not self._lines or self._lines[-1] != line):
            self._lines.append(line)
            if len(self._lines) > self._max_size:
                self._lines = self._lines[-self._max_size:]

    def get_lines(self) -> list[str]:
        return list(self._lines)

    def search(self, prefix: str) -> list[str]:
        return [line for line in reversed(self._lines) if line.startswith(prefix)]

    def save(self) -> None:
        if self._path:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text("\n".join(self._lines[-self._max_size:]))
            except OSError:
                pass


# ----------------------------------------------------------------------
# Output helpers.
# ----------------------------------------------------------------------


def _print_banner(agent: Agent) -> None:
    print(
        f"\n{BOLD}{CYAN}  DBK Agent REPL  {RESET}  -  Database Kernel Observability Assistant\n"
        f"{DIM}{'=' * 60}{RESET}\n"
    )
    info = agent.info()
    print(
        f"  Provider: {GREEN}{info['provider']}{RESET}  "
        f"Model: {info.get('model', '?')}  "
        f"Tools: {info['tool_count']}\n"
        f"  Commands: {DIM}help{RESET} | {DIM}info{RESET} | {DIM}workflow{RESET} | "
        f"{DIM}memory{RESET} | {DIM}session{RESET} | {DIM}clear{RESET} | {DIM}exit{RESET}\n"
        f"{DIM}{'─' * 60}{RESET}\n"
    )


def _print_workflow(state: Any) -> None:
    """Pretty-print the current workflow state."""
    wfm = WorkflowStateMachine(initial=state.workflow_stage)
    summary = wfm.progress_summary()
    stage_colors: dict[str, str] = {
        "requirements": CYAN,
        "design": BLUE,
        "implement": GREEN,
        "test": YELLOW,
        "runtime": MAGENTA,
        "doc": "",
        "ops": CYAN,
        "done": GREEN,
    }
    col = stage_colors.get(summary["current"], "")
    bar = _build_progress_bar(summary["stage_number"], summary["total_stages"], col)
    print(f"  {BOLD}Workflow:{RESET} {col}{summary['current']}{RESET}  {bar}")
    print(f"  Stage {summary['stage_number']}/{summary['total_stages']}")


def _build_progress_bar(current: int, total: int, color: str) -> str:
    width = 30
    filled = int(width * current / total)
    return f"[{color}{'=' * filled}{DIM}{'-' * (width - filled)}{RESET}]"


def _print_response(content: str, color: str = "") -> None:
    """Print wrapped response text with optional color."""
    width = 80
    lines = content.split("\n")
    for line in lines:
        while len(line) > width:
            print(color + line[:width] + RESET)
            line = line[width:]
        print(color + line + RESET)


def _print_error(message: str) -> None:
    print(f"{RED}Error:{RESET} {message}", file=sys.stderr)


# ----------------------------------------------------------------------
# Tab completion.
# ----------------------------------------------------------------------


_builtin_commands = [
    "help", "info", "workflow", "workflow advance", "workflow show",
    "session", "session list", "session new", "session load",
    "memory", "memory facts", "memory episodes", "memory prune",
    "clear", "exit", "quit", "q",
]


def _complete(text: str, state: int) -> str | None:
    """Basic tab completion callback (compatible with readline)."""
    try:
        import readline
    except ImportError:
        return None
    options = [cmd for cmd in _builtin_commands if cmd.startswith(text)]
    if state < len(options):
        return options[state]
    return None


# ----------------------------------------------------------------------
# REPL configuration and command parsing.
# ----------------------------------------------------------------------


@dataclass
class REPLConfig:
    """Configuration for the enhanced REPL."""
    enable_streaming: bool = True
    enable_colors: bool = True
    enable_history: bool = True
    history_path: Path | None = None
    max_history: int = 1000
    show_workflow: bool = True
    show_memory_context: bool = True


@dataclass
class REPLCommand:
    """A parsed REPL command."""
    name: str
    args: list[str]
    raw: str


def _parse_input(line: str) -> REPLCommand:
    """Parse a REPL input line into command + args."""
    parts = line.strip().split()
    if not parts:
        return REPLCommand(name="", args=[], raw=line)
    name = parts[0].lower()
    args = parts[1:]
    return REPLCommand(name=name, args=args, raw=line)


# ----------------------------------------------------------------------
# REPL main class.
# ----------------------------------------------------------------------


class REPL:
    """Enhanced REPL for DBK Agent with history, streaming, and memory."""

    def __init__(
        self,
        agent: Agent,
        memory: AgentMemory | None = None,
        config: REPLConfig | None = None,
    ) -> None:
        self._agent = agent
        self._memory = memory or AgentMemory()
        self._config = config or REPLConfig()
        self._session_id: str | None = None
        self._history: History | None = None
        if self._config.enable_history:
            self._history = History(self._config.history_path)
            if self._config.history_path:
                atexit.register(self._save_history)
        self._setup_readline()

    def _setup_readline(self) -> None:
        try:
            import readline
            readline.set_completer(_complete)
            readline.parse_and_bind("tab: complete")
            readline.set_history_length(self._config.max_history)
        except ImportError:
            pass

    def _save_history(self) -> None:
        if self._history:
            self._history.save()

    def run(self, initial_session_id: str | None = None) -> int:
        """Run the REPL loop. Returns exit code."""
        _print_banner(self._agent)

        # Load or create session.
        if initial_session_id:
            state = self._agent.get_session(initial_session_id)
            if state:
                self._session_id = initial_session_id
                print(f"  Resumed session: {GREEN}{self._session_id}{RESET}\n")
            else:
                print(f"  Session not found: {initial_session_id}, creating new.\n")

        if not self._session_id:
            state = self._agent.create_session()
            self._session_id = state.session_id
            print(f"  New session: {GREEN}{self._session_id}{RESET}\n")

        self._print_workflow_if_enabled()

        try:
            while True:
                try:
                    line = input(f"{MAGENTA}dbk>{RESET} ").rstrip("\n")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break

                if not line.strip():
                    continue

                if self._history:
                    self._history.append(line.strip())

                cmd = _parse_input(line)

                if cmd.name in ("exit", "quit", "q"):
                    print(f"{DIM}Goodbye!{RESET}")
                    break

                handled = self._handle_meta_command(cmd)
                if handled:
                    continue

                # Process as agent message.
                print(f"{DIM}...{RESET} ", end="", flush=True)
                try:
                    if self._config.enable_streaming and self._agent.provider.supports_streaming:
                        content = ""
                        for token in self._agent.process_stream(cmd.raw, session_id=self._session_id):
                            print(token, end="", flush=True)
                            content += token
                        print()
                    else:
                        result = self._agent.process_message(cmd.raw, session_id=self._session_id)
                        content = result.get("content", "")
                        if self._config.enable_colors:
                            _print_response(content, GREEN)
                        else:
                            print(content)

                    self._session_id = result.get("session_id", self._session_id or "")
                    self._update_memory_after_turn({
                        "session_id": self._session_id or "",
                        "content": content,
                        "turn_count": result.get("turn_count", 0),
                    })

                except Exception as exc:  # noqa: BLE001
                    _print_error(str(exc))

                print()

        finally:
            self._save_session()

        return 0

    def _print_workflow_if_enabled(self) -> None:
        if not self._config.show_workflow:
            return
        state = self._agent.get_session(self._session_id or "")
        if state:
            _print_workflow(state)
            print()

    def _handle_meta_command(self, cmd: REPLCommand) -> bool:
        """Handle non-chat meta-commands. Returns True if handled."""
        name, args = cmd.name, cmd.args

        if name == "help":
            self._cmd_help()
            return True

        if name == "info":
            self._cmd_info()
            return True

        if name in ("workflow", "wf"):
            self._cmd_workflow(args)
            return True

        if name == "session":
            self._cmd_session(args)
            return True

        if name in ("memory", "mem"):
            self._cmd_memory(args)
            return True

        if name == "clear":
            try:
                os.system("clear" if os.name != "nt" else "cls")
            except OSError:
                pass
            return True

        if name == "?":
            self._cmd_help()
            return True

        return False

    def _cmd_help(self) -> None:
        print(f"\n{BOLD}DBK Agent REPL Commands{RESET}\n")
        print(f"  {GREEN}help{RESET}                          Show this help")
        print(f"  {GREEN}info{RESET}                           Show agent info")
        print(f"  {GREEN}workflow{RESET} [stage]                Show or advance workflow")
        print(f"  {GREEN}session{RESET} [list|new|load ID]      Manage sessions")
        print(f"  {GREEN}memory{RESET} [facts|episodes|prune]  Memory management")
        print(f"  {GREEN}clear{RESET}                           Clear screen")
        print(f"  {GREEN}exit/quit{RESET}                       End REPL session\n")
        print(f"  {DIM}Anything else is sent to the agent as a chat message.{RESET}\n")

    def _cmd_info(self) -> None:
        import json
        info = self._agent.info()
        print(json.dumps(info, indent=2))

    def _cmd_workflow(self, args: list[str]) -> None:
        state = self._agent.get_session(self._session_id or "")
        if not state:
            _print_error("No active session")
            return

        if not args:
            _print_workflow(state)
            return

        from dbk.agent.state import WorkflowStage
        try:
            target = WorkflowStage(args[0])
        except ValueError:
            _print_error(f"Unknown stage: {args[0]}. Valid: {[s.value for s in WorkflowStage]}")
            return

        try:
            new_state = self._agent.advance_workflow(self._session_id or "", target)
            print(f"  Workflow advanced to: {GREEN}{new_state.workflow_stage.value}{RESET}")
            _print_workflow(new_state)
        except ValueError as exc:
            _print_error(str(exc))

    def _cmd_session(self, args: list[str]) -> None:
        if not args:
            state = self._agent.get_session(self._session_id or "")
            if state:
                import json
                print(json.dumps(state.to_dict(), indent=2, default=str))
            return

        subcmd = args[0].lower()
        if subcmd == "list":
            sessions = self._agent.list_sessions()
            print(f"  {len(sessions)} session(s):")
            for s in sessions:
                sid = s.get("session_id", "?")
                stage = s.get("workflow_stage", "?")
                turns = s.get("turn_count", 0)
                print(f"    {GREEN}{sid}{RESET}  stage={stage}  turns={turns}")
            return

        if subcmd == "new":
            state = self._agent.create_session()
            self._session_id = state.session_id
            print(f"  New session: {GREEN}{self._session_id}{RESET}")
            return

        if subcmd == "load" and len(args) > 1:
            sid = args[1]
            state = self._agent.get_session(sid)
            if state:
                self._session_id = sid
                print(f"  Loaded session: {GREEN}{self._session_id}{RESET}")
                _print_workflow(state)
            else:
                _print_error(f"Session not found: {sid}")
            return

        _print_error(f"Unknown session command: {subcmd}. Use: list | new | load <id>")

    def _cmd_memory(self, args: list[str]) -> None:
        if not args:
            facts = self._memory.recall(session_id=self._session_id, limit=5)
            episodes = self._memory.recall_episodes(
                session_id=self._session_id or "", limit=5,
            )
            print(f"  Facts: {len(facts)}  Episodes: {len(episodes)}")
            for f in facts[:5]:
                print(f"    {f.key}: {f.value[:50]}")
            return

        subcmd = args[0].lower()
        if subcmd == "facts":
            facts = self._memory.recall(session_id=self._session_id, limit=20)
            print(f"  {len(facts)} fact(s):")
            for f in facts:
                importance_bar = "*" * f.importance
                print(f"    [{importance_bar}] {f.key}: {f.value[:60]}")
            return

        if subcmd == "episodes":
            episodes = self._memory.recall_episodes(
                session_id=self._session_id or "", limit=20,
            )
            print(f"  {len(episodes)} episode(s):")
            for ep in episodes[-10:]:
                role = ep.get("role", "?")[0].upper()
                content = ep.get("content", "")[:60]
                print(f"    [{role}] {content}")
            return

        if subcmd == "prune":
            if not self._session_id:
                _print_error("No active session")
                return
            deleted = self._memory.prune(self._session_id, retain_turns=10)
            print(f"  Pruned {deleted} episode(s).")
            return

        _print_error(f"Unknown memory command: {subcmd}. Use: facts | episodes | prune")

    def _update_memory_after_turn(self, result: dict[str, Any]) -> None:
        """Extract and store important facts from the agent response."""
        if not self._session_id:
            return
        sid = self._session_id
        content = result.get("content", "")
        tool_results = result.get("tool_results", [])

        import re
        for match in re.finditer(
            r"(?:instance|pg-main|pg-backup|pg-replica)[- ]?([a-zA-Z0-9_-]+)",
            content, re.I,
        ):
            self._memory.remember(
                session_id=sid,
                key="instance_mentioned",
                value=match.group(0),
                importance=4,
                tags=["observation"],
            )

        for tr in tool_results:
            tool_name = tr.get("tool", "?")
            if tr.get("ok"):
                result_data = tr.get("result", {})
                if isinstance(result_data, dict):
                    for k, v in list(result_data.items())[:3]:
                        self._memory.remember(
                            session_id=sid,
                            key=f"tool_{tool_name}_{k}",
                            value=str(v)[:200],
                            importance=5,
                            tags=["tool_result"],
                        )

        turn_count = result.get("turn_count", 0)
        if turn_count > 0 and turn_count % 20 == 0:
            self._memory.prune(sid, retain_turns=15)

    def _save_session(self) -> None:
        if self._session_id:
            try:
                state = self._agent.get_session(self._session_id)
                if state:
                    self._agent._session_store.save(state)
            except Exception:  # noqa: BLE001
                pass

    @property
    def session_id(self) -> str | None:
        return self._session_id


# ----------------------------------------------------------------------
# Convenience runner.
# ----------------------------------------------------------------------


def run_repl(
    session_id: str | None = None,
    enable_streaming: bool = True,
    enable_colors: bool = True,
    history_path: Path | None = None,
) -> int:
    """Run the enhanced REPL with the default agent."""
    from dbk.config import dbk_root

    if history_path is None:
        history_path = dbk_root() / "repl_history.txt"

    agent = Agent()
    memory = AgentMemory()

    config = REPLConfig(
        enable_streaming=enable_streaming,
        enable_colors=enable_colors,
        enable_history=True,
        history_path=history_path,
        show_workflow=True,
        show_memory_context=True,
    )

    repl = REPL(agent=agent, memory=memory, config=config)
    return repl.run(initial_session_id=session_id)
