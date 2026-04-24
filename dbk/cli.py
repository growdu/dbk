from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .collectors import collect_mock_runtime_metrics
from .config import artifacts_root, runtime_db_path
from .diagnose import diagnose_latency_incident
from .pg_collectors import PgCollectorError, collect_pg_runtime_metrics
from .storage import RuntimeStore
from .thresholds import load_thresholds
from .tracing import run_trace_profile, supported_profiles


def _store() -> RuntimeStore:
    store = RuntimeStore(runtime_db_path())
    store.init_schema()
    return store


def cmd_init(_: argparse.Namespace) -> int:
    store = _store()
    store.init_schema()
    artifacts_root().mkdir(parents=True, exist_ok=True)
    print(f"Initialized DBK runtime DB: {runtime_db_path()}")
    print(f"Initialized artifacts dir: {artifacts_root()}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    store = _store()
    if args.source == "mock":
        events = collect_mock_runtime_metrics(instance=args.instance)
    elif args.source == "pgstat":
        dsn = args.dsn or os.environ.get("DBK_PG_DSN")
        if not dsn:
            print("Missing DSN: pass --dsn or set DBK_PG_DSN.", file=sys.stderr)
            return 2
        try:
            result = collect_pg_runtime_metrics(instance=args.instance, dsn=dsn)
        except PgCollectorError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        events = result.events
        if result.warnings:
            print("collector warnings:", file=sys.stderr)
            for item in result.warnings:
                print(f"- {item}", file=sys.stderr)
    else:
        print("Unsupported --source value.", file=sys.stderr)
        return 2
    count = store.insert_events(events)
    print(f"Collected {count} metrics for instance={args.instance}")
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

    p_metrics = sub.add_parser("metrics", help="Query metrics from sqlite store")
    p_metrics.add_argument("--metric", required=True)
    p_metrics.add_argument("--instance")
    p_metrics.add_argument("--limit", type=int, default=20)
    p_metrics.set_defaults(func=cmd_metrics)

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
