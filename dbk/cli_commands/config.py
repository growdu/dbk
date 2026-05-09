"""'dbk validate' and 'dbk config' commands."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dbk.config import validate_config, load_config
from dbk.config_loader import DEFAULT_CONFIG_PATH


class ValidateCommand:
    """dbk validate — check DBK configuration and environment."""

    name = "validate"
    help = "Validate DBK configuration and environment"

    def configure(self, subparsers) -> argparse.ArgumentParser:
        p = subparsers.add_parser(self.name, help=self.help)
        p.set_defaults(func=self.execute)
        return p

    def execute(self, args: argparse.Namespace) -> int:
        result = validate_config()
        print(json.dumps(result.as_dict(), ensure_ascii=True, indent=2))
        return 0 if result.ok else 2


class ConfigCommand:
    """dbk config — config management group (show / init / get / set)."""

    name = "config"
    help = "Config management"

    def configure(self, subparsers) -> argparse.ArgumentParser:
        p = subparsers.add_parser(self.name, help=self.help)
        sub = p.add_subparsers(dest="config_cmd", required=True)
        self._register_subcommands(sub)
        p.set_defaults(func=self._forward)
        return p

    def _register_subcommands(self, sub):
        # dbk config show
        p = sub.add_parser("show", help="Print resolved TOML configuration")
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

    def _forward(self, args) -> int:
        return getattr(args, "func", lambda _: 2)(args)

    def _cmd_show(self, args) -> int:
        cfg = load_config()
        print(f"# DBK Config (resolved)")
        print(f"# Active config path: {DEFAULT_CONFIG_PATH}")
        print(f"# Project config path: {Path.cwd() / 'config.toml'}")
        print()
        print(json.dumps(cfg, ensure_ascii=True, indent=2))
        return 0

    def _cmd_init(self, args) -> int:
        import shutil

        target = Path(args.path) if args.path else DEFAULT_CONFIG_PATH
        if target.exists() and not args.force:
            print(f"Config already exists at {target}. Use --force to overwrite.", file=__import__("sys").stderr)
            return 2
        src = Path(__file__).parent.parent / "config.default.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        print(f"Initialized config at {target}")
        print("Edit this file to customize your DBK settings.")
        return 0

    def _cmd_get(self, args) -> int:
        cfg = load_config()
        val = cfg.get(*args.key.split("."))
        print(val if val is not None else "")
        return 0

    def _cmd_set(self, args) -> int:
        # Minimal set implementation — load config, modify, save.
        from dbk.config import load_user_config, save_config
        cfg = load_user_config()
        keys = args.key.split(".")
        if args.value is None:
            # Delete key.
            current = cfg
            for k in keys[:-1]:
                if k not in current:
                    return 0
                current = current[k]
            current.pop(keys[-1], None)
        else:
            # Set key.
            current = cfg
            for k in keys[:-1]:
                current = current.setdefault(k, {})
            current[keys[-1]] = args.value
        save_config(cfg, path=args.path)
        return 0