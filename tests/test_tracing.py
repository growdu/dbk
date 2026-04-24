from __future__ import annotations

from pathlib import Path

import pytest

from dbk.tracing import run_trace_profile


def test_run_trace_profile_simulated(tmp_path: Path) -> None:
    result = run_trace_profile(
        profile="cpu-hotpath",
        task_id="task-1",
        duration_sec=5,
        artifacts_root=tmp_path,
        execute=False,
    )
    assert result.stdout_path.exists()
    assert result.summary_path.exists()
    assert result.artifact.profile == "cpu-hotpath"


def test_run_trace_profile_execute_requires_approval(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        run_trace_profile(
            profile="cpu-hotpath",
            task_id="task-2",
            duration_sec=5,
            artifacts_root=tmp_path,
            execute=True,
            approve_privileged=False,
        )
