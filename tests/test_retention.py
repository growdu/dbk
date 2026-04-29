from __future__ import annotations

import os
import time
from pathlib import Path

from dbk.retention import cleanup_artifact_dirs


def test_cleanup_artifact_dirs(tmp_path: Path) -> None:
    old_dir = tmp_path / "old-task"
    new_dir = tmp_path / "new-task"
    old_dir.mkdir()
    new_dir.mkdir()

    old_ts = time.time() - 10 * 24 * 3600
    os.utime(old_dir, (old_ts, old_ts))

    preview = cleanup_artifact_dirs(artifacts_root=tmp_path, older_than_hours=168, dry_run=True)
    assert preview.deleted_artifact_dirs == 1
    assert old_dir.exists()

    applied = cleanup_artifact_dirs(artifacts_root=tmp_path, older_than_hours=168, dry_run=False)
    assert applied.deleted_artifact_dirs == 1
    assert not old_dir.exists()
    assert new_dir.exists()

