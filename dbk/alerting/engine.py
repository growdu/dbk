"""Alert evaluation engine."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dbk.alerting.models import (
    Alert,
    AlertEvent,
    AlertRule,
    AlertState,
    Severity,
)


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------


def load_rules(path: Path) -> list[AlertRule]:
    """Load alert rules from a JSON file.

    Expected format:
    {
        "rules": [
            {"name": "...", "metric": "...", "operator": "gt", "threshold": 0.5, ...}
        ]
    }

    Returns an empty list if the file does not exist.
    Raises ValueError if the file is malformed.
    """
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in rules file {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("Rules file must contain a JSON object.")

    raw_rules = payload.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ValueError("'rules' must be a list.")

    rules: list[AlertRule] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            raise ValueError(f"Each rule must be a JSON object: {item}")
        try:
            rules.append(AlertRule.from_dict(item))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid rule: {exc}") from exc

    return rules


# ---------------------------------------------------------------------------
# Alert engine state
# ---------------------------------------------------------------------------


class AlertEngine:
    """Evaluates metrics against alert rules and emits events."""

    def __init__(
        self,
        rules: list[AlertRule] | None = None,
        cooldown_sec: int = 300,
    ) -> None:
        self.rules = rules or []
        self.default_cooldown_sec = cooldown_sec
        # Active alert tracking: rule_name -> {
        #   "alert": Alert,
        #   "fired_at": datetime,
        #   "last_notified_at": datetime | None,
        # }
        self._active_alerts: dict[str, dict[str, Any]] = {}
        # Event bus for observers
        self._listeners: list[callable] = []

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    def add_listener(self, fn: callable) -> None:
        """Register a callable to receive AlertEvent objects."""
        self._listeners.append(fn)

    def remove_listener(self, fn: callable) -> None:
        """Unregister a callable."""
        try:
            self._listeners.remove(fn)
        except ValueError:
            pass

    def _emit(self, event: AlertEvent) -> None:
        for fn in self._listeners:
            try:
                fn(event)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Rule management
    # ------------------------------------------------------------------

    def update_rules(self, rules: list[AlertRule]) -> None:
        """Replace the current rule set."""
        self.rules = rules

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        metric: str,
        value: float,
        *,
        instance: str,
        ts: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[AlertEvent]:
        """Evaluate a single metric value against all matching rules.

        Returns a list of AlertEvents for rules that transitioned
        firing <-> resolved.
        """
        events: list[AlertEvent] = []
        now_ts = ts or utc_now_iso()
        now_dt = _parse_ts(now_ts)

        for rule in self.rules:
            if rule.metric != metric:
                continue
            if rule.instance is not None and rule.instance != instance:
                continue

            key = f"{rule.name}:{instance}"
            active = self._active_alerts.get(key)
            violated = rule.evaluate(value)

            if violated and active is None:
                # New firing alert
                cooldown = rule.cooldown_sec
                alert = Alert(
                    id=str(uuid.uuid4()),
                    rule_name=rule.name,
                    metric=metric,
                    value=value,
                    threshold=rule.threshold,
                    operator=rule.operator,
                    severity=rule.severity,
                    state=AlertState.FIRING,
                    instance=instance,
                    description=rule.description,
                    fired_at=now_ts,
                    labels=dict(labels) if labels else {},
                    annotations={"operator": rule.operator},
                )
                self._active_alerts[key] = {
                    "alert": alert,
                    "fired_at": now_dt,
                    "last_notified_at": None,
                }
                event = AlertEvent(type="firing", alert=alert)
                events.append(event)
                self._emit(event)

            elif violated and active is not None:
                # Still firing - update value
                active["alert"].value = value

            elif not violated and active is not None:
                # Resolved
                alert = active["alert"]
                alert.state = AlertState.RESOLVED
                alert.resolved_at = now_ts
                del self._active_alerts[key]
                event = AlertEvent(type="resolved", alert=alert)
                events.append(event)
                self._emit(event)

        return events

    def evaluate_batch(
        self,
        metrics: list[dict[str, Any]],
    ) -> list[AlertEvent]:
        """Evaluate a batch of metric snapshots.

        Each dict must have keys: metric, value, instance.
        Optional keys: ts, labels.
        """
        events: list[AlertEvent] = []
        for m in metrics:
            ev = self.evaluate(
                metric=str(m["metric"]),
                value=float(m["value"]),
                instance=str(m["instance"]),
                ts=m.get("ts"),
                labels=m.get("labels"),
            )
            events.extend(ev)
        return events

    # ------------------------------------------------------------------
    # Active alert queries
    # ------------------------------------------------------------------

    def get_firing_alerts(self) -> list[Alert]:
        """Return all currently firing alerts."""
        return [v["alert"] for v in self._active_alerts.values()]

    def get_active_count(self) -> int:
        """Return count of currently firing alerts."""
        return len(self._active_alerts)

    def get_firing_count_by_severity(self) -> dict[Severity, int]:
        """Return firing alert counts keyed by severity."""
        counts: dict[Severity, int] = {Severity.INFO: 0, Severity.WARNING: 0, Severity.CRITICAL: 0}
        for entry in self._active_alerts.values():
            sev = entry["alert"].severity
            counts[sev] = counts.get(sev, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Stateless convenience wrappers (use AlertEngine internally)
# ---------------------------------------------------------------------------

_engine: AlertEngine | None = None


def evaluate_rules(
    metrics: list[dict[str, Any]],
    rules: list[AlertRule],
    state_dir: Path | None = None,
) -> tuple[list[AlertEvent], dict[str, Any]]:
    """Stateless evaluation helper.

    Returns (events, summary_dict).
    This is a convenience wrapper around AlertEngine for single-shot evaluations.
    """
    global _engine
    if _engine is None:
        _engine = AlertEngine(rules=rules)
    else:
        _engine.update_rules(rules)

    events = _engine.evaluate_batch(metrics)
    summary = {
        "firing": len([e for e in events if e.type == "firing"]),
        "resolved": len([e for e in events if e.type == "resolved"]),
        "total_active": _engine.get_active_count(),
    }
    return events, summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts: str) -> datetime:
    """Parse an ISO timestamp string, returning a UTC datetime."""
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return datetime.now(tz=timezone.utc)
