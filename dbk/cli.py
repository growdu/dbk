from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .collector_daemon import (
    daemon_status,
    list_daemons,
    run_loop,
    start_daemon,
    stop_all_daemons,
    stop_daemon,
)
from .collectors import collect_mock_runtime_metrics
from .config import artifacts_root, runtime_db_path
from .diagnose import diagnose_latency_incident
from .pg_collectors import PgCollectorError, collect_pg_health, collect_pg_runtime_metrics
from .runtime_cleanup import cleanup_runtime_data
from .runtime_cleanup_daemon import (
    build_cleanup_report,
    cleanup_daemon_status,
    run_cleanup_loop,
    start_cleanup_daemon,
    stop_cleanup_daemon,
)
from .storage import RuntimeStore
from .thresholds import load_thresholds
from .tracing import run_trace_profile, supported_profiles


def _store() -> RuntimeStore:
    store = RuntimeStore(runtime_db_path())
    store.init_schema()
    return store


def _resolve_dsn(source: str, dsn: str | None) -> str | None:
    if source != "pgstat":
        return None
    return dsn or os.environ.get("DBK_PG_DSN")


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _collect_events(
    *,
    source: str,
    instance: str,
    dsn: str | None,
) -> tuple[list[object], list[str]]:
    if source == "mock":
        return collect_mock_runtime_metrics(instance=instance), []
    if source == "pgstat":
        resolved_dsn = _resolve_dsn(source, dsn)
        if not resolved_dsn:
            raise PgCollectorError("Missing DSN: pass --dsn or set DBK_PG_DSN.")
        result = collect_pg_runtime_metrics(instance=instance, dsn=resolved_dsn)
        return result.events, result.warnings
    raise PgCollectorError("Unsupported --source value.")


