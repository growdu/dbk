"""Tests for alert notifiers: LogNotifier, WebhookNotifier, CompositeNotifier."""

from __future__ import annotations

import http.server
import json
import logging
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dbk.alerting import (
    AlertEvent,
    AlertState,
    CompositeNotifier,
    LogNotifier,
    Severity,
    WebhookNotifier,
)
from dbk.alerting.models import Alert, AlertEvent as AlertEventModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event(
    rule_name: str = "test_rule",
    metric: str = "cpu_usage",
    value: float = 95.0,
    event_type: str = "firing",
    severity: Severity = Severity.WARNING,
) -> AlertEvent:
    alert = Alert(
        id="a1",
        rule_name=rule_name,
        metric=metric,
        operator="gt",
        threshold=80.0,
        severity=severity,
        state=AlertState.FIRING if event_type == "firing" else AlertState.RESOLVED,
        value=value,
        instance="pg-01",
        description="test alert",
        fired_at="2026-04-30T00:00:00Z",
        labels={},
        resolved_at="2026-04-30T00:05:00Z" if event_type == "resolved" else None,
    )
    return AlertEvent(type=event_type, alert=alert, fired_at="2026-04-30T00:00:00Z")


# ---------------------------------------------------------------------------
# LogNotifier
# ---------------------------------------------------------------------------

class TestLogNotifier:
    def test_send_firing_logs_at_firing_level(self, caplog: pytest.LogCaptureFixture) -> None:
        notifier = LogNotifier(level_firing=logging.ERROR, level_resolved=logging.INFO)
        with caplog.at_level(logging.DEBUG, "dbk.alerts"):
            notifier.send(make_event(event_type="firing", severity=Severity.CRITICAL))
        assert any("CRITICAL" in r.message and "FIRING" in r.message for r in caplog.records)
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_send_resolved_logs_at_resolved_level(self, caplog: pytest.LogCaptureFixture) -> None:
        notifier = LogNotifier(level_firing=logging.WARNING, level_resolved=logging.INFO)
        with caplog.at_level(logging.DEBUG, "dbk.alerts"):
            notifier.send(make_event(event_type="resolved"))
        assert any(r.levelno == logging.INFO for r in caplog.records)

    def test_send_batch(self, caplog: pytest.LogCaptureFixture) -> None:
        notifier = LogNotifier(level_firing=logging.WARNING, level_resolved=logging.INFO)
        events = [make_event(rule_name=f"rule_{i}") for i in range(3)]
        with caplog.at_level(logging.DEBUG, "dbk.alerts"):
            notifier.send_batch(events)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 3

    def test_format_includes_all_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        notifier = LogNotifier()
        ev = make_event(rule_name="slow_query", metric="query_latency_ms", value=5000.0)
        with caplog.at_level(logging.WARNING, "dbk.alerts"):
            notifier.send(ev)
        msg = caplog.records[-1].message
        assert "slow_query" in msg
        assert "query_latency_ms" in msg
        assert "5000" in msg
        assert "pg-01" in msg


# ---------------------------------------------------------------------------
# WebhookNotifier — real HTTP server
# ---------------------------------------------------------------------------

