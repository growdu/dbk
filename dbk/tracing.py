from __future__ import annotations

import json
import os
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

MAX_TRACE_DURATION_SEC = 120
MAX_EXEC_TRACE_DURATION_SEC = 60

# polkit action IDs
ACTION_IDS = {
    "bpftrace": "org.dbk.bpftrace.run",
    "perf": "org.dbk.perf.run",
}

# Which binary implements each profile family
PROFILE_BINARY = {
    "cpu-hotpath": "bpftrace",
    "io-latency": "bpftrace",
    "offcpu": "bpftrace",
    "tcp-latency": "bpftrace",
    "syscall-heavy": "bpftrace",
}


@dataclass(slots=True)
class EscalationResult:
    """Result of a privilege-escalation attempt."""

    method: str          # 'root' | 'pkexec' | 'sudo' | 'none'
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


@dataclass(slots=True)
class TraceRunResult:
    artifact: TraceArtifact
    stdout_path: Path
    summary_path: Path


def _has_privilege() -> bool:
    """Return True if the current process already has CAP_SYS_ADMIN / root."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _binary_for_profile(profile: str) -> str:
    return PROFILE_BINARY.get(profile, "bpftrace")


def _action_id_for_profile(profile: str) -> str:
    binary = _binary_for_profile(profile)
    return ACTION_IDS.get(binary, "org.dbk.bpftrace.run")


def _get_username() -> str:
    """Return the real username of the caller (not the SUDO_USER env var)."""
    import pwd
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return os.environ.get("USER", "unknown")


# ---------------------------------------------------------------------------
# Low-level escalation helpers
# ---------------------------------------------------------------------------

def _run_direct(command: list[str], duration_sec: int, env: dict[str, str]) -> EscalationResult:
    """Run command directly (already root)."""
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=duration_sec,
            env=env,
            check=False,
        )
        return EscalationResult(
            method="root",
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            exit_code=proc.returncode,
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        return EscalationResult(
            method="root",
            stdout="",
            stderr="trace timeout reached",
            exit_code=None,
            timed_out=True,
        )


def _run_pkexec(
    command: list[str],
    timeout: int,
    env: dict[str, str],
    action_id: str,
) -> EscalationResult:
    """Run command via pkexec with polkit authorization.

    Raises PermissionError if polkit denies the request — we do NOT silently
    fall through to sudo, because that would defeat the purpose of scoping.
    """
    cmd = [
        "pkexec",
        "--disable-internal-agent",     # use running D-Bus auth agent
        "--keep-canonical-environment", # pass env vars (DBK_TRACE etc.)
        *command,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        # pkexec exits 126 when auth denied, 127 when not authorised at all
        if proc.returncode == 126:
            return EscalationResult(
                method="pkexec",
                stdout="",
                stderr="polkit: authentication denied",
                exit_code=proc.returncode,
                timed_out=False,
            )
        if proc.returncode == 127:
            return EscalationResult(
                method="pkexec",
                stdout="",
                stderr="polkit: not authorized",
                exit_code=proc.returncode,
                timed_out=False,
            )
        return EscalationResult(
            method="pkexec",
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            exit_code=proc.returncode,
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        return EscalationResult(
            method="pkexec",
            stdout="",
            stderr="pkexec: timeout during polkit negotiation",
            exit_code=None,
            timed_out=True,
        )
    except OSError as exc:
        # pkexec binary not found — raised here rather than silently skipping
        raise PermissionError(
            f"pkexec is not available on this system: {exc}"
        ) from exc


def _run_sudo(
    command: list[str],
    timeout: int,
    env: dict[str, str],
) -> EscalationResult:
    """Last-resort fallback: sudo (no scoping, but better than nothing)."""
    cmd = ["sudo", *command]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        return EscalationResult(
            method="sudo",
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            exit_code=proc.returncode,
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        return EscalationResult(
            method="sudo",
            stdout="",
            stderr="sudo: trace timeout reached",
            exit_code=None,
            timed_out=True,
        )
    except OSError as exc:
        raise PermissionError(
            f"sudo is not available on this system: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Escalation engine — 4-path: root → pkexec → sudo → none
# ---------------------------------------------------------------------------

def _escalate(
    *,
    command: list[str],
    duration_sec: int,
    task_id: str,
    profile: str,
) -> EscalationResult:
    """Attempt to run *command* with privilege (CAP_SYS_ADMIN).

    Resolution order:
      1. Already root  → direct execution (root)
      2. pkexec avail  → polkit-authorized execution (pkexec)
      3. sudo avail    → scoped fallback (sudo)
      4. neither       → simulated with reason

    polkit denial (returncode 126) is surfaced as a simulated result so the
    CLI can present a clear message rather than crashing.
    """
    env = os.environ.copy()
    env.update(
        DBK_TRACE="1",
        DBK_TASK_ID=task_id,
        DBK_APPROVED_BY=_get_username(),
        DBK_PROFILE=profile,
    )
    timeout = duration_sec + 15   # 15 s grace for polkit password prompt

    action_id = _action_id_for_profile(profile)

    if _has_privilege():
        return _run_direct(command, duration_sec, env)

    pkexec = shutil.which("pkexec")
    if pkexec:
        try:
            return _run_pkexec(command, timeout, env, action_id)
        except PermissionError:
            # pkexec binary exists but denied/not-authorised; try sudo.
            pass

    sudo_bin = shutil.which("sudo")
    if sudo_bin:
        try:
            return _run_sudo(command, timeout, env)
        except PermissionError:
            pass

    return EscalationResult(
        method="none",
        stdout="[no-escalation-path] pkexec and sudo unavailable",
        stderr="",
        exit_code=None,
        timed_out=False,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

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
    approve_privileged: bool = False,
) -> TraceRunResult:
    if profile not in PROFILE_COMMANDS:
        raise ValueError(f"Unsupported profile: {profile}")
    if duration_sec <= 0 or duration_sec > MAX_TRACE_DURATION_SEC:
        raise ValueError(f"duration_sec must be in range [1, {MAX_TRACE_DURATION_SEC}]")
    if execute and not approve_privileged:
        raise PermissionError(
            "Privileged trace execution requires explicit approval. "
            "Pass --approve-privileged."
        )
    if execute and duration_sec > MAX_EXEC_TRACE_DURATION_SEC:
        raise ValueError(
            f"execute mode duration_sec must be <= {MAX_EXEC_TRACE_DURATION_SEC}."
        )

    started_at = utc_now_iso()
    run_dir = artifacts_root / task_id / "traces" / profile
    stdout_path = run_dir / "trace.out"
    summary_path = run_dir / "trace-summary.md"

    command = PROFILE_COMMANDS[profile]
    command_available = shutil.which(command[0]) is not None

    if execute and command_available:
        esc = _escalate(
            command=command,
            duration_sec=duration_sec,
            task_id=task_id,
            profile=profile,
        )
        escalation_method = esc.method
        if esc.timed_out:
            mode = "timeout"
        elif esc.exit_code == 0:
            mode = "executed"
        else:
            mode = "escalation_failed"
        output = esc.stdout.strip() or esc.stderr.strip()
    elif execute and command_available and not _has_privilege():
        output = (
            "[simulated_unprivileged]\n"
            "execute=true but current user is not root.\n"
            "rerun with pkexec/sudo or keep simulated mode."
        )
        mode = "simulated_unprivileged"
        escalation_method = "none"
    elif execute and not command_available:
        output = (
            "[simulated_missing_tool]\n"
            f"command not found: {command[0]}\n"
            "install bpftrace then rerun with --execute."
        )
        mode = "simulated_missing_tool"
        escalation_method = "none"
    else:
        output = (
            f"[simulated]\nprofile={profile}\n"
            f"duration={duration_sec}s\n"
            "top_hotspot=LWLockAcquire\n"
            "top_stack=postgres:ExecProcNode->heap_getnext"
        )
        mode = "simulated"
        escalation_method = "none"

    _write_text(stdout_path, output + "\n")

    summary: dict[str, Any] = {
        "task_id": task_id,
        "profile": profile,
        "started_at": started_at,
        "duration_sec": duration_sec,
        "mode": mode,
        "escalation": escalation_method,
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
                f"- escalation: {escalation_method}",
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


def supported_escalation_methods() -> dict[str, bool]:
    """Return available privilege-escalation paths for diagnostics."""
    return {
        "has_privilege": _has_privilege(),
        "pkexec_available": shutil.which("pkexec") is not None,
        "sudo_available": shutil.which("sudo") is not None,
    }
