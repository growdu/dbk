from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class RetentionCleanupResult:
    deleted_artifact_dirs: int
    deleted_artifact_paths: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "deleted_artifact_dirs": self.deleted_artifact_dirs,
            "deleted_artifact_paths": self.deleted_artifact_paths,
        }


def cleanup_artifact_dirs(
    *,
    artifacts_root: Path,
    older_than_hours: float,
    dry_run: bool,
) -> RetentionCleanupResult:
    if older_than_hours <= 0:
        raise ValueError("older_than_hours must be > 0")
    if not artifacts_root.exists():
        return RetentionCleanupResult(deleted_artifact_dirs=0, deleted_artifact_paths=[])

    cutoff_ts = time.time() - older_than_hours * 3600
    deleted: list[str] = []
    for child in sorted(artifacts_root.iterdir()):
        if not child.is_dir():
            continue
        mtime = child.stat().st_mtime
        if mtime >= cutoff_ts:
            continue
        deleted.append(str(child))
        if not dry_run:
            shutil.rmtree(child, ignore_errors=True)
    return RetentionCleanupResult(deleted_artifact_dirs=len(deleted), deleted_artifact_paths=deleted)

