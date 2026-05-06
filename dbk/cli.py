from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, cast

from .collector_daemon import (
    daemon_status,
    list_daemons,
    run_loop,
    start_daemon,
    stop_all_daemons,
    stop_daemon,
)
from .collectors import collect_mock_runtime_metrics
from .config import artifacts_root, runtime_db_path, validate_config
from .diagnose import diagnose_latency_incident
from .models import RuntimeEvent
from .pg_collectors import PgCollectorError, collect_pg_health, collect_pg_runtime_metrics
from .runtime_cleanup import cleanup_runtime_data
from .runtime_cleanup_daemon import (
    build_cleanup_report,
    cleanup_daemon_status,
    run_cleanup_loop,
    start_cleanup_daemon,
    stop_cleanup_daemon,
)
from .agent.state import WorkflowStage
from .agent.core import Agent
from .agent.workflow import WorkflowOrchestrator
from .providers import get_provider
from .alerting import (
    AlertEngine,
    AlertEvent,
    AlertNotifier,
    AlertPrometheusExporter,
    AlertStore,
    AlertRule,
    LogNotifier,
    WebhookNotifier,
)
from .alerting.daemon import (
    alert_daemon_status,
    run_alert_loop,
    start_alert_daemon,
    stop_alert_daemon,
)
from .alerting.engine import load_rules as load_alert_rules
from .alerting.models import AlertState, Severity, DEFAULT_ALERT_RULES
from .config import dbk_root
from .storage import RuntimeStore
from .thresholds import load_thresholds
from .tracing import run_trace_profile, supported_profiles, PROFILE_COMMANDS

# Agent CLI lazy import (avoids loading LLM packages until needed).
_agent_cli_main: Callable[..., Any] | None = None


