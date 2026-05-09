"""Runtime provider abstractions: metrics, alert, and storage backends.

Design aligns with the LLM provider pattern from dbk.providers.base:

  MetricsProvider  — collect + query runtime metrics
  AlertProvider    — evaluate alert rules + manage state
  StorageProvider  — persist and retrieve runtime data

Each concrete backend implements the ABC.  Providers are instantiated
from config (e.g. metrics.provider = "mock" | "pgstat" | "prometheus").

This gives the CLI a unified interface regardless of which backend
is active, and makes it trivial to swap implementations.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator


# ---------------------------------------------------------------------------
# Shared result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MetricPoint:
    """A single metric sample."""
    metric: str
    value: float
    ts: datetime
    instance: str
    source: str
    labels: dict[str, str]


@dataclass(slots=True)
class MetricsResult:
    """Result of a metrics collection call."""
    points: list[MetricPoint]
    instance: str
    elapsed_ms: float


@dataclass(slots=True)
class QueryResult:
    """Result of a metrics query call."""
    metric: str
    points: list[MetricPoint]
    count: int
    elapsed_ms: float


# ---------------------------------------------------------------------------
# MetricsProvider ABC
# ---------------------------------------------------------------------------


class MetricsProvider(ABC):
    """Abstract base for runtime metrics providers.

    Implementations: MockMetricsProvider, PgMetricsProvider, PrometheusMetricsProvider.
    """

    name: str = "base"

    @abstractmethod
    def collect(self, instance: str, **kwargs) -> MetricsResult:
        """Collect current metrics for ``instance``."""

    @abstractmethod
    def query(
        self,
        metric: str,
        instance: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        aggregate: str | None = None,
    ) -> QueryResult:
        """Query historical metrics for ``metric``."""

    def health_check(self) -> bool:
        """Return True if the provider is ready to collect."""
        return True


# ---------------------------------------------------------------------------
# StorageProvider ABC
# ---------------------------------------------------------------------------


class StorageProvider(ABC):
    """Abstract base for runtime storage backends.

    Implementations: SQLiteStorageProvider (current), InfluxDBStorageProvider.
    """

    name: str = "base"

    @abstractmethod
    def init_schema(self) -> None:
        """Initialize the storage schema (create tables, indexes)."""

    @abstractmethod
    def store_events(self, events: list[dict]) -> int:
        """Store a batch of runtime events.  Returns number stored."""

    @abstractmethod
    def query_events(
        self,
        metric: str | None = None,
        instance: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Query stored runtime events."""

    def health_check(self) -> bool:
        """Return True if the backend is reachable."""
        return True


# ---------------------------------------------------------------------------
# AlertProvider ABC
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AlertRule:
    """A single alert rule definition."""
    name: str
    metric: str
    condition: str          # e.g. ">" | "<" | ">=" | "<="
    threshold: float
    severity: str = "warning"
    window_sec: int = 60
    cooldown_sec: int = 300
    enabled: bool = True


@dataclass(slots=True)
class AlertEvaluation:
    """Result of evaluating alert rules against current metrics."""
    rule_name: str
    metric: str
    current_value: float | None
    threshold: float
    triggered: bool
    severity: str
    message: str
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AlertProvider(ABC):
    """Abstract base for alert evaluation providers.

    Implementations: SQLiteAlertProvider (current), InfluxDBAlertProvider.
    """

    name: str = "base"

    @abstractmethod
    def evaluate(self, rules: list[AlertRule], metrics: list[MetricPoint]) -> list[AlertEvaluation]:
        """Evaluate ``rules`` against current ``metrics``."""

    @abstractmethod
    def load_rules(self) -> list[AlertRule]:
        """Load active alert rules from storage."""

    @abstractmethod
    def save_rules(self, rules: list[AlertRule]) -> None:
        """Persist alert rules to storage."""

    def health_check(self) -> bool:
        """Return True if the provider is ready."""
        return True
