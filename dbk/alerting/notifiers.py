"""Alert notifiers: log, webhook, composite."""

from __future__ import annotations

import json
import logging
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from dbk.alerting.models import Alert, AlertEvent, AlertState

logger = logging.getLogger(__name__)


class AlertNotifier(ABC):
    """Abstract base class for alert notifiers."""

    @abstractmethod
    def send(self, event: AlertEvent) -> None:
        """Send a notification for the given alert event."""
        ...

    @abstractmethod
    def send_batch(self, events: list[AlertEvent]) -> None:
        """Send a batch of events."""
        ...

    def close(self) -> None:
        """Optional cleanup (e.g., close HTTP sessions)."""
        pass


class LogNotifier(AlertNotifier):
    """Logs alert events using the Python standard logger."""

    def __init__(
        self,
        level_firing: int = logging.WARNING,
        level_resolved: int = logging.INFO,
        logger_name: str = "dbk.alerts",
    ) -> None:
        self._log = logging.getLogger(logger_name)
        self._level_firing = level_firing
        self._level_resolved = level_resolved

    def _format(self, event: AlertEvent) -> str:
        alert = event.alert
        state_label = alert.state.value.upper()
        return (
            f"[{state_label}] {alert.severity.value.upper()} | "
            f"{alert.rule_name} | {alert.metric}={alert.value} "
            f"(threshold={alert.operator}{alert.threshold}) "
            f"| instance={alert.instance} | {alert.description}"
        )

    def send(self, event: AlertEvent) -> None:
        level = self._level_firing if event.type == "firing" else self._level_resolved
        self._log.log(level, self._format(event))

    def send_batch(self, events: list[AlertEvent]) -> None:
        for ev in events:
            self.send(ev)


class WebhookNotifier(AlertNotifier):
    """Sends alert events as JSON POST requests to a webhook URL."""

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout_sec: float = 10.0,
        secret: str | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers) if headers else {}
        self.timeout = timeout_sec
        self.secret = secret

    def _build_payload(self, event: AlertEvent) -> dict[str, Any]:
        return {
            "event_type": event.type,
            "timestamp": event.fired_at,
            "alert": event.alert.to_dict(),
        }

    def _sign_payload(self, payload_bytes: bytes) -> str:
        """Simple HMAC-SHA256 signing using the configured secret."""
        import hmac
        import hashlib
        return hmac.new(
            self.secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()

    def send(self, event: AlertEvent) -> None:
        payload = self._build_payload(event)
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "DBK-Alerter/1.0",
        }
        headers.update(self.headers)

        if self.secret:
            import hmac
            import hashlib
            signature = hmac.new(
                self.secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
            headers["X-DBK-Signature"] = f"sha256={signature}"

        try:
            req = urllib.request.Request(
                self.url,
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                if not (200 <= resp.status < 300):
                    logger.warning(
                        "Webhook returned non-2xx status=%d for alert %s",
                        resp.status,
                        event.alert.id,
                    )
        except urllib.error.HTTPError as exc:
            logger.warning(
                "Webhook HTTP error=%s for alert %s: %s",
                exc.code,
                event.alert.id,
                exc.reason,
            )
        except urllib.error.URLError as exc:
            logger.warning(
                "Webhook URL error for alert %s: %s",
                event.alert.id,
                exc.reason,
            )
        except TimeoutError:
            logger.warning("Webhook timeout for alert %s", event.alert.id)

    def send_batch(self, events: list[AlertEvent]) -> None:
        for ev in events:
            self.send(ev)


class CompositeNotifier(AlertNotifier):
    """Dispatches to one or more notifiers."""

    def __init__(self, notifiers: list[AlertNotifier] | None = None) -> None:
        self._notifiers: list[AlertNotifier] = notifiers or []

    def add(self, notifier: AlertNotifier) -> None:
        self._notifiers.append(notifier)

    def remove(self, notifier: AlertNotifier) -> None:
        self._notifiers.remove(notifier)

    def send(self, event: AlertEvent) -> None:
        for n in self._notifiers:
            try:
                n.send(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Notifier %s failed: %s", n.__class__.__name__, exc)

    def send_batch(self, events: list[AlertEvent]) -> None:
        for ev in events:
            self.send(ev)

    def close(self) -> None:
        for n in self._notifiers:
            try:
                n.close()
            except Exception:  # noqa: BLE001
                pass