def _get_agent_main() -> Callable[..., Any]:
    global _agent_cli_main
    if _agent_cli_main is None:
        from . import cli_agent
        _agent_cli_main = cli_agent.main
    return _agent_cli_main


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
) -> tuple[list[RuntimeEvent], list[str]]:
    if source == "mock":
        return collect_mock_runtime_metrics(instance=instance), []
    if source == "pgstat":
        resolved_dsn = _resolve_dsn(source, dsn)
        if not resolved_dsn:
            raise PgCollectorError("Missing DSN: pass --dsn or set DBK_PG_DSN.")
        result = collect_pg_runtime_metrics(instance=instance, dsn=resolved_dsn)
        return result.events, result.warnings
    raise PgCollectorError("Unsupported --source value.")


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize DBK: runtime DB, artifact folders, and optionally a config file."""
    store = _store()
    store.init_schema()
    artifacts_root().mkdir(parents=True, exist_ok=True)
    print(f"Initialized DBK runtime DB: {runtime_db_path()}")
    print(f"Initialized artifacts dir: {artifacts_root()}")

    # Handle config initialization.
    from dbk.config_loader import DEFAULT_CONFIG_PATH
    config_target = DEFAULT_CONFIG_PATH
    config_local = Path.cwd() / "config.toml"
    # Prefer local project config if it exists, otherwise use XDG path.
    if config_local.exists():
        config_target = config_local
    else:
        config_target = DEFAULT_CONFIG_PATH

    if config_target.exists() and not args.force:
        print(f"\nConfig file already exists at {config_target}.")
        print("Use --force to overwrite it.")
        return 0

    src = Path(__file__).parent / "config.default.toml"
    if not src.exists():
        print(f"\nWarning: default config template not found at {src}", file=sys.stderr)
    else:
        import shutil
        config_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, config_target)
        print(f"Initialized config file: {config_target}")

    print()
    print("Next steps:")
    print("  1. Edit the config file to set your API keys (e.g. openai_api_key, anthropic_api_key)")
    print("  2. Run 'dbk config show' to verify your configuration")
    print("  3. Run 'dbk validate' to check your environment")
    print("  4. Run 'dbk collect daemon start' to start the collector daemon")
    print("  5. Run 'dbk agent interactive' to start the AI agent REPL")
    return 0


def cmd_validate(_: argparse.Namespace) -> int:
    result = validate_config()
    print(json.dumps(result.as_dict(), ensure_ascii=True, indent=2))
    return 0 if result.ok else 2


def cmd_config_show(_: argparse.Namespace) -> int:
    """Print the resolved TOML config."""
    from dbk.config import load_config
    from dbk.config_loader import DEFAULT_CONFIG_PATH
    cfg = load_config()
    print(f"# DBK Config (resolved)")
    print(f"# Active config path: {DEFAULT_CONFIG_PATH}")
    print(f"# Project config path: {Path.cwd() / 'config.toml'}")
    print()
    print(json.dumps(cfg, ensure_ascii=True, indent=2))
    return 0


def cmd_config_init(args: argparse.Namespace) -> int:
    """Initialize a config file at the XDG default path."""
    from dbk.config_loader import DEFAULT_CONFIG_PATH
    target = Path(args.path) if args.path else DEFAULT_CONFIG_PATH
    if target.exists() and not args.force:
        print(f"Config already exists at {target}. Use --force to overwrite.", file=sys.stderr)
        return 2
    # Copy from bundled default
    import shutil
    src = Path(__file__).parent / "config.default.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    print(f"Initialized config at {target}")
    print(f"Edit this file to customize your DBK settings.")
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    """Print the resolved value of a config key (supports dot notation)."""
    from dbk.config import load_config
    cfg = load_config()
    val = cfg.get(*args.key.split("."))
    print(val if val is not None else "")
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    """Set a config key in the user config file (creates it if needed).

    Supports dot-notation for nested keys, e.g. dbk config set agent.provider anthropic.
    """
    from dbk.config_loader import DEFAULT_CONFIG_PATH, TOMLConfig, TOMLError
    import tomllib
    cfg_path = Path(args.path) if args.path else DEFAULT_CONFIG_PATH

    # Load existing config or start from defaults.
    if cfg_path.exists():
        try:
            raw = cfg_path.read_text(encoding="utf-8")
            data: dict = tomllib.loads(raw)
        except Exception as exc:
            print(f"Failed to read existing config: {exc}", file=sys.stderr)
            return 2
    else:
        data = {}

    # Navigate/create the nested path.
    keys = args.key.split(".")
    current: dict = data
    for k in keys[:-1]:
        if k not in current:
            current[k] = {}
        if not isinstance(current[k], dict):
            print(f"Cannot set {args.key}: {'.'.join(keys[:keys.index(k)+1])} is not a table.", file=sys.stderr)
            return 2
        current = current[k]

    # Parse the value.
    final_key = keys[-1]
    if args.value is None:
        # Delete the key
        if final_key in current:
            del current[final_key]
            print(f"Deleted: {args.key}")
        else:
            print(f"Key not found: {args.key}", file=sys.stderr)
            return 1
    else:
        # Type-detect: try bool, int, float, then string.
        v = args.value
        for conv, check in [
            (lambda s: s.lower() in ("true", "false"), lambda s: s.lower() == "true"),
            (str.isdigit, int),
            (lambda s: "." in s and s.replace(".", "", 1).isdigit(), float),
        ]:
            try:
                if check(v):
                    v = conv(v)
                    break
            except ValueError:
                pass
        current[final_key] = v
        print(f"Set: {args.key} = {v!r}")

    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    def _toml_str(val: object) -> str:
        """Serialize a nested dict to TOML with [section] headers.

        Handles dot-notation in keys as TOML section paths. Our config
        has only top-level table sections (no inline tables or arrays of
        tables), so this simple serializer is sufficient.
        """
        lines: list[str] = []

        def write_dict(d: dict, prefix: str = "") -> None:
            for k, v in sorted(d.items(), key=lambda x: x[0]):
                if isinstance(v, dict):
                    section = f"{prefix}.{k}" if prefix else k
                    lines.append(f"[{section}]")
                    write_dict(v, section)
                elif v is None:
                    pass
                elif isinstance(v, bool):
                    lines.append(f"{k} = {str(v).lower()}")
                elif isinstance(v, int):
                    lines.append(f"{k} = {v}")
                elif isinstance(v, float):
                    lines.append(f"{k} = {v}")
                else:
                    escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
                    lines.append(f'{k} = "{escaped}"')

        write_dict(cast(dict, val))
        return "\n".join(lines)

    text = _toml_str(data)
    cfg_path.write_text(text, encoding="utf-8")
    print(f"Wrote: {cfg_path}")
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
    daemons = list_daemons(
        cwd=Path.cwd(),
        include_stale=True,
        tag=args.tag,
        source=args.source,
        instance_pattern=args.instance_pattern,
        min_priority=args.min_priority,
    )
    payload: dict[str, Any] = {"daemons": daemons}
    payload["running"] = any(bool(item.get("running")) for item in daemons)
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
            max_delete_per_run=args.max_delete_per_run,
            safety_floor_hours=args.safety_floor_hours,
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
            max_delete_per_run=args.max_delete_per_run,
            safety_floor_hours=args.safety_floor_hours,
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
                "max_delete_per_run": state.max_delete_per_run,
                "safety_floor_hours": state.safety_floor_hours,
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
        max_delete_per_run=args.max_delete_per_run,
        safety_floor_hours=args.safety_floor_hours,
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
    if getattr(args, "metric_from", None) is not None:
        # Range query mode.
        rows = store.query_metric_range(
            metric=args.metric,
            instance=args.instance,
            from_ts=args.metric_from,
            to_ts=args.metric_to,
            limit=args.metric_limit,
        )
        if args.aggregate:
            agg = RuntimeStore.aggregate_rows(rows)
            print(json.dumps(agg, ensure_ascii=True, indent=2))
        else:
            output = [
                {
                    "ts": row["ts"],
                    "instance": row["instance"],
                    "source": row["source"],
                    "metric": row["metric"],
                    "value": row["value"],
                }
                for row in rows
            ]
            print(json.dumps(output, ensure_ascii=True, indent=2))
    else:
        # Latest query mode (original behavior).
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

    # Audit trail for every privileged escalation attempt.
    if args.execute:
        summary = result.artifact.summary_json
        try:
            import pwd as _pwd
            username = _pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            username = os.environ.get("USER", "unknown")
        try:
            store.insert_trace_audit(
                task_id=args.task_id,
                username=username,
                action_id="org.dbk.bpftrace.run",
                command=PROFILE_COMMANDS.get(args.profile, []),
                duration_sec=args.duration,
                profile=args.profile,
                mode=summary.get("mode", "simulated"),
                escalation=summary.get("escalation", "none"),
                exit_code=None,
                approved_by_cli=args.approve_privileged,
                error=None,
            )
        except Exception as exc:
            # Never let audit failure break the trace command.
            print(
                f"[audit warning] failed to write audit record: {exc}",
                file=sys.stderr,
            )

    print(f"Trace profile complete: {args.profile}")
    print(f"stdout: {result.stdout_path}")
    print(f"summary: {result.summary_path}")
    return 0


def cmd_alert_rules_list(args: argparse.Namespace) -> int:
    rules_path = Path(args.rules_path) if args.rules_path else None
    try:
        if rules_path:
            rules = load_alert_rules(rules_path)
        else:
            # Use built-in default rules
            rules = list(DEFAULT_ALERT_RULES)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error loading rules: {exc}", file=sys.stderr)
        return 2
    if args.format == "text":
        if not rules:
            print("No rules loaded.")
            return 0
        for r in rules:
            print(f"  {r.name}: {r.metric} {r.operator} {r.threshold} [{r.severity.value}]")
            if r.instance:
                print(f"    instance={r.instance}")
            print(f"    {r.description}")
    else:
        print(json.dumps({"rules": [r.to_dict() for r in rules], "count": len(rules)}, ensure_ascii=True, indent=2))
    return 0


def cmd_alert_rules_validate(args: argparse.Namespace) -> int:
    try:
        rules = load_alert_rules(Path(args.rules_path))
        print(json.dumps({"valid": True, "count": len(rules), "rules": [r.to_dict() for r in rules]}, ensure_ascii=True, indent=2))
        return 0
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=True, indent=2))
        return 2


def cmd_alert_rules_add(args: argparse.Namespace) -> int:
    from dbk.alerting.models import AlertRule, Severity
    rule = AlertRule(
        name=args.name,
        metric=args.metric,
        operator=args.operator,
        threshold=args.threshold,
        severity=Severity(args.severity),
        description=args.description,
        instance=args.instance,
        minimum_duration_sec=args.min_duration,
        cooldown_sec=args.cooldown,
    )
    print(json.dumps({"added": rule.to_dict()}, ensure_ascii=True, indent=2))
    return 0


def cmd_alert_rules_export(args: argparse.Namespace) -> int:
    if args.include_builtin:
        rules = list(DEFAULT_ALERT_RULES)
    else:
        rules = list(DEFAULT_ALERT_RULES)
    path = Path(args.path)
    payload = {"rules": [r.to_dict() for r in rules]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True))
    print(json.dumps({"exported": str(path), "count": len(rules)}))
    return 0


def cmd_alert_rules_eval(args: argparse.Namespace) -> int:
    try:
        rules = load_alert_rules(Path(args.rules_path))
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error loading rules: {exc}", file=sys.stderr)
        return 2

    engine = AlertEngine(rules=rules)
    notifiers: list[AlertNotifier] = [LogNotifier()]
    if args.webhook_url:
        notifiers.append(WebhookNotifier(url=args.webhook_url, secret=args.webhook_secret))

    def on_event(event: AlertEvent) -> None:
        for n in notifiers:
            n.send(event)

    engine.add_listener(on_event)

    store = _store()
    rt_metrics = _collect_metrics_for_alerting(store, instance=args.instance)
    events = engine.evaluate_batch(rt_metrics)

    firing = engine.get_firing_alerts()
    counts = engine.get_firing_count_by_severity()
    payload = {
        "events": [{"type": e.type, "alert": e.alert.to_dict()} for e in events],
        "firing": [a.to_dict() for a in firing],
        "summary": {
            "total_evaluated": len(rules),
            "firing_count": engine.get_active_count(),
            "by_severity": {k.value: v for k, v in counts.items()},
        },
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_alert_daemon_start(args: argparse.Namespace) -> int:
    try:
        state = start_alert_daemon(
            interval_sec=args.interval_sec,
            rules_path=Path(args.rules_path) if args.rules_path else None,
            webhook_url=args.webhook_url,
            webhook_secret=args.webhook_secret,
            prometheus_host=args.prometheus_host,
            prometheus_port=args.prometheus_port,
            cwd=Path.cwd(),
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"started": True, **state.to_dict()}, ensure_ascii=True, indent=2))
    return 0


def cmd_alert_daemon_stop(_: argparse.Namespace) -> int:
    payload = stop_alert_daemon(cwd=Path.cwd())
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload.get("stopped") else 2


def cmd_alert_daemon_status(_: argparse.Namespace) -> int:
    payload = alert_daemon_status(cwd=Path.cwd())
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload.get("running") else 2


def cmd_alert_daemon_run(args: argparse.Namespace) -> int:
    # Build agent if --enable-agent is set.
    agent: Agent | None = None
    if args.enable_agent:
        from dbk.providers import get_provider
        agent = Agent(provider=get_provider())
    state_path = Path(args.state_path) if args.state_path else None
    return run_alert_loop(
        interval_sec=args.interval_sec,
        rules_path=Path(args.rules_path) if args.rules_path else None,
        webhook_url=args.webhook_url,
        webhook_secret=args.webhook_secret,
        prometheus_host=args.prometheus_host,
        prometheus_port=args.prometheus_port,
        state_path=state_path,
        cwd=Path.cwd(),
        agent=agent,
    )


def cmd_alert_history(args: argparse.Namespace) -> int:
    store = AlertStore(dbk_root() / "alerts.sqlite")
    store.init_schema()
    state = None
    if args.state:
        state = AlertState(args.state)
    alerts = store.query_alerts(
        rule_name=args.rule_name,
        instance=args.instance,
        state=state,
        since_hours=args.since_hours,
        limit=args.limit,
    )
    payload = {
        "alerts": [a.to_dict() for a in alerts],
        "count": len(alerts),
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_alert_prometheus(args: argparse.Namespace) -> int:
    exporter = AlertPrometheusExporter(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
    )
    store = AlertStore(dbk_root() / "alerts.sqlite")
    store.init_schema()
    firing = store.query_firing_alerts()
    exporter.sync_alerts(firing)
    counts = store.count_firing_by_severity()
    exporter.sync_summary(
        firing=len(firing),
        warning=int(counts.get("warning", 0)),
        critical=int(counts.get("critical", 0)),
        info=int(counts.get("info", 0)),
    )
    if args.once:
        print(exporter.metrics_text, end="")
        return 0
    print(f"Alert Prometheus exporter listening on {args.listen_host}:{args.listen_port}")
    print("Press Ctrl+C to stop.")
    exporter.start()
    import time
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        exporter.stop()
    return 0


def _collect_metrics_for_alerting(store: RuntimeStore, instance: str | None = None) -> list[dict[str, object]]:
    """Collect recent metrics for alert evaluation."""
    metrics: list[dict[str, object]] = []
    with store.connect() as conn:
        sql = "SELECT DISTINCT metric, instance FROM runtime_metric"
        params: list[object] = []
        if instance:
            sql += " WHERE instance = ?"
            params.append(instance)
        rows = conn.execute(sql, tuple(params))
        for row in rows:
            metric = str(row["metric"])
            inst = str(row["instance"])
            latest = conn.execute(
                """
                SELECT ts, value, labels_json FROM runtime_metric
                WHERE metric = ? AND instance = ?
                ORDER BY ts DESC LIMIT 1
                """,
                (metric, inst),
            )
            for lr in latest:
                metrics.append({
                    "metric": metric,
                    "value": float(lr["value"]),
                    "instance": inst,
                    "ts": str(lr["ts"]),
                })
    return metrics


def cmd_api_server(args: argparse.Namespace) -> int:
    """Start the DBK Agent REST API server."""
    from dbk.api_server import run_server
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


def cmd_run(args: argparse.Namespace) -> int:
    """Run agent with a natural-language goal, auto-mapping to workflow stage."""
    provider = get_provider()
    agent = Agent(provider=provider)

    # Determine target stage from --stage flag or infer from intent.
    target_stage: WorkflowStage | None = None
    if args.stage:
        target_stage = WorkflowStage(args.stage)
    else:
        # Infer stage from goal keywords.
        goal_lower = args.goal.lower()
        if any(kw in goal_lower for kw in ["monitor", "health", "check", "status", "metrics"]):
            target_stage = WorkflowStage.REQUIREMENTS
        elif any(kw in goal_lower for kw in ["design", "plan", "architecture", "approach"]):
            target_stage = WorkflowStage.DESIGN
        elif any(kw in goal_lower for kw in ["implement", "build", "create", "set up", "configure"]):
            target_stage = WorkflowStage.IMPLEMENT
        elif any(kw in goal_lower for kw in ["test", "validate", "verify", "check"]):
            target_stage = WorkflowStage.TEST
        elif any(kw in goal_lower for kw in ["deploy", "runtime", "start", "run"]):
            target_stage = WorkflowStage.RUNTIME
        elif any(kw in goal_lower for kw in ["document", "doc", "runbook", "readme"]):
            target_stage = WorkflowStage.DOC
        elif any(kw in goal_lower for kw in ["ops", "operational", "handover", "cleanup"]):
            target_stage = WorkflowStage.OPS
        else:
            target_stage = WorkflowStage.REQUIREMENTS  # Default

    session_id = args.session
    if args.resume and session_id:
        state = agent.get_session(session_id)
        if state:
            session_id = state.session_id
        else:
            print(f"Session not found: {session_id}", file=sys.stderr)
            return 2

    orchestrator = WorkflowOrchestrator(agent=agent, auto_transition_on_completion=not args.no_auto_transition)

    if args.full:
        # Run full workflow.
        result = orchestrator.run_full_workflow(goal=args.goal, session_id=session_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    else:
        # Run single stage.
        result = orchestrator.run_stage(message=args.goal, target_stage=target_stage, session_id=session_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
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

    p_init = sub.add_parser("init", help="Initialize runtime DB, artifacts, and optionally a config file")
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing config file if one is found",
    )
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="Run agent with a natural-language goal (auto-maps to workflow stage)")
    p_run.add_argument("goal", help="Natural-language goal or task description")
    p_run.add_argument("--session", help="Session ID to resume")
    p_run.add_argument("--resume", action="store_true", help="Resume existing session")
    p_run.add_argument(
        "--stage",
        choices=[s.value for s in WorkflowStage],
        help="Force a specific workflow stage (auto-inferred from goal if omitted)",
    )
    p_run.add_argument(
        "--full",
        action="store_true",
        help="Run the full workflow from REQUIREMENTS to DONE",
    )
    p_run.add_argument(
        "--no-auto-transition",
        action="store_true",
        help="Disable auto-advance between stages",
    )
    p_run.set_defaults(func=cmd_run)

    p_validate = sub.add_parser("validate", help="Validate DBK configuration and environment")
    p_validate.set_defaults(func=cmd_validate)

    p_config = sub.add_parser("config", help="Config management")
    config_sub = p_config.add_subparsers(dest="config_cmd", required=True)

    p_config_show = config_sub.add_parser("show", help="Print resolved TOML configuration")
    p_config_show.set_defaults(func=cmd_config_show)

    p_config_init = config_sub.add_parser("init", help="Create a config file from the default template")
    p_config_init.add_argument("--path", help="Target path (default: ~/.config/dbk/config.toml)")
    p_config_init.add_argument("--force", action="store_true", help="Overwrite existing config")
    p_config_init.set_defaults(func=cmd_config_init)

    p_config_get = config_sub.add_parser("get", help="Print the resolved value of a config key")
    p_config_get.add_argument("key", help="Config key (dot-notation, e.g. agent.provider)")
    p_config_get.set_defaults(func=cmd_config_get)

    p_config_set = config_sub.add_parser("set", help="Set a config key in the user config file")
    p_config_set.add_argument("key", help="Config key (dot-notation, e.g. agent.provider)")
    p_config_set.add_argument("value", nargs="?", default=None, help="Value to set (omit to delete the key)")
    p_config_set.add_argument("--path", help="Target config path (default: ~/.config/dbk/config.toml)")
    p_config_set.set_defaults(func=cmd_config_set)

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
    # Range query options:
    p_metrics.add_argument("--from", dest="metric_from", help="Start timestamp (ISO 8601)")
    p_metrics.add_argument("--to", dest="metric_to", help="End timestamp (ISO 8601)")
    p_metrics.add_argument("--limit-range", dest="metric_limit", type=int, default=1000, help="Max rows for range query")
    p_metrics.add_argument("--aggregate", action="store_true", help="Aggregate range query results (avg/max/min)")
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
    p_runtime_cleanup.add_argument(
        "--safety-floor-hours",
        type=float,
        dest="safety_floor_hours",
        help="Minimum retention hours (safety floor). Cleanup is refused if --older-than-hours is below this. Default: 24.0",
    )
    p_runtime_cleanup.add_argument(
        "--max-delete-per-run",
        type=int,
        dest="max_delete_per_run",
        help="Maximum rows deleted per cleanup run. Default: 100000. Set to 0 for unlimited.",
    )
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
    p_runtime_cleanup_daemon_start.add_argument(
        "--safety-floor-hours",
        type=float,
        dest="safety_floor_hours",
        help="Minimum retention hours. Default: 24.0",
    )
    p_runtime_cleanup_daemon_start.add_argument(
        "--max-delete-per-run",
        type=int,
        dest="max_delete_per_run",
        help="Max rows deleted per run. Default: 100000.",
    )
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
    p_runtime_cleanup_daemon_run.add_argument("--safety-floor-hours", type=float, dest="safety_floor_hours")
    p_runtime_cleanup_daemon_run.add_argument("--max-delete-per-run", type=int, dest="max_delete_per_run")
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

    p_agent = sub.add_parser("agent", help="AI Agent - LLM-powered observability assistant")
    agent_sub = p_agent.add_subparsers(dest="agent_cmd", required=False)

    p_agent_interactive = agent_sub.add_parser("interactive", help="Start interactive REPL")
    p_agent_interactive.add_argument("--session")
    p_agent_interactive.add_argument("--model")
    p_agent_interactive.add_argument("--provider", choices=["openai", "anthropic", "mock"])
    p_agent_interactive.add_argument("--no-stream", action="store_true")
    p_agent_interactive.set_defaults(func=lambda a: _get_agent_main()(["--interactive", "--session", a.session or "", "--model", a.model or "", "--provider", a.provider or "", "--no-stream" if a.no_stream else ""]))

    p_agent_info = agent_sub.add_parser("info", help="Show agent configuration")
    p_agent_info.set_defaults(func=lambda _a: _get_agent_main()(["--info"]))

    def _do_sessions(args: argparse.Namespace) -> int:
        subcmd = "session-clear" if args.clear else "session-list"
        extra: list[str] = []
        if args.clear:
            if args.session:
                extra += ["--session", args.session]
            if args.all:
                extra += ["--all"]
        return cast(int, _get_agent_main()([subcmd] + extra))

    p_agent_session_list = agent_sub.add_parser("sessions", help="List/clear agent sessions")
    p_agent_session_list.add_argument("--clear", action="store_true", help="Clear sessions")
    p_agent_session_list.add_argument("--session", help="Session ID to delete")
    p_agent_session_list.add_argument("--all", action="store_true", help="Delete all sessions")
    p_agent_session_list.set_defaults(func=_do_sessions)

    p_agent_tools = agent_sub.add_parser("tools", help="Show registered agent tools")
    p_agent_tools.set_defaults(func=lambda _a: _get_agent_main()(["tools-list"]))

    p_agent_workflow = agent_sub.add_parser("workflow", help="Manage workflow stage")
    p_agent_workflow.add_argument("--session", required=True)
    p_agent_workflow.add_argument("--stage", choices=[s.value for s in WorkflowStage])
    p_agent_workflow.set_defaults(func=lambda a: _get_agent_main()(["workflow-advance", "--session", a.session, "--stage", a.stage or ""]))

    # ------------------------------------------------------------------
    # Alert subcommand.
    # ------------------------------------------------------------------
    p_alert = sub.add_parser("alert", help="Alerting: evaluate rules and notify")
    alert_sub = p_alert.add_subparsers(dest="alert_cmd", required=True)

    # --- alert rules ---
    p_alert_rules = alert_sub.add_parser("rules", help="Manage alert rules")
    alert_rules_sub = p_alert_rules.add_subparsers(dest="rules_cmd", required=True)

    p_alert_rules_list = alert_rules_sub.add_parser("list", help="List alert rules")
    p_alert_rules_list.add_argument("--rules-path")
    p_alert_rules_list.add_argument("--format", default="json", choices=["json", "text"])
    p_alert_rules_list.set_defaults(func=cmd_alert_rules_list)

    p_alert_rules_validate = alert_rules_sub.add_parser("validate", help="Validate a rules file")
    p_alert_rules_validate.add_argument("--rules-path", required=True)
    p_alert_rules_validate.set_defaults(func=cmd_alert_rules_validate)

    p_alert_rules_eval = alert_rules_sub.add_parser("eval", help="Evaluate rules against live metrics")
    p_alert_rules_eval.add_argument("--rules-path", required=True)
    p_alert_rules_eval.add_argument("--instance")
    p_alert_rules_eval.add_argument("--webhook-url")
    p_alert_rules_eval.add_argument("--webhook-secret")
    p_alert_rules_eval.set_defaults(func=cmd_alert_rules_eval)

    p_alert_rules_add = alert_rules_sub.add_parser("add", help="Add a new alert rule")
    p_alert_rules_add.add_argument("--name", required=True)
    p_alert_rules_add.add_argument("--metric", required=True)
    p_alert_rules_add.add_argument("--operator", required=True, choices=["gt", "lt", "gte", "lte", "eq"])
    p_alert_rules_add.add_argument("--threshold", type=float, required=True)
    p_alert_rules_add.add_argument("--severity", required=True, choices=["info", "warning", "critical"])
    p_alert_rules_add.add_argument("--description", default="")
    p_alert_rules_add.add_argument("--instance")
    p_alert_rules_add.add_argument("--min-duration", type=int, default=0)
    p_alert_rules_add.add_argument("--cooldown", type=int, default=300)
    p_alert_rules_add.set_defaults(func=cmd_alert_rules_add)

    p_alert_rules_export = alert_rules_sub.add_parser("export", help="Export rules to a JSON file")
    p_alert_rules_export.add_argument("--path", required=True, help="Output file path")
    p_alert_rules_export.add_argument("--include-builtin", action="store_true", help="Include built-in rules")
    p_alert_rules_export.set_defaults(func=cmd_alert_rules_export)

    # --- alert daemon ---
    p_alert_daemon = alert_sub.add_parser("daemon", help="Manage alert daemon")
    alert_daemon_sub = p_alert_daemon.add_subparsers(dest="alert_daemon_cmd", required=True)

    p_alert_daemon_start = alert_daemon_sub.add_parser("start", help="Start alert daemon")
    p_alert_daemon_start.add_argument("--interval-sec", type=int, default=60)
    p_alert_daemon_start.add_argument("--rules-path")
    p_alert_daemon_start.add_argument("--webhook-url")
    p_alert_daemon_start.add_argument("--webhook-secret")
    p_alert_daemon_start.add_argument("--prometheus-host", default="127.0.0.1")
    p_alert_daemon_start.add_argument("--prometheus-port", type=int, default=9090)
    p_alert_daemon_start.set_defaults(func=cmd_alert_daemon_start)

    p_alert_daemon_stop = alert_daemon_sub.add_parser("stop", help="Stop alert daemon")
    p_alert_daemon_stop.set_defaults(func=cmd_alert_daemon_stop)

    p_alert_daemon_status = alert_daemon_sub.add_parser("status", help="Show alert daemon status")
    p_alert_daemon_status.set_defaults(func=cmd_alert_daemon_status)

    p_alert_daemon_run = alert_daemon_sub.add_parser("run", help=argparse.SUPPRESS)
    p_alert_daemon_run.add_argument("--interval-sec", type=int, default=60)
    p_alert_daemon_run.add_argument("--rules-path")
    p_alert_daemon_run.add_argument("--webhook-url")
    p_alert_daemon_run.add_argument("--webhook-secret")
    p_alert_daemon_run.add_argument("--prometheus-host", default="127.0.0.1")
    p_alert_daemon_run.add_argument("--prometheus-port", type=int, default=9090)
    p_alert_daemon_run.add_argument("--state-path", help=argparse.SUPPRESS)
    p_alert_daemon_run.add_argument(
        "--enable-agent",
        action="store_true",
        help="Enable AI Agent diagnostic sessions when alerts fire (AIOps闭环)",
    )
    p_alert_daemon_run.set_defaults(func=cmd_alert_daemon_run)

    # --- alert history ---
    p_alert_history = alert_sub.add_parser("history", help="Query alert history")
    p_alert_history.add_argument("--rule-name")
    p_alert_history.add_argument("--instance")
    p_alert_history.add_argument("--state", choices=["firing", "resolved"])
    p_alert_history.add_argument("--since-hours", type=float)
    p_alert_history.add_argument("--limit", type=int, default=50)
    p_alert_history.set_defaults(func=cmd_alert_history)

    # --- alert prometheus ---
    p_alert_prom = alert_sub.add_parser("prometheus", help="Start Prometheus exporter and query metrics")
    p_alert_prom.add_argument("--listen-host", default="127.0.0.1")
    p_alert_prom.add_argument("--listen-port", type=int, default=9090)
    p_alert_prom.add_argument("--once", action="store_true", help="Print current metrics and exit")
    p_alert_prom.set_defaults(func=cmd_alert_prometheus)

    # ------------------------------------------------------------------
    # api-server subcommand.
    # ------------------------------------------------------------------
    p_api_server = sub.add_parser("api-server", help="Start the DBK Agent REST API server")
    p_api_server.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_api_server.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    p_api_server.add_argument("--workers", type=int, default=1, help="Number of worker processes (default: 1)")
    p_api_server.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Uvicorn log level (default: info)",
    )
    p_api_server.set_defaults(func=cmd_api_server)

    # ------------------------------------------------------------------
    # tui subcommand (Textual TUI for the agent).
    # ------------------------------------------------------------------
    p_tui = sub.add_parser("tui", help="Start the DBK Agent Textual TUI")
    p_tui.add_argument("--session", dest="session_id", metavar="ID",
                       help="Resume an existing session by ID")
    p_tui.add_argument("--provider", choices=["openai", "anthropic", "mock"],
                       default="mock", help="LLM provider (default: mock)")
    p_tui.add_argument("--model", dest="model", metavar="NAME",
                       help="Model name (provider-specific)")
    p_tui.add_argument("--no-stream", action="store_true",
                       help="Disable streaming token display")
    p_tui.set_defaults(func=lambda a: _run_tui(a))

    return parser


def _run_tui(args: argparse.Namespace) -> int:
    from dbk.cli_tui import main as tui_main
    argv = []
    if getattr(args, "session_id", None):
        argv += ["--session", args.session_id]
    if getattr(args, "provider", "mock") != "mock":
        argv += ["--provider", args.provider]
    if getattr(args, "model", None):
        argv += ["--model", args.model]
    if getattr(args, "no_stream", False):
        argv += ["--no-stream"]
    return tui_main(argv)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return cast(int, args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
