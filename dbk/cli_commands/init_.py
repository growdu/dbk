"""'dbk init' command."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from dbk.storage import RuntimeStore
from dbk.config import artifacts_root, runtime_db_path
from dbk.config_loader import DEFAULT_CONFIG_PATH


class InitCommand:
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
        p.set_defaults(func=self.execute)
        return p

    def execute(self, args: argparse.Namespace) -> int:
        store = RuntimeStore(runtime_db_path())
        store.init_schema()
        artifacts_root().mkdir(parents=True, exist_ok=True)
        print(f"Initialized DBK runtime DB: {runtime_db_path()}")
        print(f"Initialized artifacts dir: {artifacts_root()}")

        # Handle config initialization.
        config_target = DEFAULT_CONFIG_PATH
        config_local = Path.cwd() / "config.toml"
        if config_local.exists():
            config_target = config_local
        else:
            config_target = DEFAULT_CONFIG_PATH

        if config_target.exists() and not args.force:
            print(f"\nConfig file already exists at {config_target}.")
            print("Use --force to overwrite it.")
            return 0

        src = Path(__file__).parent.parent / "config.default.toml"
        if not src.exists():
            print(f"\nWarning: default config template not found at {src}", file=sys.stderr)
        else:
            config_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, config_target)
            print(f"Initialized config file: {config_target}")

        print()
        print("Next steps:")
        print("  1. Edit the config file to set your API keys")
        print("  2. Run 'dbk config show' to verify your configuration")
        print("  3. Run 'dbk validate' to check your environment")
        print("  4. Run 'dbk collect daemon start' to start the collector daemon")
        print("  5. Run 'dbk agent interactive' to start the AI agent REPL")
        return 0


# Singleton for register_all
def get_command() -> InitCommand:
    return InitCommand()