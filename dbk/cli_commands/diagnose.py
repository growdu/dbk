"""'dbk diagnose' command."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dbk.diagnose import diagnose_latency_incident


class DiagnoseCommand:
    """'dbk diagnose' group — incident diagnosis."""

    name = "diagnose"
    help = "Diagnose database incidents"

    def configure(self, subparsers) -> argparse.ArgumentParser:
        p = subparsers.add_parser(self.name, help=self.help)
        sub = p.add_subparsers(dest="diagnose_cmd", required=True)
        self._register_subcommands(sub)
        p.set_defaults(func=self._forward)
        return p

    def _register_subcommands(self, sub):
        p = sub.add_parser("latency", help="Diagnose a latency incident")
        p.add_argument("--instance", default="pg-main-01")
        p.add_argument("--from", dest="from_ts")
        p.add_argument("--to", dest="to_ts")
        p.add_argument("--output", help="Output directory for evidence bundle")
        p.set_defaults(func=self._cmd_latency)

    def _forward(self, args) -> int:
        return getattr(args, "func", lambda _: 2)(args)

    def _cmd_latency(self, args) -> int:
        result = diagnose_latency_incident(
            instance=args.instance,
            from_ts=getattr(args, "from_ts", None),
            to_ts=getattr(args, "to_ts", None),
            output_dir=getattr(args, "output", None),
            cwd=Path.cwd(),
        )
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0