"""'dbk run' command — natural-language goal → workflow stage."""
from __future__ import annotations

import argparse
import json
from dbk.agent.core import Agent
from dbk.agent.state import WorkflowStage
from dbk.agent.workflow import WorkflowOrchestrator
from dbk.providers import get_provider


class RunCommand:
    """'dbk run' — run agent with a natural-language goal."""

    name = "run"
    help = "Run agent with a natural-language goal (auto-maps to workflow stage)"

    def configure(self, subparsers) -> argparse.ArgumentParser:
        p = subparsers.add_parser(self.name, help=self.help)
        p.add_argument("goal", help="Natural-language goal or task description")
        p.add_argument("--session", help="Session ID to resume")
        p.add_argument("--resume", action="store_true", help="Resume existing session")
        p.add_argument("--stage", help="Target workflow stage")
        p.add_argument("--no-auto-transition", dest="no_auto_transition", action="store_true")
        p.add_argument("--full", action="store_true", help="Run full workflow")
        p.set_defaults(func=self.execute)
        return p

    def execute(self, args: argparse.Namespace) -> int:
        provider = get_provider()
        agent = Agent(provider=provider)

        target_stage: WorkflowStage | None = None
        if args.stage:
            target_stage = WorkflowStage(args.stage)
        else:
            goal_lower = args.goal.lower()
            if any(kw in goal_lower for kw in ["monitor", "health", "check", "status", "metrics"]):
                target_stage = WorkflowStage.REQUIREMENTS
            elif any(kw in goal_lower for kw in ["design", "plan", "architecture", "approach"]):
                target_stage = WorkflowStage.DESIGN
            elif any(kw in goal_lower for kw in ["implement", "build", "create", "set up", "configure"]):
                target_stage = WorkflowStage.IMPLEMENT
            elif any(kw in goal_lower for kw in ["test", "validate", "verify", "check"]):
                target_stage = WorkflowStage.TEST
            elif any(kw in goal_lower for kw in ["deploy", "runtime", "start", "run"]):
                target_stage = WorkflowStage.RUNTIME
            elif any(kw in goal_lower for kw in ["document", "doc", "runbook", "readme"]):
                target_stage = WorkflowStage.DOC
            elif any(kw in goal_lower for kw in ["ops", "operational", "handover", "cleanup"]):
                target_stage = WorkflowStage.OPS
            else:
                target_stage = WorkflowStage.REQUIREMENTS

        session_id = args.session
        if args.resume and session_id:
            state = agent.get_session(session_id)
            if state:
                session_id = state.session_id
            else:
                print(f"Session not found: {session_id}", file=__import__("sys").stderr)
                return 2

        orchestrator = WorkflowOrchestrator(
            agent=agent, auto_transition_on_completion=not args.no_auto_transition
        )

        if args.full:
            result = orchestrator.run_full_workflow(goal=args.goal, session_id=session_id)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            result = orchestrator.run_stage(
                message=args.goal, target_stage=target_stage, session_id=session_id
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0