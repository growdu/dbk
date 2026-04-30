from __future__ import annotations

from dataclasses import dataclass

from .config import artifacts_root
from .retention import RetentionCleanupResult, cleanup_artifact_dirs
from .storage import RuntimeStore


# Default safety limits for cleanup operations.
# Guard against accidental mass deletion in production.
DEFAULT_MAX_DELETE_PER_RUN: int | None = 100_000
DEFAULT_MIN_RETENTION_HOURS: float = 24.0


@dataclass(slots=True)
class RuntimeCleanupSummary:
    dry_run: bool
    older_than_hours: float
    instance: str | None
    runtime_metrics_candidate: int
    runtime_metrics_deleted: int
    runtime_metrics_truncated: bool  # True if max_delete limit was hit
    runtime_metrics_top_instances: dict[str, int]  # per-instance candidate counts
    trace_candidate: int
    trace_deleted: int
    trace_skipped: bool
    artifacts_candidate: int
    artifacts_deleted: int
    artifacts_skipped: bool
    vacuum_applied: bool
    safety_floor_hours: float | None
    max_delete_per_run: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "older_than_hours": self.older_than_hours,
            "instance": self.instance,
            "runtime_metrics": {
                "candidate": self.runtime_metrics_candidate,
                "deleted": self.runtime_metrics_deleted,
                "truncated": self.runtime_metrics_truncated,
                "top_instances": self.runtime_metrics_top_instances,
            },
            "trace_artifacts_db": {
                "candidate": self.trace_candidate,
                "deleted": self.trace_deleted,
                "skipped": self.trace_skipped,
            },
            "artifact_dirs": {
                "candidate": self.artifacts_candidate,
                "deleted": self.artifacts_deleted,
                "skipped": self.artifacts_skipped,
            },
            "vacuum": self.vacuum_applied,
            "safety": {
                "floor_hours": self.safety_floor_hours,
                "max_delete_per_run": self.max_delete_per_run,
            },
        }


def cleanup_runtime_data(
    *,
    store: RuntimeStore,
    older_than_hours: float,
    instance: str | None,
    dry_run: bool,
    skip_trace_db: bool,
    skip_artifacts: bool,
    vacuum: bool,
    max_delete_per_run: int | None = DEFAULT_MAX_DELETE_PER_RUN,
    safety_floor_hours: float | None = DEFAULT_MIN_RETENTION_HOURS,
) -> RuntimeCleanupSummary:
    # Safety: refuse if older_than_hours is below the floor.
    if not dry_run and safety_floor_hours is not None and older_than_hours < safety_floor_hours:
        raise ValueError(
            f"older_than_hours={older_than_hours} is below the minimum retention "
            f"floor of {safety_floor_hours} hours. Use --dry-run to inspect first "
            f"or set a higher --older-than-hours value."
        )

    # Always count candidates regardless of dry_run (for visibility).
    metrics_candidate = store.count_metrics_older_than(
        older_than_hours=older_than_hours,
        instance=instance,
    )
    # Build per-instance breakdown for the report.
    top_instances: dict[str, int] = {}
    if instance is None:
        top_instances = store.count_metrics_by_instance_older_than(older_than_hours=older_than_hours)
    trace_candidate = store.count_trace_artifacts_older_than(older_than_hours=older_than_hours)
    artifact_preview: RetentionCleanupResult = cleanup_artifact_dirs(
        artifacts_root=artifacts_root(),
        older_than_hours=older_than_hours,
        dry_run=True,
    )

    metrics_deleted = 0
    metrics_truncated = False
    trace_deleted = 0
    artifacts_deleted = 0
    if not dry_run:
        metrics_deleted, metrics_truncated = store.delete_metrics_older_than(
            older_than_hours=older_than_hours,
            instance=instance,
            max_delete=max_delete_per_run,
        )
        if not skip_trace_db:
            trace_deleted = store.delete_trace_artifacts_older_than(
                older_than_hours=older_than_hours
            )
        if not skip_artifacts:
            applied = cleanup_artifact_dirs(
                artifacts_root=artifacts_root(),
                older_than_hours=older_than_hours,
                dry_run=False,
            )
            artifacts_deleted = applied.deleted_artifact_dirs
        if vacuum:
            store.vacuum()

    return RuntimeCleanupSummary(
        dry_run=dry_run,
        older_than_hours=older_than_hours,
        instance=instance,
        runtime_metrics_candidate=metrics_candidate,
        runtime_metrics_deleted=metrics_deleted,
        runtime_metrics_truncated=metrics_truncated,
        runtime_metrics_top_instances=top_instances,
        trace_candidate=trace_candidate,
        trace_deleted=trace_deleted,
        trace_skipped=skip_trace_db,
        artifacts_candidate=artifact_preview.deleted_artifact_dirs,
        artifacts_deleted=artifacts_deleted,
        artifacts_skipped=skip_artifacts,
        vacuum_applied=bool(vacuum and not dry_run),
        safety_floor_hours=safety_floor_hours,
        max_delete_per_run=max_delete_per_run,
    )

