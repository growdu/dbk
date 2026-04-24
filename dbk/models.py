from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(slots=True)
class RuntimeEvent:
    ts: str
    instance: str
    source: str
    category: str
    metric: str
    value: float
    labels: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        instance: str,
        source: str,
        category: str,
        metric: str,
        value: float,
        labels: dict[str, Any] | None = None,
    ) -> "RuntimeEvent":
        return cls(
            ts=utc_now_iso(),
            instance=instance,
            source=source,
            category=category,
            metric=metric,
            value=float(value),
            labels=labels or {},
        )


@dataclass(slots=True)
class TraceArtifact:
    task_id: str
    profile: str
    started_at: str
    duration_sec: int
    artifact_path: str
    summary_json: dict[str, Any]

