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


class CollectCommand:
    """'dbk collect' group — metrics collection and daemon management."""

    name = "collect"
    help = "Collect runtime metrics"

    def configure(self, subparsers) -> argparse.ArgumentParser:
        p = subparsers.add_parser(self.name, help=self.help)
        sub = p.add_subparsers(dest="collect_cmd", required=True)
        self._register_subcommands(sub)
        p.set_defaults(func=self._forward)
        return p

    def _register_subcommands(self, sub):
        # dbk collect (no subcommand — one-shot collect)
        p = sub.add_parser("collect", help="Collect runtime metrics (one-shot)")
        p.add_argument("--instance", default="pg-main-01")
        p.add_argument("--source", default="mock", choices=["mock", "pgstat"])
        p.add_argument("--dsn", help="PostgreSQL DSN (pgstat only)")
        p.set_defaults(func=self._cmd_collect)

        # dbk collect health
        p = sub.add_parser("health", help="Check collector health/readiness")
        p.add_argument("--source", default="pgstat", choices=["mock", "pgstat"])
        p.add_argument("--dsn")
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
        ps.set_defaults(func=self._cmd_daemon_start)

        # daemon stop
        ps = daemon_sub.add_parser("stop", help="Stop collector daemon")
        ps.add_argument("--instance")
        ps.add_argument("--all", action="store_true")
        ps.set_defaults(func=self._cmd_daemon_stop)

        # daemon status
        ps = daemon_sub.add_parser("status", help="Show daemon status")
        ps.add_argument("--instance")
        ps.set_defaults(func=self._cmd_daemon_status)

        # daemon list
        ps = daemon_sub.add_parser("list", help="List all daemon instances")
        ps.add_argument("--tag")
        ps.add_argument("--source")
        ps.add_argument("--instance-pattern")
        ps.add_argument("--min-priority", type=int)
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

    def _forward(self, args) -> int:
        return getattr(args, "func", lambda _: 2)(args)

    def _cmd_collect(self, args) -> int:
        store = _store()
        source = args.source
        instance = args.instance
        dsn = _resolve_dsn(source, args.dsn)
        try:
            if source == "mock":
                events = collect_mock_runtime_metrics(instance=instance)
                warnings: list[str] = []
            else:
                if not dsn:
                    print("Missing DSN: pass --dsn or set DBK_PG_DSN.", file=sys.stderr)
                    return 2
                result = collect_pg_runtime_metrics(instance=instance, dsn=dsn)
                events = result.events
                warnings = result.warnings
        except PgCollectorError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        count = store.insert_events(events)
        if warnings:
            print("collector warnings:", file=sys.stderr)
            for item in warnings:
                print(f"  - {item}", file=sys.stderr)
        print(f"Collected {count} metrics for instance={instance}")
        return 0

    def _cmd_health(self, args) -> int:
        if args.source != "pgstat":
            payload = {
                "ok": True, "degraded": False,
                "details": {"collector": args.source},
                "warnings": [], "error": None,
            }
            print(json.dumps(payload, ensure_ascii=True, indent=2))
            return 0
        dsn = _resolve_dsn(args.source, args.dsn)
        if not dsn:
            print("Missing DSN: pass --dsn or set DBK_PG_DSN.", file=sys.stderr)
            return 2
        report = collect_pg_health(dsn=dsn)
        print(json.dumps(report.to_dict(), ensure_ascii=True, indent=2))
        return 0 if report.ok else 2

    def _cmd_daemon_start(self, args) -> int:
        dsn = _resolve_dsn(args.source, args.dsn)
        if args.source == "pgstat" and not dsn:
            print("Missing DSN: pass --dsn or set DBK_PG_DSN.", file=sys.stderr)
            return 2
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
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({
            "started": True, "pid": state.pid, "instance": state.instance,
            "interval_sec": state.interval_sec, "priority": state.priority,
            "tags": state.tags, "max_collections_per_minute": state.max_collections_per_minute,
            "source": state.source,
        }, ensure_ascii=True, indent=2))
        return 0

    def _cmd_daemon_stop(self, args) -> int:
        if args.all or not args.instance:
            payload = stop_all_daemons(cwd=Path.cwd())
        else:
            payload = stop_daemon(instance=args.instance, cwd=Path.cwd())
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if payload.get("stopped") else 2

    def _cmd_daemon_status(self, args) -> int:
        payload = daemon_status(instance=args.instance, cwd=Path.cwd())
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if payload.get("running") else 2

    def _cmd_daemon_list(self, args) -> int:
        daemons = list_daemons(
            cwd=Path.cwd(), include_stale=True,
            tag=getattr(args, "tag", None),
            source=getattr(args, "source", None),
            instance_pattern=getattr(args, "instance_pattern", None),
            min_priority=getattr(args, "min_priority", None),
        )
        payload: dict[str, Any] = {"daemons": daemons}
        payload["running"] = any(bool(item.get("running")) for item in daemons)
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if payload["running"] else 2

    def _cmd_daemon_run(self, args) -> int:
        dsn = _resolve_dsn(args.source, args.dsn)
        if args.source == "pgstat" and not dsn:
            print("Missing DSN: pass --dsn or set DBK_PG_DSN.", file=sys.stderr)
            return 2
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
            print(str(exc), file=sys.stderr)
            return 2
        run_loop(state=state, store=_store(), cwd=Path.cwd())