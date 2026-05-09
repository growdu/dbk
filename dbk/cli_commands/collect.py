"""'dbk collect' command group."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dbk.collectors import collect_mock_runtime_metrics
from dbk.pg_collectors import collect_pg_health, collect_pg_runtime_metrics, PgCollectorError
from dbk.storage import RuntimeStore
from dbk.config import runtime_db_path
from dbk.collector_daemon import (
    daemon_status, list_daemons, run_loop, start_daemon,
    stop_all_daemons, stop_daemon,
)

from dbk.cli_commands.base import CommandGroup, CommandResult


def _store() -> RuntimeStore:
    store = RuntimeStore(runtime_db_path())
    store.init_schema()
    return store


def _resolve_dsn(source: str, dsn: str | None) -> str | None:
    if source != "pgstat":
        return None
    import os
    return dsn or os.environ.get("DBK_PG_DSN")


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _fmt_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--format",
        default=argparse.SUPPRESS,
        choices=["text", "json", "json-lines"],
        help="Output format (default: text)",
    )


class CollectCommand(CommandGroup):
    """'dbk collect' group — metrics collection and daemon management."""

    name = "collect"
    subcommand = "collect"
    subcommand_help = "Collect runtime metrics"

    def register_subcommands(self, sub):
        # dbk collect (no subcommand — one-shot collect)
        p = sub.add_parser("collect", help="Collect runtime metrics (one-shot)")
        p.add_argument("--instance", default="pg-main-01")
        p.add_argument("--source", default="mock", choices=["mock", "pgstat"])
        p.add_argument("--dsn", help="PostgreSQL DSN (pgstat only)")
        _fmt_arg(p)
        p.set_defaults(func=self._cmd_collect)

        # dbk collect health
        p = sub.add_parser("health", help="Check collector health/readiness")
        p.add_argument("--source", default="pgstat", choices=["mock", "pgstat"])
        p.add_argument("--dsn")
        _fmt_arg(p)
        p.set_defaults(func=self._cmd_health)

        # dbk collect daemon
        daemon = sub.add_parser("daemon", help="Manage collector daemon")
        daemon_sub = daemon.add_subparsers(dest="daemon_cmd", required=True)

        # daemon start
        ps = daemon_sub.add_parser("start", help="Start collector daemon")
        ps.add_argument("--instance", default="pg-main-01")
        ps.add_argument("--source", default="mock", choices=["mock", "pgstat"])
        ps.add_argument("--dsn")
        ps.add_argument("--interval-sec", type=int, default=15)
        ps.add_argument("--priority", type=int, default=50)
        ps.add_argument("--tags")
        ps.add_argument("--max-collections-per-minute", type=int, default=0)
        ps.add_argument("--max-running", type=int, default=3)
        ps.add_argument("--preempt-lower-priority", action="store_true")
        _fmt_arg(ps)
        ps.set_defaults(func=self._cmd_daemon_start)

        # daemon stop
        ps = daemon_sub.add_parser("stop", help="Stop collector daemon")
        ps.add_argument("--instance")
        ps.add_argument("--all", action="store_true")
        _fmt_arg(ps)
        ps.set_defaults(func=self._cmd_daemon_stop)

        # daemon status
        ps = daemon_sub.add_parser("status", help="Show daemon status")
        ps.add_argument("--instance")
        _fmt_arg(ps)
        ps.set_defaults(func=self._cmd_daemon_status)

        # daemon list
        ps = daemon_sub.add_parser("list", help="List all daemon instances")
        ps.add_argument("--tag")
        ps.add_argument("--source")
        ps.add_argument("--instance-pattern")
        ps.add_argument("--min-priority", type=int)
        _fmt_arg(ps)
        ps.set_defaults(func=self._cmd_daemon_list)

        # daemon run (foreground)
        ps = daemon_sub.add_parser("run", help="Run daemon in foreground (debug)")
        ps.add_argument("--instance", default="pg-main-01")
        ps.add_argument("--source", default="mock", choices=["mock", "pgstat"])
        ps.add_argument("--dsn")
        ps.add_argument("--interval-sec", type=int, default=15)
        ps.add_argument("--priority", type=int, default=50)
        ps.add_argument("--tags")
        ps.add_argument("--max-collections-per-minute", type=int, default=0)
        ps.add_argument("--max-running", type=int, default=3)
        ps.add_argument("--preempt-lower-priority", action="store_true")
        ps.set_defaults(func=self._cmd_daemon_run)

    def _cmd_collect(self, args) -> CommandResult:
        store = _store()
        source = args.source
        instance = args.instance
        dsn = _resolve_dsn(source, args.dsn)
        warnings: list[str] = []
        try:
            if source == "mock":
                events = collect_mock_runtime_metrics(instance=instance)
            else:
                if not dsn:
                    return CommandResult.config_error(
                        "Missing DSN: pass --dsn or set DBK_PG_DSN."
                    )
                result = collect_pg_runtime_metrics(instance=instance, dsn=dsn)
                events = result.events
                warnings = result.warnings
        except PgCollectorError as exc:
            return CommandResult.runtime_error(str(exc))
        count = store.insert_events(events)
        return CommandResult.ok(
            message=f"Collected {count} metrics for instance={instance}",
            data={"count": count, "instance": instance, "source": source},
            warnings=warnings,
        )

    def _cmd_health(self, args) -> CommandResult:
        if args.source != "pgstat":
            return CommandResult.ok(
                data={"ok": True, "degraded": False, "collector": args.source},
            )
        dsn = _resolve_dsn(args.source, args.dsn)
        if not dsn:
            return CommandResult.config_error(
                "Missing DSN: pass --dsn or set DBK_PG_DSN."
            )
        report = collect_pg_health(dsn=dsn)
        return CommandResult.ok(data=report.to_dict())

    def _cmd_daemon_start(self, args) -> CommandResult:
        dsn = _resolve_dsn(args.source, args.dsn)
        if args.source == "pgstat" and not dsn:
            return CommandResult.config_error(
                "Missing DSN: pass --dsn or set DBK_PG_DSN."
            )
        try:
            state = start_daemon(
                instance=args.instance, source=args.source, dsn=dsn,
                interval_sec=args.interval_sec, priority=args.priority,
                tags=_parse_tags(args.tags),
                max_collections_per_minute=args.max_collections_per_minute,
                max_running=args.max_running,
                preempt_lower_priority=args.preempt_lower_priority,
                cwd=Path.cwd(),
            )
        except (RuntimeError, ValueError) as exc:
            return CommandResult.runtime_error(str(exc))
        return CommandResult.ok(
            data={
                "started": True, "pid": state.pid, "instance": state.instance,
                "interval_sec": state.interval_sec, "priority": state.priority,
                "tags": state.tags, "max_collections_per_minute": state.max_collections_per_minute,
                "source": state.source,
            },
        )

    def _cmd_daemon_stop(self, args) -> CommandResult:
        if args.all or not args.instance:
            payload = stop_all_daemons(cwd=Path.cwd())
        else:
            payload = stop_daemon(instance=args.instance, cwd=Path.cwd())
        return CommandResult.ok(data=payload)

    def _cmd_daemon_status(self, args) -> CommandResult:
        payload = daemon_status(instance=args.instance, cwd=Path.cwd())
        return CommandResult.ok(data=payload)

    def _cmd_daemon_list(self, args) -> CommandResult:
        daemons = list_daemons(
            cwd=Path.cwd(), include_stale=True,
            tag=getattr(args, "tag", None),
            source=getattr(args, "source", None),
            instance_pattern=getattr(args, "instance_pattern", None),
            min_priority=getattr(args, "min_priority", None),
        )
        running = any(bool(item.get("running")) for item in daemons)
        return CommandResult.ok(
            data={"daemons": daemons, "running": running},
        )

    def _cmd_daemon_run(self, args) -> CommandResult:
        # Foreground run blocks — return immediately with a start ack.
        dsn = _resolve_dsn(args.source, args.dsn)
        if args.source == "pgstat" and not dsn:
            return CommandResult.config_error(
                "Missing DSN: pass --dsn or set DBK_PG_DSN."
            )
        try:
            state = start_daemon(
                instance=args.instance, source=args.source, dsn=dsn,
                interval_sec=args.interval_sec, priority=args.priority,
                tags=_parse_tags(args.tags),
                max_collections_per_minute=args.max_collections_per_minute,
                max_running=args.max_running,
                preempt_lower_priority=args.preempt_lower_priority,
                cwd=Path.cwd(),
            )
        except (RuntimeError, ValueError) as exc:
            return CommandResult.runtime_error(str(exc))
        # Run foreground loop (blocks).  User pressed Ctrl-C to stop.
        run_loop(state=state, store=_store(), cwd=Path.cwd())
        return CommandResult.ok(message="Daemon stopped.")
