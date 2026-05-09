"""'dbk runtime', 'dbk metrics', 'dbk trace' commands."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dbk.collectors import collect_mock_runtime_metrics
from dbk.pg_collectors import collect_pg_runtime_metrics, PgCollectorError
from dbk.storage import RuntimeStore
from dbk.config import runtime_db_path
from dbk.tracing import run_trace_profile, supported_profiles, PROFILE_COMMANDS
from dbk.diagnose import diagnose_latency_incident
from dbk.runtime_cleanup import cleanup_runtime_data
from dbk.runtime_cleanup_daemon import (
    build_cleanup_report, cleanup_daemon_status,
    run_cleanup_loop, start_cleanup_daemon, stop_cleanup_daemon,
)


def _store() -> RuntimeStore:
    store = RuntimeStore(runtime_db_path())
    store.init_schema()
    return store


class RuntimeCommand:
    """'dbk runtime' group — cleanup daemon + 'dbk metrics' + 'dbk trace'."""

    name = "runtime"
    help = "Runtime data management (cleanup, metrics, trace)"

    def configure(self, subparsers) -> argparse.ArgumentParser:
        p = subparsers.add_parser(self.name, help=self.help)
        sub = p.add_subparsers(dest="runtime_cmd", required=True)
        self._register_subcommands(sub)
        p.set_defaults(func=self._forward)
        return p

    def _register_subcommands(self, sub):
        # ---- dbk runtime cleanup ----
        p = sub.add_parser("cleanup", help="Clean up old runtime data")
        p.add_argument("--instance")
        p.add_argument("--safety-floor-hours", type=float, default=24.0)
        p.add_argument("--max-delete-per-run", type=int, default=100_000)
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--force", action="store_true", help="Skip truncation safety check")
        p.set_defaults(func=self._cmd_cleanup)

        # cleanup daemon start
        p = sub.add_parser("cleanup-daemon", help="Manage cleanup daemon")
        cd = p.add_subparsers(dest="cleanup_daemon_cmd", required=True)
        ps = cd.add_parser("start", help="Start cleanup daemon")
        ps.add_argument("--interval-hr", type=float, default=6.0)
        ps.add_argument("--safety-floor-hours", type=float, default=24.0)
        ps.add_argument("--max-delete-per-run", type=int, default=100_000)
        ps.add_argument("--dry-run", action="store_true")
        ps.set_defaults(func=self._cmd_cleanup_daemon_start)

        ps = cd.add_parser("stop", help="Stop cleanup daemon")
        ps.set_defaults(func=self._cmd_cleanup_daemon_stop)

        ps = cd.add_parser("status", help="Show cleanup daemon status")
        ps.set_defaults(func=self._cmd_cleanup_daemon_status)

        ps = cd.add_parser("run", help="Run cleanup daemon in foreground")
        ps.add_argument("--interval-hr", type=float, default=6.0)
        ps.add_argument("--safety-floor-hours", type=float, default=24.0)
        ps.add_argument("--max-delete-per-run", type=int, default=100_000)
        ps.add_argument("--dry-run", action="store_true")
        ps.set_defaults(func=self._cmd_cleanup_daemon_run)

        # dbk runtime cleanup-report
        p = sub.add_parser("cleanup-report", help="Print cleanup history report")
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--window-hours", type=float, default=168.0)
        p.set_defaults(func=self._cmd_report)

        # ---- dbk metrics ----
        p = sub.add_parser("metrics", help="Query stored metrics")
        p.add_argument("metric", nargs="?", help="Metric name (omit for all)")
        p.add_argument("--instance")
        p.add_argument("--limit", type=int, default=10)
        p.add_argument("--from", dest="metric_from")
        p.add_argument("--to", dest="metric_to")
        p.add_argument("--aggregate", choices=["avg", "max", "min"])
        p.add_argument("--metric-limit", type=int)
        p.set_defaults(func=self._cmd_metrics)

        # ---- dbk trace profiles ----
        p = sub.add_parser("trace", help="Trace command execution")
        trace_sub = p.add_subparsers(dest="trace_cmd", required=True)
        pp = trace_sub.add_parser("profiles", help="List supported trace profiles")
        pp.set_defaults(func=self._cmd_trace_profiles)
        pp = trace_sub.add_parser("run", help="Run a trace profile")
        pp.add_argument("profile", choices=list(supported_profiles()))
        pp.add_argument("--command", help="Override command to trace")
        pp.set_defaults(func=self._cmd_trace_run)

    def _forward(self, args) -> int:
        return getattr(args, "func", lambda _: 2)(args)

    def _cmd_cleanup(self, args) -> int:
        payload = cleanup_runtime_data(
            instance=args.instance,
            safety_floor_hours=args.safety_floor_hours,
            max_delete_per_run=args.max_delete_per_run,
            dry_run=args.dry_run,
            force=args.force,
            cwd=Path.cwd(),
        )
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if payload.get("truncated") is not True else 1

    def _cmd_cleanup_daemon_start(self, args) -> int:
        try:
            state = start_cleanup_daemon(
                interval_hr=args.interval_hr,
                safety_floor_hours=args.safety_floor_hours,
                max_delete_per_run=args.max_delete_per_run,
                dry_run=args.dry_run,
                cwd=Path.cwd(),
            )
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({"started": True, "pid": state.pid, "instance": state.instance}, ensure_ascii=True, indent=2))
        return 0

    def _cmd_cleanup_daemon_stop(self, args) -> int:
        payload = stop_cleanup_daemon(cwd=Path.cwd())
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if payload.get("stopped") else 2

    def _cmd_cleanup_daemon_status(self, args) -> int:
        payload = cleanup_daemon_status(cwd=Path.cwd())
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if payload.get("running") else 2

    def _cmd_cleanup_daemon_run(self, args) -> int:
        try:
            state = start_cleanup_daemon(
                interval_hr=args.interval_hr,
                safety_floor_hours=args.safety_floor_hours,
                max_delete_per_run=args.max_delete_per_run,
                dry_run=args.dry_run,
                cwd=Path.cwd(),
            )
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        run_cleanup_loop(state=state, cwd=Path.cwd())

    def _cmd_report(self, args) -> int:
        try:
            payload = build_cleanup_report(
                limit=args.limit,
                window_hours=args.window_hours,
                cwd=Path.cwd(),
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        daemon = cleanup_daemon_status(cwd=Path.cwd())
        payload["daemon"] = daemon
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    def _cmd_metrics(self, args) -> int:
        store = _store()
        metric = getattr(args, "metric", None)
        instance = getattr(args, "instance", None)
        limit = args.limit
        m_from = getattr(args, "metric_from", None)
        m_to = getattr(args, "metric_to", None)
        agg = getattr(args, "aggregate", None)
        m_limit = getattr(args, "metric_limit", None)

        if m_from is not None:
            rows = store.query_metric_range(
                metric=metric or "", instance=instance or "", from_ts=m_from, to_ts=m_to, limit=m_limit or 1000,
            )
            if agg:
                print(json.dumps(RuntimeStore.aggregate_rows(rows), ensure_ascii=True, indent=2))
            else:
                print(json.dumps([
                    {"ts": r["ts"], "instance": r["instance"], "source": r["source"], "metric": r["metric"], "value": r["value"]}
                    for r in rows
                ], ensure_ascii=True, indent=2))
        else:
            rows = store.query_latest_metric(metric=metric or "", instance=instance or "", limit=limit)
            print(json.dumps([
                {"ts": r["ts"], "instance": r["instance"], "source": r["source"], "category": r["category"], "metric": r["metric"], "value": r["value"]}
                for r in rows
            ], ensure_ascii=True, indent=2))
        return 0

    def _cmd_trace_profiles(self, args) -> int:
        profiles = supported_profiles()
        print(json.dumps({k: v for k, v in profiles.items()}, ensure_ascii=True, indent=2))
        return 0

    def _cmd_trace_run(self, args) -> int:
        result = run_trace_profile(args.profile, command=args.command)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0