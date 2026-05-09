"""'dbk init' command."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from dbk.storage import RuntimeStore
from dbk.config import artifacts_root, runtime_db_path
from dbk.config_loader import DEFAULT_CONFIG_PATH

from dbk.cli_commands.base import Command, CommandResult, ExitCode


class InitCommand(Command):
    """dbk init — initialize runtime DB, artifact dirs, and config file."""

    name = "init"
    help = "Initialize runtime DB, artifacts, and optionally a config file"

    def configure(self, subparsers) -> argparse.ArgumentParser:
        p = subparsers.add_parser(self.name, help=self.help)
        p.add_argument(
            "--force",
            action="store_true",
            help="Overwrite existing config file if one is found",
        )
        p.add_argument(
            "--format",
            default=argparse.SUPPRESS,
            choices=["text", "json", "json-lines"],
            help="Output format (default: text)",
        )
        p.set_defaults(func=self.execute)
        return p

    def execute(self, args: argparse.Namespace) -> CommandResult:
        warnings: list[str] = []
        store = RuntimeStore(runtime_db_path())
        store.init_schema()
        artifacts_root().mkdir(parents=True, exist_ok=True)

        # Handle config initialization.
        config_target = DEFAULT_CONFIG_PATH
        config_local = Path.cwd() / "config.toml"
        if config_local.exists():
            config_target = config_local
        else:
            config_target = DEFAULT_CONFIG_PATH

        if config_target.exists() and not args.force:
            return CommandResult.ok(
                message=f"Config file already exists at {config_target}. Use --force to overwrite.",
            )

        src = Path(__file__).parent.parent / "config.default.toml"
        if not src.exists():
            warnings.append(f"Default config template not found at {src}")
        else:
            config_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, config_target)

        next_steps = [
            "1. Edit the config file to set your API keys",
            "2. Run 'dbk config show' to verify your configuration",
            "3. Run 'dbk validate' to check your environment",
            "4. Run 'dbk collect daemon start' to start the collector daemon",
            "5. Run 'dbk agent interactive' to start the AI agent REPL",
        ]

        return CommandResult.ok(
            message="\n".join(next_steps),
            data={
                "db": str(runtime_db_path()),
                "artifacts": str(artifacts_root()),
                "config": str(config_target),
            },
            warnings=warnings,
            details={
                "initialized": ["db", "artifacts"],
            },
        )
