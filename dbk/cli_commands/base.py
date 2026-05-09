"""Base command protocol and result types for DBK CLI.

Contract
--------
Every command handler receives argparse.Namespace and returns CommandResult.
The exit code is derived from result.code, so handlers never return raw ints.

Error codes (0-127 reserved by POSIX, we use 0-9 for DBK):
  0  SUCCESS
  1  WARNING      — completed but with degraded output or skipped items
  2  USAGE        — bad arguments / missing subcommand
  3  CONFIG_ERROR — configuration missing or invalid
  4  RUNTIME_ERROR — collector/daemon/startup failure
  5  DATA_ERROR    — query returned nothing / schema mismatch
  6  PERMISSION    — permission denied / auth failure
  9  INTERNAL      — unexpected exception in command handler
"""
from __future__ import annotations

import argparse
import json
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import IntEnum
from typing import Any, Callable, IO


class ExitCode(IntEnum):
    """DBK CLI exit codes.

    POSIX reserves 0 and 1-255; we use 0-9 for DBK-specific codes.
    Codes 10+ are free for application-specific use.
    """
    SUCCESS = 0
    WARNING = 1
    USAGE = 2          # bad arguments / missing subcommand / help shown
    CONFIG_ERROR = 3   # configuration missing or invalid
    RUNTIME_ERROR = 4  # collector/daemon/startup failure
    DATA_ERROR = 5     # query returned nothing / schema mismatch
    PERMISSION = 6     # permission denied / auth failure
    INTERNAL = 9       # unexpected exception in command handler


@dataclass(slots=True)
class CommandResult:
    """Unified result type returned by every DBK CLI command.

    Attributes
    ----------
    code : ExitCode
        Exit code for the process.  0 = success, non-zero = error.
    message : str
        Human-readable summary (shown on stderr when code != 0).
    data : Any
        Structured payload.  Commands choose what to put here; it flows
        through ResultFormatter for output.
    warnings : list[str]
        Non-fatal issues encountered during execution (e.g. partial failures,
        deprecated flags, degraded mode).
    details : dict[str, Any]
        Additional structured metadata (e.g. which daemon was stopped,
        how many rows deleted, elapsed_ms).
    """
    code: ExitCode = ExitCode.SUCCESS
    message: str = ""
    data: Any = None
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------
    @classmethod
    def ok(cls, message: str = "", data: Any = None, **kwargs) -> "CommandResult":
        return cls(code=ExitCode.SUCCESS, message=message, data=data, **kwargs)

    @classmethod
    def warning(cls, message: str, data: Any = None, **kwargs) -> "CommandResult":
        return cls(code=ExitCode.WARNING, message=message, data=data, **kwargs)

    @classmethod
    def usage_error(cls, message: str) -> "CommandResult":
        return cls(code=ExitCode.USAGE, message=message)

    @classmethod
    def config_error(cls, message: str) -> "CommandResult":
        return cls(code=ExitCode.CONFIG_ERROR, message=message)

    @classmethod
    def runtime_error(cls, message: str, **kwargs) -> "CommandResult":
        return cls(code=ExitCode.RUNTIME_ERROR, message=message, **kwargs)

    @classmethod
    def data_error(cls, message: str, **kwargs) -> "CommandResult":
        return cls(code=ExitCode.DATA_ERROR, message=message, **kwargs)

    @classmethod
    def permission_error(cls, message: str) -> "CommandResult":
        return cls(code=ExitCode.PERMISSION, message=message)

    @classmethod
    def internal_error(cls, message: str) -> "CommandResult":
        return cls(code=ExitCode.INTERNAL, message=message)

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------
    def exit(self) -> int:
        """Print message to stderr if non-zero code, return exit code."""
        if self.code != ExitCode.SUCCESS:
            sep = "\n" if "\n" in self.message else " "
            for line in self.message.splitlines():
                print(f"error{sep}{line}", file=sys.stderr)
        return int(self.code)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def as_dict(self) -> dict:
        return asdict(self)


class ResultFormatter:
    """Output formatter for CommandResult.

    Supports multiple formats (text / json / yaml) and streams to
    configurable file-like objects so the same formatter works for
    CLI output, HTTP responses, and IPC.
    """

    def __init__(self, stdout: IO[str] | None = None, stderr: IO[str] | None = None):
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr

    def format(self, result: CommandResult, fmt: str = "text") -> int:
        """Format and write result, return exit code.

        Parameters
        ----------
        result : CommandResult
        fmt : "text" | "json" | "json-lines"
            Output format.  text is human-readable, json is machine-readable.
        """
        if fmt == "json" or fmt == "json-lines":
            self._format_json(result)
        else:
            self._format_text(result)
        return int(result.code)

    def _format_json(self, result: CommandResult) -> None:
        payload = result.as_dict()
        # Serialize code as int for JSON compatibility
        payload["code"] = int(payload["code"])
        self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        self.stdout.write("\n")

    def _format_text(self, result: CommandResult) -> None:
        # Warnings
        for w in result.warnings:
            print(f"warning: {w}", file=self.stderr)
        # Main output
        if result.message:
            print(result.message, file=self.stdout)
        # Structured data
        if result.data is not None:
            if isinstance(result.data, str):
                print(result.data, file=self.stdout)
            elif isinstance(result.data, list):
                for item in result.data:
                    print(item, file=self.stdout)
            elif isinstance(result.data, dict):
                for key, val in result.data.items():
                    print(f"{key}: {val}", file=self.stdout)
            else:
                print(repr(result.data), file=self.stdout)
        # Details footer
        if result.details and result.code == ExitCode.SUCCESS:
            for key, val in result.details.items():
                print(f"{key}: {val}", file=self.stdout)


