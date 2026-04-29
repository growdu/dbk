from __future__ import annotations

from dataclasses import dataclass

from .config import artifacts_root
from .retention import RetentionCleanupResult, cleanup_artifact_dirs
from .storage import RuntimeStore


@dataclass(slots=True)
class RuntimeCleanupSummary:
    dry_run: bool
    older_than_hours: float
    instance: str | None
    runtime_metrics_candidate: int
    runtime_metrics_deleted: int
    trace_candidate: int
    trace_deleted: int
    trace_skipped: bool
    artifacts_candidate: int
    artifacts_deleted: int
    artifacts_skipped: bool
    vacuum_applied: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "older_than_hours": self.older_than_hours,
            "instance": self.instance,
            "runtime_metrics": {
                "candidate": self.runtime_metrics_candidate,
                "deleted": self.runtime_metrics_deleted,
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
) -> RuntimeCleanupSummary:
    metrics_candidate = store.count_metrics_older_than(
        older_than_hours=older_than_hours,
        instance=instance,
    )
    trace_candidate = store.count_trace_artifacts_older_than(older_than_hours=older_than_hours)
    artifact_preview: RetentionCleanupResult = cleanup_artifact_dirs(
        artifacts_root=artifacts_root(),
        older_than_hours=older_than_hours,
        dry_run=True,
    )

    metrics_deleted = 0
    trace_deleted = 0
    artifacts_deleted = 0
    if not dry_run:
        metrics_deleted = store.delete_metrics_older_than(
            older_than_hours=older_than_hours,
            instance=instance,
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
        trace_candidate=trace_candidate,
        trace_deleted=trace_deleted,
        trace_skipped=skip_trace_db,
        artifacts_candidate=artifact_preview.deleted_artifact_dirs,
        artifacts_deleted=artifacts_deleted,
        artifacts_skipped=skip_artifacts,
        vacuum_applied=bool(vacuum and not dry_run),
    )

