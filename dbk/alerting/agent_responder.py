"""Alert → Agent responder: automatically triggers agent diagnostic session when alerts fire.

This closes the AIOps loop:
    Alert fires → AgentResponder → Agent.create_session → diagnose_incident → session store

Usage in alert daemon:
    from dbk.alerting.agent_responder import AgentResponder, start_responder
    responder = start_responder(alert_engine, agent)
    # responder runs in background, no further action needed.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dbk.alerting.engine import AlertEngine
    from dbk.alerting.models import Alert, AlertEvent
    from dbk.agent.core import Agent

logger = logging.getLogger(__name__)


@dataclass
class AgentResponderConfig:
    """Configuration for the agent responder."""

    # Minimum severity to trigger a diagnostic session.
    min_severity: str = "warning"  # info | warning | critical

    # Cooldown: ignore the same rule+instance if a session was started recently.
    cooldown_sec: float = 300.0

    # Timeout for the diagnostic session (seconds). 0 = no timeout (run to DONE).
    session_timeout_sec: float = 0.0

    # Whether to run full workflow (all 8 stages) or just a single-stage diagnostic.
    full_workflow: bool = False

    # If True, log every event the responder processes.
    verbose: bool = True


@dataclass
class DiagnosticSession:
    """Record of an agent diagnostic session started in response to an alert."""

    alert_id: str
    rule_name: str
    metric: str
    instance: str
    severity: str
    session_id: str
    started_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    completed_at: str | None = None
    verdict: str | None = None
    findings_count: int = 0
    error: str | None = None


# In-memory registry of responder sessions (also persisted in alert annotations).
_responder_sessions: dict[str, DiagnosticSession] = {}
_sessions_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _build_diagnostic_message(alert: "Alert", event_type: str = "firing") -> str:
    """Build a natural-language diagnostic goal from an Alert."""
    return (
        f"An alert has fired on instance '{alert.instance}'.\n"
        f"Rule: {alert.rule_name}\n"
        f"Metric: {alert.metric}\n"
        f"Current value: {alert.value:.4f} (threshold: {alert.threshold:.4f}, operator: {alert.operator})\n"
        f"Severity: {alert.severity.value}\n"
        f"Description: {alert.description}\n\n"
        f"Please diagnose this incident by:\n"
        f"1. Running the appropriate diagnostic SQL commands\n"
        f"2. Checking the relevant metrics time-series\n"
        f"3. Producing an evidence bundle with findings and a runbook\n"
        f"4. Recommending remediation steps\n\n"
        f"Focus on {alert.metric} and related metrics for the instance '{alert.instance}'."
    )


def _build_session_id(alert: "Alert") -> str:
    """Generate a stable session ID from an alert."""
    import hashlib
    key = f"{alert.id}:{alert.rule_name}:{alert.instance}"
    short_hash = hashlib.md5(key.encode()).hexdigest()[:8]
    return f"alert-diag-{short_hash}"


class AgentResponder:
    """Listens to AlertEngine events and triggers agent diagnostic sessions."""

    def __init__(
        self,
        agent: "Agent",
        config: AgentResponderConfig | None = None,
    ) -> None:
        self._agent = agent
        self._config = config or AgentResponderConfig()
        self._seen: dict[str, float] = {}  # rule_name:instance → last_seen timestamp
        self._seen_lock = threading.Lock()
        self._running = False

    def on_event(self, event: "AlertEvent") -> None:
        """Called by the alert engine via add_listener."""
        if not self._running:
            return

        if event.type != "firing":
            return

        alert = event.alert

        # Severity gate.
        severity_order = {"info": 0, "warning": 1, "critical": 2}
        min_level = severity_order.get(self._config.min_severity, 1)
        event_level = severity_order.get(alert.severity.value, 0)
        if event_level < min_level:
            return

        # Cooldown check.
        key = f"{alert.rule_name}:{alert.instance}"
        now = time.time()
        with self._seen_lock:
            last_seen = self._seen.get(key, 0.0)
            if now - last_seen < self._config.cooldown_sec:
                if self._config.verbose:
                    logger.info(
                        "[agent-responder] Cooldown active for %s, skipping (%.0f sec remaining)",
                        key,
                        self._config.cooldown_sec - (now - last_seen),
                    )
                return
            self._seen[key] = now

        # Build diagnostic session.
        session_id = _build_session_id(alert)
        diag_session = DiagnosticSession(
            alert_id=alert.id,
            rule_name=alert.rule_name,
            metric=alert.metric,
            instance=alert.instance,
            severity=alert.severity.value,
            session_id=session_id,
        )

        with _sessions_lock:
            _responder_sessions[alert.id] = diag_session

        if self._config.verbose:
            logger.info(
                "[agent-responder] Alert firing: rule=%s instance=%s metric=%s value=%.4f "
                "→ starting diagnostic session %s",
                alert.rule_name,
                alert.instance,
                alert.metric,
                alert.value,
                session_id,
            )

        # Run diagnostic (synchronously in the listener thread — alert daemon is
        # already async; a separate responder thread is started separately below).
        self._run_diagnostic(alert, session_id, diag_session)

    def _run_diagnostic(
        self,
        alert: "Alert",
        session_id: str,
        diag_session: DiagnosticSession,
    ) -> None:
        """Execute the diagnostic workflow for a firing alert."""
        try:
            # Create or resume the agent session.
            state = self._agent.get_session(session_id)
            if state is None:
                state = self._agent.create_session(
                    session_id=session_id,
                    goal=f"Diagnose alert: {alert.rule_name} on {alert.instance}",
                )

            message = _build_diagnostic_message(alert)

            # Process the diagnostic message.
            result = self._agent.process_message(message, session_id=session_id)

            diag_session.verdict = result.get("workflow_stage", "unknown")
            diag_session.findings_count = len(result.get("tool_results", []))
            diag_session.completed_at = _utc_now_iso()

            # Annotate the alert with session ID for traceability.
            alert.annotations["diagnostic_session_id"] = session_id
            alert.annotations["diagnostic_verdict"] = str(diag_session.verdict)

            if self._config.verbose:
                logger.info(
                    "[agent-responder] Diagnostic complete: session=%s verdict=%s findings=%d",
                    session_id,
                    diag_session.verdict,
                    diag_session.findings_count,
                )

        except Exception as exc:  # noqa: BLE001
            diag_session.error = str(exc)
            diag_session.completed_at = _utc_now_iso()
            logger.error(
                "[agent-responder] Diagnostic failed for alert %s: %s",
                alert.id,
                exc,
            )

    def start(self) -> "AgentResponder":
        """Mark the responder as active."""
        self._running = True
        return self

    def stop(self) -> None:
        """Stop the responder from processing new events."""
        self._running = False

    def get_session(self, alert_id: str) -> DiagnosticSession | None:
        """Return the diagnostic session for a given alert ID."""
        with _sessions_lock:
            return _responder_sessions.get(alert_id)

    def list_sessions(self) -> list[DiagnosticSession]:
        """Return all diagnostic sessions."""
        with _sessions_lock:
            return list(_responder_sessions.values())


def start_responder(
    alert_engine: "AlertEngine",
    agent: "Agent",
    config: AgentResponderConfig | None = None,
) -> AgentResponder:
    """Wire an AgentResponder into an AlertEngine.

    Returns the responder. The responder processes events in the calling thread
    (typically the alert daemon's loop). For fully async operation, call
    `responder.start()` after adding it as a listener.
    """
    responder = AgentResponder(agent=agent, config=config)
    alert_engine.add_listener(responder.on_event)
    responder.start()
    return responder


def get_diagnostic_session(alert_id: str) -> DiagnosticSession | None:
    """Helper: retrieve a diagnostic session by alert ID."""
    with _sessions_lock:
        return _responder_sessions.get(alert_id)


def list_diagnostic_sessions() -> list[DiagnosticSession]:
    """Helper: list all diagnostic sessions."""
    with _sessions_lock:
        return list(_responder_sessions.values())