class _RequestCapture:
    """Simple HTTP server that captures the last request."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.shutdown = threading.Event()
        self.port = 0
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        import socket
        with socket.socket() as s:
            s.bind(("", 0))
            self.port = s.getsockname()[1]

        class _Handler(http.server.BaseHTTPRequestHandler):
            captured: list[dict[str, Any]] = self.received  # type: ignore[assignment]

            def do_POST(self) -> None:
                import json as _json
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                try:
                    if isinstance(body, bytes):
                        body = body.decode("utf-8")
                    payload = _json.loads(body)
                except Exception:
                    payload = {}
                self.captured.append({
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": payload,
                })
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, fmt: str, *args: object) -> None:
                pass  # suppress server logs

        self.server = http.server.HTTPServer(("127.0.0.1", self.port), _Handler)
        self.server.allow_reuse_address = True
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if hasattr(self, "server"):
            self.server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)


class TestWebhookNotifier:
    def test_send_success(self) -> None:
        server = _RequestCapture()
        port = server.start()
        try:
            notifier = WebhookNotifier(url=f"http://127.0.0.1:{port}/webhook")
            notifier.send(make_event())
            assert len(server.received) == 1
            req = server.received[0]
            assert req["body"]["event_type"] == "firing"
            assert "alert" in req["body"]
            assert req["headers"]["Content-Type"] == "application/json"
            assert "User-Agent" in req["headers"]
        finally:
            server.stop()

    def test_send_with_secret_adds_signature_header(self) -> None:
        server = _RequestCapture()
        port = server.start()
        try:
            notifier = WebhookNotifier(url=f"http://127.0.0.1:{port}/webhook", secret="mysecret")
            notifier.send(make_event())
            req = server.received[0]
            assert "X-Dbk-Signature" in req["headers"], (
                f"Missing X-Dbk-Signature header. Got: {list(req['headers'].keys())}"
            )
            sig = req["headers"]["X-Dbk-Signature"]
            assert sig.startswith("sha256=")
            # Verify HMAC correctness
            import hashlib
            import hmac
            import json as jsonmod
            body_bytes = jsonmod.dumps(req["body"], ensure_ascii=True).encode()
            expected = "sha256=" + hmac.new(b"mysecret", body_bytes, hashlib.sha256).hexdigest()
            assert sig == expected
        finally:
            server.stop()

    def test_send_batch(self) -> None:
        server = _RequestCapture()
        port = server.start()
        try:
            notifier = WebhookNotifier(url=f"http://127.0.0.1:{port}/webhook")
            notifier.send_batch([make_event(rule_name=f"r{i}") for i in range(3)])
            assert len(server.received) == 3
        finally:
            server.stop()

    def test_send_non_2xx_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Non-2xx responses should be logged as warnings."""
        received: list[dict[str, Any]] = []

        class _500Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                try:
                    body_str = body.decode("utf-8") if isinstance(body, bytes) else body
                    import json as _json
                    received.append(_json.loads(body_str))
                except Exception:
                    received.append({})
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"internal"}')

            def log_message(self, fmt: str, *args: object) -> None:
                pass  # suppress server noise

        # Reuse-address so consecutive runs don't get "address in use"
        with http.server.HTTPServer(("127.0.0.1", 0), _500Handler) as srv:
            srv.allow_reuse_address = True
            port = srv.server_address[1]
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            try:
                with caplog.at_level(logging.WARNING):
                    notifier = WebhookNotifier(
                        url=f"http://127.0.0.1:{port}/webhook", timeout_sec=3,
                    )
                    notifier.send(make_event())
                assert any(
                    "500" in r.message or "non-2xx" in r.message or "HTTP error" in r.message
                    for r in caplog.records
                ), f"Expected warning about non-2xx response in {caplog.records}"
                assert len(received) == 1, "Server should have received the request"
            finally:
                thread.join(timeout=2)

    def test_send_connection_refused_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            notifier = WebhookNotifier(url="http://127.0.0.1:59999/webhook", timeout_sec=1)
            notifier.send(make_event())
        assert any("URL error" in r.message or "Connection" in r.message for r in caplog.records)

    def test_custom_headers(self) -> None:
        server = _RequestCapture()
        port = server.start()
        try:
            notifier = WebhookNotifier(
                url=f"http://127.0.0.1:{port}/webhook",
                headers={"X-Custom": "header-value", "Authorization": "Bearer token123"},
            )
            notifier.send(make_event())
            req = server.received[0]
            assert req["headers"]["X-Custom"] == "header-value"
            assert req["headers"]["Authorization"] == "Bearer token123"
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# CompositeNotifier
# ---------------------------------------------------------------------------

class TestCompositeNotifier:
    def test_add_and_remove(self) -> None:
        cn = CompositeNotifier()
        n1 = LogNotifier()
        n2 = LogNotifier()
        cn.add(n1)
        cn.add(n2)
        assert len(cn._notifiers) == 2
        cn.remove(n1)
        assert len(cn._notifiers) == 1
        assert n2 in cn._notifiers

    def test_send_dispatches_to_all(self, caplog: pytest.LogCaptureFixture) -> None:
        cn = CompositeNotifier()
        cn.add(LogNotifier(logger_name="t1"))
        cn.add(LogNotifier(logger_name="t2"))
        with caplog.at_level(logging.WARNING):
            cn.send(make_event())
        assert len([r for r in caplog.records if "FIRING" in r.message]) >= 2

    def test_send_batch(self, caplog: pytest.LogCaptureFixture) -> None:
        cn = CompositeNotifier()
        cn.add(LogNotifier())
        with caplog.at_level(logging.WARNING):
            cn.send_batch([make_event(rule_name=f"r{i}") for i in range(2)])
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2

    def test_one_notifier_fails_others_still_run(self, caplog: pytest.LogCaptureFixture) -> None:
        class BadNotifier:
            def send(self, ev: AlertEvent) -> None:
                raise RuntimeError("boom")

        cn = CompositeNotifier()
        cn.add(LogNotifier())
        cn.add(BadNotifier())  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING):
            cn.send(make_event())  # should not raise, just log warning
        assert any("BadNotifier" in r.message and "boom" in r.message for r in caplog.records)

    def test_close_calls_all(self) -> None:
        class CloseTracker(LogNotifier):
            closed = False
            def close(self) -> None:
                CloseTracker.closed = True

        CloseTracker.closed = False
        cn = CompositeNotifier()
        cn.add(CloseTracker())
        cn.close()
        assert CloseTracker.closed is True