def cmd_init(_: argparse.Namespace) -> int:
    store = _store()
    store.init_schema()
    artifacts_root().mkdir(parents=True, exist_ok=True)
    print(f"Initialized DBK runtime DB: {runtime_db_path()}")
    print(f"Initialized artifacts dir: {artifacts_root()}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    store = _store()
    try:
        events, warnings = _collect_events(source=args.source, instance=args.instance, dsn=args.dsn)
    except PgCollectorError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    count = store.insert_events(events)
    if warnings:
        print("collector warnings:", file=sys.stderr)
        for item in warnings:
            print(f"- {item}", file=sys.stderr)
    print(f"Collected {count} metrics for instance={args.instance}")
    return 0


def cmd_collect_health(args: argparse.Namespace) -> int:
    if args.source != "pgstat":
        payload = {
            "ok": True,
            "degraded": False,
            "details": {"collector": args.source},
            "warnings": [],
            "error": None,
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


def cmd_collect_daemon_start(args: argparse.Namespace) -> int:
    dsn = _resolve_dsn(args.source, args.dsn)
    if args.source == "pgstat" and not dsn:
        print("Missing DSN: pass --dsn or set DBK_PG_DSN.", file=sys.stderr)
        return 2
    try:
        state = start_daemon(
            instance=args.instance,
            source=args.source,
            interval_sec=args.interval_sec,
            priority=args.priority,
            tags=_parse_tags(args.tags),
            max_collections_per_minute=args.max_collections_per_minute,
            max_running=args.max_running,
            preempt_lower_priority=args.preempt_lower_priority,
            dsn=dsn,
            cwd=Path.cwd(),
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "started": True,
                "pid": state.pid,
                "instance": state.instance,
                "interval_sec": state.interval_sec,
                "priority": state.priority,
                "tags": state.tags,
                "max_collections_per_minute": state.max_collections_per_minute,
                "source": state.source,
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


def cmd_collect_daemon_stop(args: argparse.Namespace) -> int:
    if args.all or not args.instance:
        payload = stop_all_daemons(cwd=Path.cwd())
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if payload.get("stopped") else 2
    payload = stop_daemon(instance=args.instance, cwd=Path.cwd())
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload.get("stopped") else 2


def cmd_collect_daemon_status(args: argparse.Namespace) -> int:
    payload = daemon_status(instance=args.instance, cwd=Path.cwd())
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload.get("running") else 2


def cmd_collect_daemon_list(args: argparse.Namespace) -> int:
    payload = {
        "daemons": list_daemons(
            cwd=Path.cwd(),
            include_stale=True,
            tag=args.tag,
            source=args.source,
            instance_pattern=args.instance_pattern,
            min_priority=args.min_priority,
        )
    }
    payload["running"] = any(bool(item.get("running")) for item in payload["daemons"])
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload["running"] else 2


def cmd_collect_daemon_run(args: argparse.Namespace) -> int:
    store = _store()

    def _collect_once() -> tuple[int, list[str]]:
        events, warnings = _collect_events(source=args.source, instance=args.instance, dsn=args.dsn)
        count = store.insert_events(events)
        return count, warnings

    state_path = Path(args.state_path) if args.state_path else None
    return run_loop(
        collect_once=_collect_once,
        interval_sec=args.interval_sec,
        state_path=state_path,
        max_collections_per_minute=args.max_collections_per_minute,
    )


def cmd_runtime_cleanup(args: argparse.Namespace) -> int:
    store = _store()

    try:
        summary = cleanup_runtime_data(
            store=store,
            older_than_hours=args.older_than_hours,
            instance=args.instance,
            dry_run=args.dry_run,
            skip_trace_db=args.skip_trace_db,
            skip_artifacts=args.skip_artifacts,
            vacuum=args.vacuum,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(summary.to_dict(), ensure_ascii=True, indent=2))
    return 0


def cmd_runtime_cleanup_daemon_start(args: argparse.Namespace) -> int:
    try:
        state = start_cleanup_daemon(
            interval_sec=args.interval_sec,
            older_than_hours=args.older_than_hours,
            instance=args.instance,
            skip_trace_db=args.skip_trace_db,
            skip_artifacts=args.skip_artifacts,
            vacuum=args.vacuum,
            cwd=Path.cwd(),
        )
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "started": True,
                "pid": state.pid,
                "interval_sec": state.interval_sec,
                "older_than_hours": state.older_than_hours,
                "instance": state.instance,
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


def cmd_runtime_cleanup_daemon_status(_: argparse.Namespace) -> int:
    payload = cleanup_daemon_status(cwd=Path.cwd())
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload.get("running") else 2


def cmd_runtime_cleanup_daemon_stop(_: argparse.Namespace) -> int:
    payload = stop_cleanup_daemon(cwd=Path.cwd())
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload.get("stopped") else 2


def cmd_runtime_cleanup_daemon_run(args: argparse.Namespace) -> int:
    state_path = Path(args.state_path) if args.state_path else None
    history_path = Path(args.history_path) if args.history_path else None
    return run_cleanup_loop(
        interval_sec=args.interval_sec,
        older_than_hours=args.older_than_hours,
        instance=args.instance,
        skip_trace_db=args.skip_trace_db,
        skip_artifacts=args.skip_artifacts,
        vacuum=args.vacuum,
        state_path=state_path,
        history_path=history_path,
    )


def cmd_runtime_cleanup_report(args: argparse.Namespace) -> int:
    try:
        payload = build_cleanup_report(
            limit=args.limit,
            window_hours=args.window_hours,
            cwd=Path.cwd(),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    daemon_payload = cleanup_daemon_status(cwd=Path.cwd())
    payload["daemon"] = daemon_payload
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    store = _store()
    rows = store.query_latest_metric(metric=args.metric, instance=args.instance, limit=args.limit)
    output = [
        {
            "ts": row["ts"],
            "instance": row["instance"],
            "source": row["source"],
            "category": row["category"],
            "metric": row["metric"],
            "value": row["value"],
            "labels": json.loads(row["labels_json"] or "{}"),
        }
        for row in rows
    ]
    print(json.dumps(output, ensure_ascii=True, indent=2))
    return 0


def cmd_trace_profiles(_: argparse.Namespace) -> int:
    print(json.dumps(supported_profiles(), ensure_ascii=True, indent=2))
    return 0


def cmd_trace_run(args: argparse.Namespace) -> int:
    store = _store()
    try:
        result = run_trace_profile(
            profile=args.profile,
            task_id=args.task_id,
            duration_sec=args.duration,
            artifacts_root=artifacts_root(),
            execute=args.execute,
            approve_privileged=args.approve_privileged,
        )
    except (ValueError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    store.insert_trace_artifact(result.artifact)
    print(f"Trace profile complete: {args.profile}")
    print(f"stdout: {result.stdout_path}")
    print(f"summary: {result.summary_path}")
    return 0


def cmd_diagnose_latency(args: argparse.Namespace) -> int:
    store = _store()
    try:
        thresholds = load_thresholds(Path(args.thresholds_file)) if args.thresholds_file else None
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid thresholds file: {exc}", file=sys.stderr)
        return 2
    result = diagnose_latency_incident(
        store=store,
        instance=args.instance,
        task_id=args.task_id,
        artifacts_root=artifacts_root(),
        auto_trace=args.auto_trace,
        thresholds=thresholds,
    )
    print(f"verdict: {result.verdict}")
    if result.findings:
        print("findings:")
        for item in result.findings:
            print(f"- {item}")
    print(f"evidence_bundle: {result.evidence_bundle}")
    if result.trace_summary:
        print(f"trace_summary: {result.trace_summary}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dbk", description="Database Kernel Agent CLI (MVP)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize runtime DB and artifact folders")
    p_init.set_defaults(func=cmd_init)

    p_collect = sub.add_parser("collect", help="Collect runtime metrics")
    p_collect.add_argument("--instance", default="pg-main-01")
    p_collect.add_argument("--source", default="mock", choices=["mock", "pgstat"])
    p_collect.add_argument("--dsn", help="PostgreSQL DSN, used only when --source pgstat")
    p_collect.set_defaults(func=cmd_collect)
    collect_sub = p_collect.add_subparsers(dest="collect_cmd")

    p_collect_health = collect_sub.add_parser("health", help="Check collector health/readiness")
    p_collect_health.add_argument("--source", default="pgstat", choices=["mock", "pgstat"])
    p_collect_health.add_argument("--dsn", help="PostgreSQL DSN, used when --source pgstat")
    p_collect_health.set_defaults(func=cmd_collect_health)

    p_collect_daemon = collect_sub.add_parser("daemon", help="Manage background collector daemon")
    daemon_sub = p_collect_daemon.add_subparsers(dest="daemon_cmd", required=True)

    p_collect_daemon_start = daemon_sub.add_parser("start", help="Start collector daemon")
    p_collect_daemon_start.add_argument("--instance", default="pg-main-01")
    p_collect_daemon_start.add_argument("--source", default="mock", choices=["mock", "pgstat"])
    p_collect_daemon_start.add_argument("--dsn", help="PostgreSQL DSN, used when --source pgstat")
    p_collect_daemon_start.add_argument("--interval-sec", type=int, default=15)
    p_collect_daemon_start.add_argument("--priority", type=int, default=50, help="1(low)-100(high)")
    p_collect_daemon_start.add_argument("--tags", help="Comma-separated labels, e.g. prod,critical")
    p_collect_daemon_start.add_argument(
        "--max-collections-per-minute",
        type=int,
        help="Optional per-daemon throttle limit",
    )
    p_collect_daemon_start.add_argument(
        "--max-running",
        type=int,
        help="Optional upper bound of concurrently running daemons",
    )
    p_collect_daemon_start.add_argument(
        "--preempt-lower-priority",
        action="store_true",
        help="When max-running reached, preempt lower-priority daemon if possible",
    )
    p_collect_daemon_start.set_defaults(func=cmd_collect_daemon_start)

    p_collect_daemon_stop = daemon_sub.add_parser("stop", help="Stop collector daemon")
    p_collect_daemon_stop.add_argument("--instance", help="Target instance name")
    p_collect_daemon_stop.add_argument("--all", action="store_true", help="Stop all daemon instances")
    p_collect_daemon_stop.set_defaults(func=cmd_collect_daemon_stop)

    p_collect_daemon_status = daemon_sub.add_parser("status", help="Show collector daemon status")
    p_collect_daemon_status.add_argument("--instance", help="Target instance name; omit to show all")
    p_collect_daemon_status.set_defaults(func=cmd_collect_daemon_status)

    p_collect_daemon_list = daemon_sub.add_parser("list", help="List daemon instances")
    p_collect_daemon_list.add_argument("--tag", help="Filter by tag")
    p_collect_daemon_list.add_argument("--source", choices=["mock", "pgstat"], help="Filter by source")
    p_collect_daemon_list.add_argument("--instance-pattern", help="Filter by wildcard pattern, e.g. pg-prod-*")
    p_collect_daemon_list.add_argument("--min-priority", type=int, help="Filter by minimum priority")
    p_collect_daemon_list.set_defaults(func=cmd_collect_daemon_list)

    # Internal command used by daemon start.
    p_collect_daemon_run = daemon_sub.add_parser("run", help=argparse.SUPPRESS)
    p_collect_daemon_run.add_argument("--instance", default="pg-main-01")
    p_collect_daemon_run.add_argument("--source", default="mock", choices=["mock", "pgstat"])
    p_collect_daemon_run.add_argument("--dsn", help="PostgreSQL DSN, used when --source pgstat")
    p_collect_daemon_run.add_argument("--interval-sec", type=int, default=15)
    p_collect_daemon_run.add_argument("--max-collections-per-minute", type=int)
    p_collect_daemon_run.add_argument("--state-path", help=argparse.SUPPRESS)
    p_collect_daemon_run.set_defaults(func=cmd_collect_daemon_run)

    p_metrics = sub.add_parser("metrics", help="Query metrics from sqlite store")
    p_metrics.add_argument("--metric", required=True)
    p_metrics.add_argument("--instance")
    p_metrics.add_argument("--limit", type=int, default=20)
    p_metrics.set_defaults(func=cmd_metrics)

    p_runtime = sub.add_parser("runtime", help="Runtime maintenance operations")
    runtime_sub = p_runtime.add_subparsers(dest="runtime_cmd", required=True)
    p_runtime_cleanup = runtime_sub.add_parser("cleanup", help="Cleanup retained runtime data")
    p_runtime_cleanup.add_argument("--older-than-hours", type=float, default=168.0)
    p_runtime_cleanup.add_argument("--instance", help="Only cleanup metrics for this instance")
    p_runtime_cleanup.add_argument("--dry-run", action="store_true")
    p_runtime_cleanup.add_argument("--skip-trace-db", action="store_true")
    p_runtime_cleanup.add_argument("--skip-artifacts", action="store_true")
    p_runtime_cleanup.add_argument("--vacuum", action="store_true")
    p_runtime_cleanup.set_defaults(func=cmd_runtime_cleanup)

    p_runtime_cleanup_daemon = runtime_sub.add_parser(
        "cleanup-daemon",
        help="Manage periodic runtime cleanup daemon",
    )
    cleanup_daemon_sub = p_runtime_cleanup_daemon.add_subparsers(
        dest="cleanup_daemon_cmd",
        required=True,
    )

    p_runtime_cleanup_daemon_start = cleanup_daemon_sub.add_parser("start", help="Start cleanup daemon")
    p_runtime_cleanup_daemon_start.add_argument("--interval-sec", type=int, default=3600)
    p_runtime_cleanup_daemon_start.add_argument("--older-than-hours", type=float, default=168.0)
    p_runtime_cleanup_daemon_start.add_argument("--instance", help="Only cleanup metrics for this instance")
    p_runtime_cleanup_daemon_start.add_argument("--skip-trace-db", action="store_true")
    p_runtime_cleanup_daemon_start.add_argument("--skip-artifacts", action="store_true")
    p_runtime_cleanup_daemon_start.add_argument("--vacuum", action="store_true")
    p_runtime_cleanup_daemon_start.set_defaults(func=cmd_runtime_cleanup_daemon_start)

    p_runtime_cleanup_daemon_status = cleanup_daemon_sub.add_parser("status", help="Show cleanup daemon status")
    p_runtime_cleanup_daemon_status.set_defaults(func=cmd_runtime_cleanup_daemon_status)

    p_runtime_cleanup_daemon_stop = cleanup_daemon_sub.add_parser("stop", help="Stop cleanup daemon")
    p_runtime_cleanup_daemon_stop.set_defaults(func=cmd_runtime_cleanup_daemon_stop)

    p_runtime_cleanup_daemon_run = cleanup_daemon_sub.add_parser("run", help=argparse.SUPPRESS)
    p_runtime_cleanup_daemon_run.add_argument("--interval-sec", type=int, default=3600)
    p_runtime_cleanup_daemon_run.add_argument("--older-than-hours", type=float, default=168.0)
    p_runtime_cleanup_daemon_run.add_argument("--instance")
    p_runtime_cleanup_daemon_run.add_argument("--skip-trace-db", action="store_true")
    p_runtime_cleanup_daemon_run.add_argument("--skip-artifacts", action="store_true")
    p_runtime_cleanup_daemon_run.add_argument("--vacuum", action="store_true")
    p_runtime_cleanup_daemon_run.add_argument("--state-path", help=argparse.SUPPRESS)
    p_runtime_cleanup_daemon_run.add_argument("--history-path", help=argparse.SUPPRESS)
    p_runtime_cleanup_daemon_run.set_defaults(func=cmd_runtime_cleanup_daemon_run)

    p_runtime_cleanup_report = runtime_sub.add_parser("cleanup-report", help="Show cleanup history report")
    p_runtime_cleanup_report.add_argument("--limit", type=int, default=50)
    p_runtime_cleanup_report.add_argument("--window-hours", type=float, help="Only include recent N hours")
    p_runtime_cleanup_report.set_defaults(func=cmd_runtime_cleanup_report)

    p_trace = sub.add_parser("trace", help="Trace operations")
    trace_sub = p_trace.add_subparsers(dest="trace_cmd", required=True)

    p_profiles = trace_sub.add_parser("profiles", help="List supported trace profiles")
    p_profiles.set_defaults(func=cmd_trace_profiles)

    p_run = trace_sub.add_parser("run", help="Run a trace profile")
    p_run.add_argument("--profile", required=True, choices=supported_profiles())
    p_run.add_argument("--task-id", required=True)
    p_run.add_argument("--duration", type=int, default=30)
    p_run.add_argument("--execute", action="store_true")
    p_run.add_argument(
        "--approve-privileged",
        action="store_true",
        help="Required when --execute is used for privileged trace runs",
    )
    p_run.set_defaults(func=cmd_trace_run)

    p_diagnose = sub.add_parser("diagnose", help="Incident diagnosis")
    diagnose_sub = p_diagnose.add_subparsers(dest="diagnose_cmd", required=True)
    p_latency = diagnose_sub.add_parser("latency", help="Diagnose latency incident")
    p_latency.add_argument("--instance", default="pg-main-01")
    p_latency.add_argument("--task-id", required=True)
    p_latency.add_argument("--auto-trace", action=argparse.BooleanOptionalAction, default=True)
    p_latency.add_argument(
        "--thresholds-file",
        help="Optional JSON file to override diagnosis thresholds",
    )
    p_latency.set_defaults(func=cmd_diagnose_latency)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
