"""Alert data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    def __int__(self) -> int:
        return {"info": 0, "warning": 1, "critical": 2}[self.value]


class AlertState(str, Enum):
    FIRING = "firing"
    RESOLVED = "resolved"
    ACKNOWLEDGED = "acknowledged"


@dataclass(slots=True)
class AlertRule:
    """A single alerting rule."""

    name: str
    metric: str
    operator: str  # gt, lt, gte, lte, eq
    threshold: float
    severity: Severity
    description: str
    instance: str | None = None
    minimum_duration_sec: int = 0
    cooldown_sec: int = 300
    labels: dict[str, str] = field(default_factory=dict)

    def evaluate(self, value: float) -> bool:
        """Return True if the value violates the threshold."""
        ops = {
            "gt": lambda v, t: v > t,
            "lt": lambda v, t: v < t,
            "gte": lambda v, t: v >= t,
            "lte": lambda v, t: v <= t,
            "eq": lambda v, t: abs(v - t) < 1e-9,
        }
        fn = ops.get(self.operator)
        if fn is None:
            return False
        return bool(fn(value, self.threshold))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "metric": self.metric,
            "operator": self.operator,
            "threshold": self.threshold,
            "severity": self.severity.value,
            "description": self.description,
            "instance": self.instance,
            "minimum_duration_sec": self.minimum_duration_sec,
            "cooldown_sec": self.cooldown_sec,
            "labels": self.labels,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AlertRule":
        return cls(
            name=str(d["name"]),
            metric=str(d["metric"]),
            operator=str(d["operator"]),
            threshold=float(d["threshold"]),
            severity=Severity(d.get("severity", "warning")),
            description=str(d.get("description", "")),
            instance=d.get("instance"),
            minimum_duration_sec=int(d.get("minimum_duration_sec", 0)),
            cooldown_sec=int(d.get("cooldown_sec", 300)),
            labels={k: str(v) for k, v in d.get("labels", {}).items()},
        )


# --------------------------------------------------------------------------
# Built-in default rules (9 rules covering latency/lock/IO/replication/buffer/connection/rollback/checkpoint)
# --------------------------------------------------------------------------
DEFAULT_ALERT_RULES: list["AlertRule"] = [
    AlertRule(
        name="query_latency_high",
        metric="query_latency_ms",
        operator="gt",
        threshold=1000.0,
        severity=Severity.WARNING,
        description="Average query latency exceeds 1 second",
    ),
    AlertRule(
        name="connection_high",
        metric="connection_count",
        operator="gt",
        threshold=80.0,
        severity=Severity.WARNING,
        description="Connection usage above 80% of max_connections",
    ),
    AlertRule(
        name="connection_critical",
        metric="connection_count",
        operator="gt",
        threshold=95.0,
        severity=Severity.CRITICAL,
        description="Connection usage above 95% of max_connections",
    ),
    AlertRule(
        name="slow_queries",
        metric="slow_query_count",
        operator="gt",
        threshold=10.0,
        severity=Severity.WARNING,
        description="More than 10 slow queries in the last interval",
    ),
    AlertRule(
        name="lock_wait_timeout",
        metric="lock_wait_count",
        operator="gt",
        threshold=5.0,
        severity=Severity.WARNING,
        description="More than 5 concurrent lock waits detected",
    ),
    AlertRule(
        name="replication_lag",
        metric="replication_lag_sec",
        operator="gt",
        threshold=30.0,
        severity=Severity.CRITICAL,
        description="Replication lag exceeds 30 seconds",
    ),
    AlertRule(
        name="buffer_hit_ratio_low",
        metric="buffer_hit_ratio",
        operator="lt",
        threshold=90.0,
        severity=Severity.WARNING,
        description="Buffer cache hit ratio below 90%",
    ),
    AlertRule(
        name="checkpoint_slow",
        metric="checkpoint_duration_ms",
        operator="gt",
        threshold=500.0,
        severity=Severity.WARNING,
        description="Checkpoint duration exceeds 500ms",
    ),
    AlertRule(
        name="rollback_ratio_high",
        metric="rollback_ratio",
        operator="gt",
        threshold=0.2,
        severity=Severity.WARNING,
        description="Transaction rollback ratio exceeds 20%",
    ),
]


@dataclass(slots=True)
class Alert:
    """A triggered alert instance."""

    id: str
    rule_name: str
    metric: str
    value: float
    threshold: float
    operator: str
    severity: Severity
    state: AlertState
    instance: str
    description: str
    fired_at: str
    resolved_at: str | None = None
    acknowledged_at: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule_name": self.rule_name,
            "metric": self.metric,
            "value": self.value,
            "threshold": self.threshold,
            "operator": self.operator,
            "severity": self.severity.value,
            "state": self.state.value,
            "instance": self.instance,
            "description": self.description,
            "fired_at": self.fired_at,
            "resolved_at": self.resolved_at,
            "acknowledged_at": self.acknowledged_at,
            "labels": self.labels,
            "annotations": self.annotations,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Alert":
        return cls(
            id=str(d["id"]),
            rule_name=str(d["rule_name"]),
            metric=str(d["metric"]),
            value=float(d["value"]),
            threshold=float(d["threshold"]),
            operator=str(d["operator"]),
            severity=Severity(d.get("severity", "warning")),
            state=AlertState(d.get("state", "firing")),
            instance=str(d["instance"]),
            description=str(d.get("description", "")),
            fired_at=str(d["fired_at"]),
            resolved_at=d.get("resolved_at"),
            acknowledged_at=d.get("acknowledged_at"),
            labels={k: str(v) for k, v in d.get("labels", {}).items()},
            annotations={k: str(v) for k, v in d.get("annotations", {}).items()},
        )


@dataclass(slots=True)
class AlertEvent:
    """An in-memory event emitted when an alert fires or resolves."""

    type: str  # "firing" | "resolved" | "acknowledged"
    alert: Alert
    fired_at: str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())
