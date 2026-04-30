"""Alert daemon: background process that evaluates rules and dispatches notifications."""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from dbk.alerting.engine import AlertEngine
from dbk.alerting.models import AlertEvent, AlertRule
from dbk.alerting.notifiers import AlertNotifier, CompositeNotifier, LogNotifier, WebhookNotifier
from dbk.alerting.prometheus import AlertPrometheusExporter
from dbk.alerting.store import AlertStore
from dbk.config import dbk_root
from dbk.models import utc_now_iso
from dbk.storage import RuntimeStore
from dbk.config import runtime_db_path


def alert_daemon_dir(cwd: Path | None = None) -> Path:
    return dbk_root(cwd) / "alert-daemon"


def alert_daemon_state_path(cwd: Path | None = None) -> Path:
    return alert_daemon_dir(cwd) / "state.json"


def alert_daemon_log_path(cwd: Path | None = None) -> Path:
    return alert_daemon_dir(cwd) / "alert-daemon.log"


@dataclass(slots=True)
class AlertDaemonState:
    pid: int
    started_at: str
    interval_sec: int
    rules_loaded: int
    total_evaluations: int
    total_firings: int
    total_resolutions: int
    last_evaluation_at: str | None = None
    last_error: str | None = None
    log_path: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "interval_sec": self.interval_sec,
            "rules_loaded": self.rules_loaded,
            "total_evaluations": self.total_evaluations,
            "total_firings": self.total_firings,
            "total_resolutions": self.total_resolutions,
            "last_evaluation_at": self.last_evaluation_at,
            "last_error": self.last_error,
            "log_path": self.log_path,
        }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def read_state(path: Path | None = None) -> dict[str, object] | None:
    state_path = path or alert_daemon_state_path()
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def start_alert_daemon(
    *,
    interval_sec: int = 60,
    rules_path: Path | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    prometheus_host: str = "127.0.0.1",
    prometheus_port: int = 9090,
    cwd: Path | None = None,
) -> AlertDaemonState:
    if interval_sec <= 0:
        raise ValueError("interval_sec must be > 0")

    from dbk.cli import main as _cli_main
    import sys

    state_path = alert_daemon_state_path(cwd)
    log_path = alert_daemon_log_path(cwd)
    existing = read_state(state_path)
    if existing and is_pid_running(int(existing["pid"])):
        raise RuntimeError(
            f"alert daemon already running with pid={existing['pid']}"
        )

    cmd = [
        sys.executable,
        "-m",
        "dbk.cli",
        "alert",
        "daemon",
        "run",
        "--interval-sec",
        str(interval_sec),
        "--state-path",
        str(state_path),
    ]
    if rules_path:
        cmd.extend(["--rules-path", str(rules_path)])
    if webhook_url:
        cmd.extend(["--webhook-url", webhook_url])
    if webhook_secret:
        cmd.extend(["--webhook-secret", webhook_secret])
    if prometheus_host:
        cmd.extend(["--prometheus-host", prometheus_host])
    cmd.extend(["--prometheus-port", str(prometheus_port)])

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as logf:
        import subprocess
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(cwd or Path.cwd()),
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )

    state = AlertDaemonState(
        pid=proc.pid,
        started_at=utc_now_iso(),
        interval_sec=interval_sec,
        rules_loaded=0,
        total_evaluations=0,
        total_firings=0,
        total_resolutions=0,
        log_path=str(log_path),
    )
    _write_json(state_path, state.to_dict())
    return state


def stop_alert_daemon(
    *,
    cwd: Path | None = None,
    timeout_sec: float = 5.0,
    graceful_timeout_sec: float = 3.0,
) -> dict[str, object]:
    state_path = alert_daemon_state_path(cwd)
    state = read_state(state_path)
    if state is None:
        return {"stopped": False, "reason": "not_running"}

    pid = int(state["pid"])
    if not is_pid_running(pid):
        state_path.unlink(missing_ok=True)
        return {"stopped": True, "pid": pid, "signal": "none"}

    graceful_deadline = time.time() + graceful_timeout_sec
    term_sent = False
    try:
        os.kill(pid, signal.SIGTERM)
        term_sent = True
    except PermissionError:
        return {"stopped": False, "pid": pid, "reason": "permission_denied_sigterm"}
    except ProcessLookupError:
        state_path.unlink(missing_ok=True)
        return {"stopped": True, "pid": pid, "signal": "none"}

    while time.time() < graceful_deadline:
        if not is_pid_running(pid):
            state_path.unlink(missing_ok=True)
            return {"stopped": True, "pid": pid, "signal": "SIGTERM"}
        time.sleep(0.1)

    if term_sent:
        try:
            os.kill(pid, signal.SIGKILL)
        except PermissionError:
            return {"stopped": False, "pid": pid, "reason": "permission_denied_sigkill"}
        except ProcessLookupError:
            state_path.unlink(missing_ok=True)
            return {"stopped": True, "pid": pid, "signal": "none"}

    kill_deadline = time.time() + max(0, timeout_sec - graceful_timeout_sec)
    while time.time() < kill_deadline:
        if not is_pid_running(pid):
            state_path.unlink(missing_ok=True)
            return {"stopped": True, "pid": pid, "signal": "SIGKILL"}
        time.sleep(0.1)
    return {"stopped": False, "pid": pid, "reason": "survived_sigkill"}


