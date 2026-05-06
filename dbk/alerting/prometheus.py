"""Prometheus metrics exporter for DBK alerts.

Exposes an HTTP endpoint that serves alert metrics in Prometheus text format.
"""

from __future__ import annotations

import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from dbk.alerting.models import Alert, AlertState, Severity

# Metric name regex per Prometheus spec.
_VALID_METRIC_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


def _sanitize(name: str) -> str:
    """Replace invalid Prometheus metric name characters with underscores."""
    if _VALID_METRIC_RE.match(name):
        return name
    return re.sub(r"[^a-zA-Z0-9_:]", "_", name)


def _escape_label_value(value: str) -> str:
    """Escape a label value per Prometheus conventions."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class AlertPrometheusMetrics:
    """In-memory metrics registry for alert Prometheus export."""

    def __init__(self) -> None:
        self._gauge_values: dict[tuple[str, frozenset[tuple[str, str]]], float] = {}
        self._gauge_labels: dict[tuple[str, frozenset[tuple[str, str]]], dict[str, str]] = {}
        self._gauge_descriptions: dict[str, str] = {}

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
        description: str = "",
    ) -> None:
        labels = labels or {}
        mname = _sanitize(name)
        key = (mname, frozenset(sorted(labels.items())))
        self._gauge_values[key] = value
        self._gauge_labels[key] = labels
        if description:
            self._gauge_descriptions[mname] = description

    def export(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        lines: list[str] = []
        seen: set[str] = set()

        for (mname, label_set), value in sorted(self._gauge_values.items()):
            if mname not in seen:
                seen.add(mname)
                desc = self._gauge_descriptions.get(mname, "")
                lines.append(f"# HELP {mname} {desc}")
                lines.append(f"# TYPE {mname} gauge")

            labels = self._gauge_labels[(mname, label_set)]
            if labels:
                label_str = ", ".join(
                    f'{k}="{_escape_label_value(v)}"' for k, v in sorted(labels.items())
                )
                lines.append(f"{mname}{{{label_str}}} {value}")
            else:
                lines.append(f"{mname} {value}")

        return "\n".join(lines) + "\n"


class AlertPrometheusExporter:
    """Syncs alert state to Prometheus metrics and optionally serves them over HTTP."""

    def __init__(
        self,
        listen_host: str = "127.0.0.1",
        listen_port: int = 9090,
        prefix: str = "dbk_alert",
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.prefix = prefix
        self._metrics = AlertPrometheusMetrics()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Alert metric sync
    # ------------------------------------------------------------------

    def sync_alerts(self, alerts: list[Alert]) -> None:
        """Update the Prometheus gauge metrics to reflect the current alert list."""
        with self._lock:
            # Reset gauges
            self._metrics._gauge_values.clear()
            self._metrics._gauge_labels.clear()

            name = f"{self.prefix}_firing"
            self._metrics._gauge_descriptions[name] = (
                "1 if the alert is currently firing, 0 if resolved"
            )
            severity_name = f"{self.prefix}_severity_level"
            self._metrics._gauge_descriptions[severity_name] = (
                "Numeric severity level (0=info, 1=warning, 2=critical)"
            )

            for alert in alerts:
                labels: dict[str, str] = {
                    "alert_name": alert.rule_name,
                    "severity": alert.severity.value,
                    "instance": alert.instance,
                    "metric": alert.metric,
                    "state": alert.state.value,
                }
                labels.update(alert.labels)

                val = 1.0 if alert.state == AlertState.FIRING else 0.0
                self._metrics.set_gauge(name, val, labels)
                self._metrics.set_gauge(severity_name, float(int(alert.severity)), labels)

    def sync_summary(self, *, firing: int, warning: int, critical: int, info: int) -> None:
        """Sync summary counters."""
        with self._lock:
            total_name = f"{self.prefix}_total"
            self._metrics._gauge_descriptions[total_name] = (
                "Total number of currently firing alerts by severity"
            )
            self._metrics.set_gauge(total_name, float(firing), {"severity": "firing"})
            self._metrics.set_gauge(total_name, float(warning), {"severity": "warning"})
            self._metrics.set_gauge(total_name, float(critical), {"severity": "critical"})
            self._metrics.set_gauge(total_name, float(info), {"severity": "info"})

    # ------------------------------------------------------------------
    # HTTP server
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the HTTP server in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            return

        exporter_ref: "AlertPrometheusExporter" = self  # noqa: F841

        class _Handler(BaseHTTPRequestHandler):
            _exporter: "AlertPrometheusExporter" | None = None

            def do_GET(self) -> None:
                if self.path not in ("/", "/metrics"):
                    self.send_error(404)
                    return
                if _Handler._exporter is None:
                    self.send_error(503)
                    return
                content = _Handler._exporter._metrics.export()
                payload = content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args: Any) -> None:
                pass  # Suppress default request logging to stderr

        _Handler._exporter = self

        def run_server() -> None:
            server = HTTPServer((self.listen_host, self.listen_port), _Handler)
            self._server = server
            server.serve_forever()

        self._thread = threading.Thread(target=run_server, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the HTTP server."""
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    @property
    def metrics_text(self) -> str:
        """Return the current metrics as Prometheus text (thread-safe)."""
        with self._lock:
            return self._metrics.export()
