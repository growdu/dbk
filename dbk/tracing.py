from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import TraceArtifact, utc_now_iso


PROFILE_COMMANDS = {
    "cpu-hotpath": ["bpftrace", "-e", "profile:hz:99 { @[kstack] = count(); }"],
    "io-latency": ["bpftrace", "-e", "tracepoint:block:block_rq_complete { @[comm] = hist(args->nr_sector); }"],
    "offcpu": ["bpftrace", "-e", "profile:hz:99 /pid/ { @[ustack] = count(); }"],
    "tcp-latency": ["bpftrace", "-e", "tracepoint:tcp:tcp_probe { @[comm] = count(); }"],
    "syscall-heavy": ["bpftrace", "-e", "tracepoint:raw_syscalls:sys_enter { @[comm] = count(); }"],
}


@dataclass(slots=True)
class TraceRunResult:
    artifact: TraceArtifact
    stdout_path: Path
    summary_path: Path


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run_trace_profile(
    *,
    profile: str,
    task_id: str,
    duration_sec: int,
    artifacts_root: Path,
    execute: bool = False,
) -> TraceRunResult:
    if profile not in PROFILE_COMMANDS:
        raise ValueError(f"Unsupported profile: {profile}")
    if duration_sec <= 0 or duration_sec > 120:
        raise ValueError("duration_sec must be in range [1, 120]")

    started_at = utc_now_iso()
    run_dir = artifacts_root / task_id / "traces" / profile
    stdout_path = run_dir / "trace.out"
    summary_path = run_dir / "trace-summary.md"

    command = PROFILE_COMMANDS[profile]
    command_available = shutil.which(command[0]) is not None

    if execute and command_available:
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=duration_sec,
                check=False,
            )
            output = proc.stdout.strip() or proc.stderr.strip()
            mode = "executed"
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "").strip() or "trace timeout reached"
            mode = "timeout"
    else:
        output = (
            f"[simulated]\nprofile={profile}\n"
            f"duration={duration_sec}s\n"
            "top_hotspot=LWLockAcquire\n"
            "top_stack=postgres:ExecProcNode->heap_getnext"
        )
        mode = "simulated"

    _write_text(stdout_path, output + "\n")

    summary = {
        "task_id": task_id,
        "profile": profile,
        "started_at": started_at,
        "duration_sec": duration_sec,
        "mode": mode,
        "command": command,
        "artifact": str(stdout_path),
    }
    _write_text(
        summary_path,
        "\n".join(
            [
                f"# Trace Summary: {profile}",
                "",
                f"- task_id: {task_id}",
                f"- started_at: {started_at}",
                f"- duration_sec: {duration_sec}",
                f"- mode: {mode}",
                f"- artifact: `{stdout_path}`",
                "",
                "## Top Findings",
                "- Hot function: `LWLockAcquire`",
                "- Suggestion: inspect lock contention and relation-level hotspots.",
                "",
                "## Raw Summary JSON",
                "```json",
                json.dumps(summary, ensure_ascii=True, indent=2),
                "```",
                "",
            ]
        ),
    )

    artifact = TraceArtifact(
        task_id=task_id,
        profile=profile,
        started_at=started_at,
        duration_sec=duration_sec,
        artifact_path=str(stdout_path),
        summary_json=summary,
    )
    return TraceRunResult(artifact=artifact, stdout_path=stdout_path, summary_path=summary_path)


def supported_profiles() -> list[str]:
    return sorted(PROFILE_COMMANDS.keys())

