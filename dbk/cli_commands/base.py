"""Base command protocol for DBK CLI."""
from __future__ import annotations

import argparse
from abc import ABC, abstractmethod
from typing import Callable


class Command(ABC):
    """Abstract base class for CLI commands.

    Each command owns its argument parser fragment and handler.
    The handler receives the parsed Namespace and returns an int exit code.
    """

    name: str = ""
    help: str = ""

    @abstractmethod
    def configure(self, subparsers) -> argparse.ArgumentParser:
        """Add this command's argument group to the parser.

        Args:
            subparsers: The result of parser.add_subparsers().

        Returns:
            The parser created for this command (to attach set_defaults).
        """

    @abstractmethod
    def execute(self, args: argparse.Namespace) -> int:
        """Run the command with parsed arguments.

        Args:
            args: The parsed argument namespace.

        Returns:
            Exit code (0 = success, non-zero = error).
        """

    def _add_parser(
        self, subparsers, name: str, help: str | None = None, **kwargs
    ) -> argparse.ArgumentParser:
        """Helper to add a parser with standard defaults."""
        p = subparsers.add_parser(name, help=help, **kwargs)
        p.set_defaults(func=self.execute)
        return p


class CommandGroup(Command):
    """A command that owns a subparsers group (e.g. 'dbk config', 'dbk collect')."""

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

    def _forward(self, args: argparse.Namespace) -> int:
        # Extract the subcommand name from the dest attribute.
        dest = f"{self.name}_cmd"
        sub_cmd = getattr(args, dest, None)
        if sub_cmd is None:
            print(f"Missing subcommand for '{self.name}'", file=__import__("sys").stderr)
            return 2
        # Delegate to the sub-handler stored on the namespace by set_defaults.
        handler = getattr(args, "func", None)
        if handler is None:
            print(f"No handler for subcommand '{sub_cmd}'", file=__import__("sys").stderr)
            return 2
        return handler(args)