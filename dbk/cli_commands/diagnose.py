"""'dbk diagnose' command."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dbk.diagnose import diagnose_latency_incident

from dbk.cli_commands.base import CommandGroup, CommandResult


class DiagnoseCommand(CommandGroup):
    """'dbk diagnose' group — incident diagnosis."""

    name = "diagnose"
    subcommand = "diagnose"
    subcommand_help = "Diagnose database incidents"

    def register_subcommands(self, sub):
        p = sub.add_parser("latency", help="Diagnose a latency incident")
        p.add_argument("--instance", default="pg-main-01")
        p.add_argument("--from", dest="from_ts")
        p.add_argument("--to", dest="to_ts")
        p.add_argument("--output", help="Output directory for evidence bundle")
        p.add_argument(
            "--format",
            default=argparse.SUPPRESS,
            choices=["text", "json", "json-lines"],
            help="Output format (default: text)",
        )
        p.set_defaults(func=self._cmd_latency)

    def _cmd_latency(self, args) -> CommandResult:
        result = diagnose_latency_incident(
            instance=args.instance,
            from_ts=getattr(args, "from_ts", None),
            to_ts=getattr(args, "to_ts", None),
            output_dir=getattr(args, "output", None),
            cwd=Path.cwd(),
        )
        return CommandResult.ok(data=result)