# ----------------------------------------------------------------------
# Command ABC — the interface each DBK CLI command implements
# ----------------------------------------------------------------------


class Command(ABC):
    """Abstract base class for DBK CLI commands.

    Each command owns its argument parser fragment (``configure``) and
    handler (``execute``).  The handler receives the parsed Namespace and
    returns a ``CommandResult`` — never a raw exit code.

    Subclass notes
    --------------
    * Set ``name`` and ``help`` as class attributes.
    * ``configure`` must call ``p.set_defaults(func=self.execute)`` so the
      forwarder in ``cli.py`` can route to the right handler.
    * Prefer raising ``CommandResult`` exceptions (``ConfigError`,
      ``RuntimeError`) over returning error codes directly; the dispatcher
      catches them and converts to ``CommandResult``.
    """

    name: str = ""
    help: str = ""

    @abstractmethod
    def configure(self, subparsers) -> argparse.ArgumentParser:
        """Add this command's argument group to the parser.

        Args:
            subparsers: The result of ``parser.add_subparsers()``.

        Returns:
            The parser created for this command (to attach ``set_defaults``).
        """

    @abstractmethod
    def execute(self, args: argparse.Namespace) -> CommandResult:
        """Run the command with parsed arguments.

        Args:
            args: The parsed argument namespace.

        Returns:
            CommandResult describing the outcome.
        """

    # ------------------------------------------------------------------
    # Shared helpers available to all subclasses
    # ------------------------------------------------------------------
    def add_format_flag(self, parser: argparse.ArgumentParser) -> None:
        """Add ``--format text|json`` to any parser."""
        parser.add_argument(
            "--format",
            default="text",
            choices=["text", "json", "json-lines"],
            help="Output format (default: text)",
        )

    def result(self, args: argparse.Namespace) -> CommandResult:
        """Entry point used by the dispatcher.  Calls ``execute`` and
        formats the result according to ``--format``."""

        # --format flag lives at the top-level namespace; detect it here
        fmt = getattr(args, "format", "text")
        result = self.execute(args)
        formatter = ResultFormatter()
        code = formatter.format(result, fmt)
        # Return a CommandResult whose code already carries the formatter exit
        result.code = ExitCode(code)
        return result


# ----------------------------------------------------------------------
# Command Group — for multi-subcommand groups (e.g. 'dbk config')
# ----------------------------------------------------------------------


class CommandGroup(Command):
    """A command that owns a subparsers group (e.g. 'dbk config', 'dbk collect').

    Subclasses implement ``register_subcommands`` to attach their subparsers.
    """

    subcommand: str = ""
    subcommand_help: str = ""

    def configure(self, subparsers) -> argparse.ArgumentParser:
        p = subparsers.add_parser(self.subcommand, help=self.subcommand_help)
        sub = p.add_subparsers(dest=f"{self.name}_cmd", required=True)
        self.register_subcommands(sub)
        p.set_defaults(func=self._forward)
        return p

    @abstractmethod
    def register_subcommands(self, subparsers) -> None:
        """Register all subcommands under this group."""

    def _forward(self, args: argparse.Namespace) -> CommandResult:
        dest = f"{self.name}_cmd"
        sub_cmd = getattr(args, dest, None)
        if sub_cmd is None:
            return CommandResult.usage_error(f"Missing subcommand for '{self.name}'")
        handler = getattr(args, "func", None)
        if handler is None:
            return CommandResult.usage_error(f"No handler for subcommand '{sub_cmd}'")
        return handler(args)

    def execute(self, args: argparse.Namespace) -> CommandResult:
        # CommandGroup's real entry point is _forward, which subparsers dispatch to.
        # This is reached only if the group itself is called directly without
        # a subcommand (which argparse already rejects as 'required=True').
        return CommandResult.usage_error(f"Missing subcommand for '{self.name}'")

    def _result_from_handler(self, handler: Callable, args: argparse.Namespace) -> CommandResult:
        """Call a handler and return its CommandResult, catching plain ints."""
        raw = handler(args)
        if isinstance(raw, CommandResult):
            return raw
        if isinstance(raw, int):
            # Old-style handlers still returning raw exit codes
            return CommandResult(code=ExitCode(raw))
        return CommandResult.ok(data=raw)
