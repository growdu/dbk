"""'dbk validate' and 'dbk config' commands."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from dbk.config import validate_config, load_config
from dbk.config_loader import DEFAULT_CONFIG_PATH

from dbk.cli_commands.base import Command, CommandResult, CommandGroup, ExitCode


class ValidateCommand(Command):
    """dbk validate — check DBK configuration and environment."""

    name = "validate"
    help = "Validate DBK configuration and environment"

    def configure(self, subparsers) -> argparse.ArgumentParser:
        p = subparsers.add_parser(self.name, help=self.help)
        p.add_argument(
            "--format",
            default=argparse.SUPPRESS,
            choices=["text", "json", "json-lines"],
            help="Output format (default: text)",
        )
        p.set_defaults(func=self.execute)
        return p

    def execute(self, args: argparse.Namespace) -> CommandResult:
        result = validate_config()
        if result.ok:
            return CommandResult.ok(
                message="Configuration is valid.",
                data=result.as_dict(),
                details={"problems": len(result.problems)},
            )
        return CommandResult.config_error(
            message=f"Configuration has {len(result.problems)} problem(s).",
            data=result.as_dict(),
        )


class ConfigCommand(CommandGroup):
    """dbk config — config management group (show / init / get / set)."""

    name = "config"
    subcommand = "config"
    subcommand_help = "Config management"

    def register_subcommands(self, sub):
        # dbk config show
        p = sub.add_parser("show", help="Print resolved TOML configuration")
        p.add_argument(
            "--format",
            default=argparse.SUPPRESS,
            choices=["text", "json", "json-lines"],
            help="Output format (default: text)",
        )
        p.set_defaults(func=self._cmd_show)

        # dbk config init
        p = sub.add_parser("init", help="Create a config file from the default template")
        p.add_argument("--path", help="Target path")
        p.add_argument("--force", action="store_true", help="Overwrite existing config")
        p.set_defaults(func=self._cmd_init)

        # dbk config get
        p = sub.add_parser("get", help="Print the resolved value of a config key")
        p.add_argument("key", help="Config key (dot-notation, e.g. agent.provider)")
        p.set_defaults(func=self._cmd_get)

        # dbk config set
        p = sub.add_parser("set", help="Set a config key in the user config file")
        p.add_argument("key", help="Config key (dot-notation)")
        p.add_argument("value", nargs="?", default=None)
        p.add_argument("--path", help="Target config path")
        p.set_defaults(func=self._cmd_set)

    def _cmd_show(self, args) -> CommandResult:
        cfg = load_config()
        return CommandResult.ok(
            data=cfg,
            details={
                "active_config": str(DEFAULT_CONFIG_PATH),
                "project_config": str(Path.cwd() / "config.toml"),
            },
        )

    def _cmd_init(self, args) -> CommandResult:
        target = Path(args.path) if args.path else DEFAULT_CONFIG_PATH
        if target.exists() and not args.force:
            return CommandResult.config_error(
                message=f"Config already exists at {target}. Use --force to overwrite.",
            )
        src = Path(__file__).parent.parent / "config.default.toml"
        if not src.exists():
            return CommandResult.config_error(
                message=f"Default config template not found at {src}.",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        return CommandResult.ok(
            message=f"Initialized config at {target}",
            details={"path": str(target)},
        )

    def _cmd_get(self, args) -> CommandResult:
        cfg = load_config()
        val = cfg.get(*args.key.split("."))
        return CommandResult.ok(
            data={"key": args.key, "value": val},
            details={"key": args.key},
        )

    def _cmd_set(self, args) -> CommandResult:
        import tomllib
        cfg_path = Path(args.path) if args.path else DEFAULT_CONFIG_PATH

        # Load existing config or start from defaults.
        if cfg_path.exists():
            try:
                raw = cfg_path.read_text(encoding="utf-8")
                data: dict = tomllib.loads(raw)
            except Exception as exc:
                return CommandResult.config_error(
                    message=f"Failed to read config at {cfg_path}: {exc}",
                )
        else:
            data = {}

        keys = args.key.split(".")
        if args.value is None:
            # Delete key.
            current = data
            for k in keys[:-1]:
                if k not in current:
                    return CommandResult.ok(message=f"Key '{args.key}' not found, nothing to remove.")
                current = current[k]
            current.pop(keys[-1], None)
            message = f"Removed key '{args.key}'."
        else:
            # Set key.
            current = data
            for k in keys[:-1]:
                current = current.setdefault(k, {})
            current[keys[-1]] = args.value
            message = f"Set '{args.key}' = '{args.value}'."

        # Write back.
        import tomli_w
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(tomli_w.dumps(data), encoding="utf-8")
        return CommandResult.ok(
            message=message,
            details={"key": args.key, "value": args.value},
        )
