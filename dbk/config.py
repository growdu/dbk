from __future__ import annotations

from pathlib import Path


def workspace_root(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve()


def dbk_root(cwd: Path | None = None) -> Path:
    return workspace_root(cwd) / ".dbk"


def runtime_db_path(cwd: Path | None = None) -> Path:
    return dbk_root(cwd) / "runtime.sqlite"


def artifacts_root(cwd: Path | None = None) -> Path:
    return dbk_root(cwd) / "artifacts" / "runtime"