def alert_daemon_status(*, cwd: Path | None = None) -> dict[str, object]:
    state_path = alert_daemon_state_path(cwd)
    state = read_state(state_path)
    if state is None:
        return {"running": False}
    running = is_pid_running(int(state["pid"]))
    payload = dict(state)
    payload["running"] = running
    payload["state_path"] = str(state_path)
    return payload


def run_alert_loop(
    *,
    interval_sec: int = 60,
    rules_path: Path | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    prometheus_host: str = "127.0.0.1",
    prometheus_port: int = 9090,
    state_path: Path | None = None,
    cwd: Path | None = None,
) -> int:
    """Main loop for the alert daemon."""
    from dbk.alerting.engine import load_rules

    target_state_path = state_path or alert_daemon_state_path(cwd)
    target_state = read_state(target_state_path) or {}
    state = AlertDaemonState(
        pid=os.getpid(),
        started_at=str(target_state.get("started_at", utc_now_iso())),
        interval_sec=int(target_state.get("interval_sec", interval_sec)),
        rules_loaded=0,
        total_evaluations=int(target_state.get("total_evaluations", 0)),
        total_firings=int(target_state.get("total_firings", 0)),
        total_resolutions=int(target_state.get("total_resolutions", 0)),
        log_path=str(target_state.get("log_path", alert_daemon_log_path(cwd))),
        last_evaluation_at=target_state.get("last_evaluation_at"),
        last_error=target_state.get("last_error"),
    )

    # Load rules
    rules: list[AlertRule] = []
    if rules_path:
        try:
            rules = load_rules(rules_path)
        except (FileNotFoundError, ValueError) as exc:
            print(f"[alert-daemon] Failed to load rules: {exc}", flush=True)

    engine = AlertEngine(rules=rules)
    rt_store = RuntimeStore(runtime_db_path(cwd))
    rt_store.init_schema()
    store = AlertStore(dbk_root(cwd) / "alerts.sqlite")
    store.init_schema()
    notifiers: CompositeNotifier = CompositeNotifier()
    notifiers.add(LogNotifier())
    if webhook_url:
        notifiers.add(WebhookNotifier(url=webhook_url, secret=webhook_secret))
    prom_exporter: AlertPrometheusExporter | None = None
    if prometheus_host or prometheus_port:
        prom_exporter = AlertPrometheusExporter(
            listen_host=prometheus_host or "127.0.0.1",
            listen_port=prometheus_port or 9090,
        )
        prom_exporter.start()

    # Wire engine -> store -> notifiers chain
    def on_event(event: AlertEvent) -> None:
        store.insert_event(event)
        try:
            notifiers.send(event)
        except Exception as exc:
            print(f"[alert-daemon] Notifier error: {exc}", flush=True)

    engine.add_listener(on_event)

    stop_flag = {"stop": False}

    def handle_signal(_sig: int, _frame: object) -> None:
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    if rules:
        state.rules_loaded = len(rules)

    while not stop_flag["stop"]:
        state.last_evaluation_at = utc_now_iso()
        try:
            # Collect latest metrics from the runtime store
            metrics = _collect_metrics_for_alerting(rt_store)
            events = engine.evaluate_batch(metrics)
            state.total_evaluations += 1
            for ev in events:
                if ev.type == "firing":
                    state.total_firings += 1
                    store.insert_alert(ev.alert)
                elif ev.type == "resolved":
                    state.total_resolutions += 1
                    store.update_alert_state(
                        ev.alert.id,
                        ev.alert.state,
                        resolved_at=ev.alert.resolved_at,
                    )

            # Sync Prometheus
            if prom_exporter:
                firing = engine.get_firing_alerts()
                prom_exporter.sync_alerts(firing)
                counts = engine.get_firing_count_by_severity()
                prom_exporter.sync_summary(
                    firing=engine.get_active_count(),
                    warning=counts.get("warning", 0),
                    critical=counts.get("critical", 0),
                    info=counts.get("info", 0),
                )

            state.last_error = None
        except Exception as exc:
            state.last_error = str(exc)
            print(f"[alert-daemon] Evaluation error: {exc}", flush=True)

        _write_json(target_state_path, state.to_dict())

        for _ in range(state.interval_sec):
            if stop_flag["stop"]:
                break
            time.sleep(1)

    notifiers.close()
    if prom_exporter:
        prom_exporter.stop()
    return 0


def _collect_metrics_for_alerting(
    store: RuntimeStore,
    max_age_minutes: int = 5,
    limit_per_metric: int = 5,
) -> list[dict[str, object]]:
    """Collect the latest metric values from the runtime store for alerting.

    Returns a list of dicts with keys: metric, value, instance, ts.
    """
    now = datetime.now(tz=timezone.utc)
    cutoff = (now - timedelta(minutes=max_age_minutes)).isoformat()
    metrics: list[dict[str, object]] = []
    # Get distinct metric names from the last window
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT metric, instance
            FROM runtime_metric
            WHERE ts >= ?
            """,
            (cutoff,),
        )
        for row in rows:
            metric = str(row["metric"])
            instance = str(row["instance"])
            latest = conn.execute(
                """
                SELECT ts, value, labels_json
                FROM runtime_metric
                WHERE metric = ? AND instance = ?
                ORDER BY ts DESC LIMIT ?
                """,
                (metric, instance, limit_per_metric),
            )
            for lr in latest:
                ts = str(lr["ts"])
                if ts >= cutoff:
                    metrics.append({
                        "metric": metric,
                        "value": float(lr["value"]),
                        "instance": instance,
                        "ts": ts,
                    })
    return metrics
