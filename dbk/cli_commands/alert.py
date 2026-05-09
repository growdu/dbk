"""'dbk alert' command group."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from dbk.alerting import (
    AlertEngine, AlertEvent, AlertNotifier, AlertPrometheusExporter,
    AlertRule, AlertStore, DEFAULT_ALERT_RULES, LogNotifier,
    WebhookNotifier,
)
from dbk.alerting.daemon import (
    alert_daemon_status, run_alert_loop, start_alert_daemon,
    stop_alert_daemon,
)
from dbk.alerting.engine import load_rules as load_alert_rules
from dbk.alerting.models import AlertState, Severity
from dbk.config import dbk_root, load_config
from dbk.storage import RuntimeStore
from dbk.config import runtime_db_path

from dbk.cli_commands.base import CommandGroup, CommandResult


def _store() -> RuntimeStore:
    s = RuntimeStore(runtime_db_path())
    s.init_schema()
    return s


def _collect_metrics_for_alerting(store: RuntimeStore, instance: str | None = None) -> list[dict[str, object]]:
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
                "SELECT ts, value, labels_json FROM runtime_metric WHERE metric = ? AND instance = ? ORDER BY ts DESC LIMIT 1",
                (metric, inst),
            )
            for lr in latest:
                metrics.append({
                    "metric": metric, "value": float(lr["value"]),
                    "instance": inst, "ts": str(lr["ts"]),
                })
    return metrics


def _fmt_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--format",
        default=argparse.SUPPRESS,
        choices=["text", "json", "json-lines"],
        help="Output format (default: text)",
    )


class AlertCommand(CommandGroup):
    """'dbk alert' group — rules, daemon, history, prometheus."""

    name = "alert"
    subcommand = "alert"
    subcommand_help = "Alerting system management"

    def register_subcommands(self, sub):
        # alert rules
        rules = sub.add_parser("rules", help="Manage alert rules")
        r = rules.add_subparsers(dest="alert_rules_cmd", required=True)

        pp = r.add_parser("list", help="List alert rules")
        pp.add_argument("--rules-path")
        _fmt_arg(pp)
        pp.set_defaults(func=self._cmd_rules_list)

        pp = r.add_parser("validate", help="Validate a rules file")
        pp.add_argument("rules_path")
        _fmt_arg(pp)
        pp.set_defaults(func=self._cmd_rules_validate)

        pp = r.add_parser("add", help="Add a new rule (print JSON)")
        pp.add_argument("--name", required=True)
        pp.add_argument("--metric", required=True)
        pp.add_argument("--operator", required=True)
        pp.add_argument("--threshold", type=float, required=True)
        pp.add_argument("--severity", required=True, choices=["info", "warning", "critical"])
        pp.add_argument("--description")
        pp.add_argument("--instance")
        pp.add_argument("--min-duration", type=float, dest="min_duration")
        pp.add_argument("--cooldown", type=float)
        _fmt_arg(pp)
        pp.set_defaults(func=self._cmd_rules_add)

        pp = r.add_parser("export", help="Export rules to a file")
        pp.add_argument("--path", required=True)
        pp.add_argument("--include-builtin", action="store_true")
        _fmt_arg(pp)
        pp.set_defaults(func=self._cmd_rules_export)

        pp = r.add_parser("eval", help="Evaluate rules against current metrics")
        pp.add_argument("--rules-path")
        pp.add_argument("--instance")
        pp.add_argument("--webhook-url")
        pp.add_argument("--webhook-secret")
        _fmt_arg(pp)
        pp.set_defaults(func=self._cmd_rules_eval)

        # alert daemon
        daemon = sub.add_parser("daemon", help="Manage alert daemon")
        d = daemon.add_subparsers(dest="alert_daemon_cmd", required=True)

        ps = d.add_parser("start", help="Start alert daemon")
        ps.add_argument("--interval-sec", type=int, default=60)
        ps.add_argument("--rules-path")
        ps.add_argument("--webhook-url")
        ps.add_argument("--webhook-secret")
        ps.add_argument("--prometheus-host", default="127.0.0.1")
        ps.add_argument("--prometheus-port", type=int, default=9090)
        _fmt_arg(ps)
        ps.set_defaults(func=self._cmd_daemon_start)

        ps = d.add_parser("stop", help="Stop alert daemon")
        _fmt_arg(ps)
        ps.set_defaults(func=self._cmd_daemon_stop)

        ps = d.add_parser("status", help="Show alert daemon status")
        _fmt_arg(ps)
        ps.set_defaults(func=self._cmd_daemon_status)

        ps = d.add_parser("run", help="Run alert daemon in foreground")
        ps.add_argument("--interval-sec", type=int, default=60)
        ps.add_argument("--rules-path")
        ps.add_argument("--webhook-url")
        ps.add_argument("--webhook-secret")
        ps.add_argument("--prometheus-host", default="127.0.0.1")
        ps.add_argument("--prometheus-port", type=int, default=9090)
        ps.add_argument("--state-path")
        ps.add_argument("--enable-agent", action="store_true")
        _fmt_arg(ps)
        ps.set_defaults(func=self._cmd_daemon_run)

        # alert history
        p = sub.add_parser("history", help="Query alert history")
        p.add_argument("--rule-name")
        p.add_argument("--instance")
        p.add_argument("--state", choices=["firing", "resolved"])
        p.add_argument("--since-hours", type=float)
        p.add_argument("--limit", type=int, default=50)
        _fmt_arg(p)
        p.set_defaults(func=self._cmd_history)

        # alert prometheus
        p = sub.add_parser("prometheus", help="Start Prometheus exporter")
        p.add_argument("--listen-host", default="127.0.0.1")
        p.add_argument("--listen-port", type=int, default=9090)
        p.add_argument("--once", action="store_true")
        _fmt_arg(p)
        p.set_defaults(func=self._cmd_prometheus)

    # --- rules subcommands ---

    def _cmd_rules_list(self, args) -> CommandResult:
        try:
            rules = load_alert_rules(Path(args.rules_path)) if args.rules_path else list(DEFAULT_ALERT_RULES)
        except (FileNotFoundError, ValueError) as exc:
            return CommandResult.config_error(f"Error loading rules: {exc}")
        if args.format == "text":
            if not rules:
                return CommandResult.ok(message="No rules loaded.")
            lines = []
            for r in rules:
                lines.append(f"  {r.name}: {r.metric} {r.operator} {r.threshold} [{r.severity.value}]")
                if r.instance:
                    lines.append(f"    instance={r.instance}")
                lines.append(f"    {r.description}")
            return CommandResult.ok(message="\n".join(lines), data={"count": len(rules)})
        return CommandResult.ok(data={"rules": [r.to_dict() for r in rules], "count": len(rules)})

    def _cmd_rules_validate(self, args) -> CommandResult:
        try:
            rules = load_alert_rules(Path(args.rules_path))
            return CommandResult.ok(data={"valid": True, "count": len(rules), "rules": [r.to_dict() for r in rules]})
        except (FileNotFoundError, ValueError) as exc:
            return CommandResult.config_error(f"Error loading rules: {exc}")

    def _cmd_rules_add(self, args) -> CommandResult:
        rule = AlertRule(
            name=args.name, metric=args.metric, operator=args.operator,
            threshold=args.threshold, severity=Severity(args.severity),
            description=args.description or "",
            instance=args.instance,
            minimum_duration_sec=getattr(args, "min_duration", None),
            cooldown_sec=getattr(args, "cooldown", None),
        )
        return CommandResult.ok(data={"added": rule.to_dict()})

    def _cmd_rules_export(self, args) -> CommandResult:
        rules = list(DEFAULT_ALERT_RULES)
        path = Path(args.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"rules": [r.to_dict() for r in rules]}, indent=2, ensure_ascii=True))
        return CommandResult.ok(message=f"Exported {len(rules)} rules to {path}", data={"path": str(path), "count": len(rules)})

    def _cmd_rules_eval(self, args) -> CommandResult:
        try:
            rules = load_alert_rules(Path(args.rules_path)) if args.rules_path else list(DEFAULT_ALERT_RULES)
        except (FileNotFoundError, ValueError) as exc:
            return CommandResult.config_error(f"Error loading rules: {exc}")
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
        return CommandResult.ok(data={
            "events": [{"type": e.type, "alert": e.alert.to_dict()} for e in events],
            "firing": [a.to_dict() for a in firing],
            "summary": {
                "total_evaluated": len(rules),
                "firing_count": engine.get_active_count(),
                "by_severity": {k.value: v for k, v in counts.items()},
            },
        })

    # --- daemon subcommands ---

    def _cmd_daemon_start(self, args) -> CommandResult:
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
            return CommandResult.runtime_error(str(exc))
        return CommandResult.ok(data={"started": True, **state.to_dict()})

    def _cmd_daemon_stop(self, args) -> CommandResult:
        payload = stop_alert_daemon(cwd=Path.cwd())
        return CommandResult.ok(data=payload)

    def _cmd_daemon_status(self, args) -> CommandResult:
        payload = alert_daemon_status(cwd=Path.cwd())
        return CommandResult.ok(data=payload)

    def _cmd_daemon_run(self, args) -> CommandResult:
        agent = None
        if args.enable_agent:
            from dbk.providers import get_provider
            from dbk.agent.core import Agent
            agent = Agent(provider=get_provider())
        run_alert_loop(
            interval_sec=args.interval_sec,
            rules_path=Path(args.rules_path) if args.rules_path else None,
            webhook_url=args.webhook_url,
            webhook_secret=args.webhook_secret,
            prometheus_host=args.prometheus_host,
            prometheus_port=args.prometheus_port,
            state_path=Path(args.state_path) if getattr(args, "state_path", None) else None,
            cwd=Path.cwd(),
            agent=agent,
        )
        return CommandResult.ok(message="Alert daemon stopped.")

    # --- alert history ---

    def _cmd_history(self, args) -> CommandResult:
        store = AlertStore(dbk_root() / "alerts.sqlite")
        store.init_schema()
        state = AlertState(args.state) if args.state else None
        alerts = store.query_alerts(
            rule_name=args.rule_name, instance=args.instance,
            state=state, since_hours=args.since_hours, limit=args.limit,
        )
        return CommandResult.ok(data={"alerts": [a.to_dict() for a in alerts], "count": len(alerts)})

    # --- alert prometheus ---

    def _cmd_prometheus(self, args) -> CommandResult:
        exporter = AlertPrometheusExporter(listen_host=args.listen_host, listen_port=args.listen_port)
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
            return CommandResult.ok(data={"metrics": exporter.metrics_text})
        exporter.start()
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            exporter.stop()
        return CommandResult.ok(message="Prometheus exporter stopped.")
