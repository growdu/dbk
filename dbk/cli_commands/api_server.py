"""'dbk api-server' command."""
from __future__ import annotations

import argparse
from dbk.api_server import run_server


class APIServerCommand:
    """'dbk api-server' — start the DBK Agent REST API server."""

    name = "api-server"
    help = "Start the DBK Agent REST API server"

    def configure(self, subparsers) -> argparse.ArgumentParser:
        p = subparsers.add_parser(self.name, help=self.help)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8080)
        p.add_argument("--workers", type=int, default=1)
        p.add_argument("--log-level", default="INFO")
        p.set_defaults(func=self.execute)
        return p

    def execute(self, args: argparse.Namespace) -> int:
        try:
            run_server(
                host=args.host,
                port=args.port,
                workers=args.workers,
                log_level=args.log_level,
            )
        except KeyboardInterrupt:
            return 0
        return 0