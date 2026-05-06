"""REST API server for DBK Agent using FastAPI + uvicorn.

Usage:
    dbk api-server [--host 0.0.0.0] [--port 8080] [--workers 1]
    # Or via Python:
    from dbk.api_server import create_app, run_server
    app = create_app()
"""
from __future__ import annotations

import uuid
from typing import Any

from dbk.agent.core import Agent
from dbk.agent.memory import AgentMemory, SQLiteMemoryBackend
from dbk.agent.session_store import SessionStore
from dbk.agent.state import WorkflowStage
from dbk.agent.workflow import WorkflowStateMachine
from dbk.providers.base import BaseProvider
from dbk.providers.mock import MockProvider

# Lazy import to avoid hard dependency on fastapi/uvicorn at import time.
_fastapi: Any = None
_Starlette: Any = None


def _get_fastapi() -> Any:
    global _fastapi
    if _fastapi is None:
        _fastapi = __import__("fastapi", fromlist=["FastAPI"]).FastAPI
    return _fastapi


def _get_starlette() -> Any:
    global _Starlette
    if _Starlette is None:
        _Starlette = __import__("starlette.background", fromlist=["BackgroundTasks"]).BackgroundTasks
    return _Starlette


# ----------------------------------------------------------------------
# App state (shared across requests).
# ----------------------------------------------------------------------


class AppState:
    """Request-unsafe singleton shared state for the API server.

    Thread-safety note: agent.process_message is safe to call concurrently
    in most LLM backends, but the underlying store is protected via locks.
    """

    def __init__(
        self,
        agent: Agent | None = None,
        memory: AgentMemory | None = None,
    ) -> None:
        from dbk.config import agent_provider, agent_model

        provider_name = agent_provider()
        if provider_name == "openai":
            from dbk.providers.openai import OpenAIProvider

            _model = agent_model() or None
            _provider: BaseProvider = OpenAIProvider(model=_model)
        elif provider_name == "anthropic":
            from dbk.providers.anthropic import AnthropicProvider

            _model = agent_model() or None
            _provider = AnthropicProvider(model=_model)
        else:
            _provider = MockProvider()

        self.agent = agent or Agent(provider=_provider)
        self.memory = memory or AgentMemory()


# ----------------------------------------------------------------------
# Pydantic-like request/response models (plain dict + validation for simplicity).
# ----------------------------------------------------------------------


def _validate_session_id(session_id: str | None) -> str:
    if not session_id:
        return str(uuid.uuid4())
    return session_id


def _state_to_payload(state: Any) -> dict[str, Any]:
    """Convert an AgentState to a JSON-safe dict."""
    return {
        "session_id": state.session_id,
        "workflow_stage": state.workflow_stage.value,
        "workflow_goal": state.workflow_goal,
        "intent": state.intent,
        "turn_count": state.turn_count,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "metadata": state.metadata,
    }


# ----------------------------------------------------------------------
# FastAPI app factory.
# ----------------------------------------------------------------------


def create_app(state: AppState | None = None) -> Any:
    """Build and configure the FastAPI application."""
    FastAPI = _get_fastapi()

    if state is None:
        state = AppState()

    app = FastAPI(title="DBK Agent API", version="1.0.0")

    # ------------------------------------------------------------------
    # Health / info routes.
    # ------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, Any]:
        """Readiness probe: check agent and store are reachable."""
        try:
            info = state.agent.info()
            return {"ready": True, "agent": info}
        except Exception as exc:  # noqa: BLE001
            return {"ready": False, "error": str(exc)}

    @app.get("/info")
    def agent_info() -> dict[str, Any]:
        """Return agent configuration and capabilities."""
        info = state.agent.info()
        # Augment with memory info.
        backend = state.memory.backend
        backend_type = type(backend).__name__
        return {
            "agent": info,
            "memory_backend": backend_type,
        }

    # ------------------------------------------------------------------
    # Session routes.
    # ------------------------------------------------------------------

    @app.post("/sessions")
    def create_session(
        goal: str = "",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new agent session."""
        sid = _validate_session_id(session_id)
        s = state.agent.create_session(session_id=sid, goal=goal)
        return _state_to_payload(s)

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        """Get session details."""
        s = state.agent.get_session(session_id)
        if s is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        return _state_to_payload(s)

    @app.get("/sessions/{session_id}/history")
    def get_session_history(
        session_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Get conversation history for a session."""
        s = state.agent.get_session(session_id)
        if s is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        history = s.conversation_history[-limit:]
        return {
            "session_id": session_id,
            "turn_count": s.turn_count,
            "history": history,
        }

    @app.delete("/sessions/{session_id}")
    def delete_session(session_id: str) -> dict[str, Any]:
        """Delete a session (removes from both store and in-memory manager)."""
        # First remove from in-memory manager if it exists there.
        state.agent._session_manager.delete_session(session_id)
        # Then remove from persistent store.
        deleted = state.agent._session_store.delete(session_id)
        return {"deleted": True, "session_id": session_id}

    @app.get("/sessions")
    def list_sessions(
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List persisted sessions."""
        sessions = state.agent.list_sessions()
        return {
            "sessions": sessions[offset:offset + limit],
            "total": len(sessions),
        }

    @app.post("/sessions/{session_id}/workflow")
    def advance_workflow(
        session_id: str,
        stage: WorkflowStage | None = None,
    ) -> dict[str, Any]:
        """Advance the workflow stage for a session."""
        try:
            if stage is None:
                # Advance to next stage automatically.
                s = state.agent.get_session(session_id)
                if s is None:
                    from fastapi import HTTPException
                    raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
                wfm = WorkflowStateMachine(initial=s.workflow_stage)
                try:
                    wfm.next()
                except ValueError:
                    pass  # Already at terminal stage.
                stage = wfm.current
            new_state = state.agent.advance_workflow(session_id, stage)
            return _state_to_payload(new_state)
        except KeyError as exc:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------------
    # Message / chat routes.
    # ------------------------------------------------------------------

    @app.post("/chat")
    def chat(
        message: str,
        session_id: str | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Process a chat message and return the agent response.

        If stream=True, returns a Server-Sent Events response (text/event-stream).
        """
        sid = _validate_session_id(session_id)
        if stream:
            # Return a plain dict with session_id; streaming is handled
            # by the /chat/stream endpoint.
            return {"session_id": sid, "stream": True}
        result = state.agent.process_message(message, session_id=sid)

        # Optionally archive to memory every N turns.
        _maybe_archive_to_memory(state, result)

        return {
            "session_id": result["session_id"],
            "content": result["content"],
            "intent": result.get("intent", "general"),
            "tool_calls": result.get("tool_calls", []),
            "tool_results": result.get("tool_results", []),
            "workflow_stage": result.get("workflow_stage", "requirements"),
            "turn_count": result.get("turn_count", 1),
        }

    @app.post("/chat/stream")
    async def chat_stream(
        message: str,
        session_id: str | None = None,
    ) -> Any:
        """SSE streaming endpoint for chat responses."""
        import asyncio
        FastAPI = _get_fastapi()
        sid = _validate_session_id(session_id)

        async def event_generator() -> Any:
            loop = asyncio.get_event_loop()
            # Use process_stream which yields tokens synchronously.
            gen = state.agent.process_stream(message, session_id=sid)
            full_content = ""
            done = False
            while not done:
                try:
                    token: str | None = await loop.run_in_executor(None, next, (lambda g=gen: g.__next__()), None)  # type: ignore[arg-type]
                    if token is None:
                        done = True
                        break
                    full_content += token
                    yield {"event": "token", "data": token}
                except StopIteration:
                    done = True
            # Yield done marker.
            yield {"event": "done", "data": ""}
            # Archive to memory after stream completes.
            _maybe_archive_to_memory(state, {
                "session_id": sid,
                "content": full_content,
                "turn_count": 1,
            })

        # Return an SSE StreamingResponse.
        from starlette.responses import StreamingResponse
        async def sse_wrapper() -> Any:
            body_chunks: list[bytes] = []
            async for ev in event_generator():
                if ev["event"] == "token":
                    body_chunks.append(f"data: {ev['data']}\n\n".encode())
                elif ev["event"] == "done":
                    body_chunks.append(b"data: [DONE]\n\n")
            return StreamingResponse(
                iter(body_chunks),
                media_type="text/event-stream",
                headers={"X-Session-ID": sid},
            )
        return await sse_wrapper()

    # ------------------------------------------------------------------
    # Memory routes.
    # ------------------------------------------------------------------

    @app.post("/memory/facts")
    def memory_remember(
        session_id: str,
        key: str,
        value: str,
        importance: int = 5,
        tags: str = "",
    ) -> dict[str, Any]:
        """Store an important fact."""
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        fact = state.memory.remember(
            session_id=session_id,
            key=key,
            value=value,
            importance=importance,
            tags=tag_list,
        )
        return fact.to_dict()

    @app.get("/memory/facts")
    def memory_recall(
        session_id: str | None = None,
        key_prefix: str | None = None,
        min_importance: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Recall facts."""
        facts = state.memory.recall(
            session_id=session_id,
            key_prefix=key_prefix,
            min_importance=min_importance,
            limit=limit,
        )
        return {
            "facts": [f.to_dict() for f in facts],
            "count": len(facts),
        }

    @app.delete("/memory/facts/{fact_id}")
    def memory_forget(fact_id: str) -> dict[str, Any]:
        """Delete a fact."""
        deleted = state.memory.forget(fact_id)
        return {"deleted": deleted, "fact_id": fact_id}

    @app.post("/memory/summaries")
    def memory_summarize(
        session_id: str,
        summary: str,
        window_start: int,
        window_end: int,
    ) -> dict[str, Any]:
        """Record a conversation window summary."""
        s = state.memory.summarize(
            session_id=session_id,
            summary=summary,
            window_start=window_start,
            window_end=window_end,
        )
        return s.to_dict()

    @app.get("/memory/summaries")
    def memory_get_summaries(
        session_id: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Get recent summaries."""
        summaries = state.memory.get_summaries(session_id=session_id, limit=limit)
        return {
            "summaries": [s.to_dict() for s in summaries],
            "count": len(summaries),
        }

    @app.get("/memory/episodes")
    def memory_recall_episodes(
        session_id: str,
        since_turn: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Recall episodic memory entries."""
        episodes = state.memory.recall_episodes(
            session_id=session_id,
            since_turn=since_turn,
            limit=limit,
        )
        return {
            "episodes": episodes,
            "count": len(episodes),
        }

    @app.get("/memory/context")
    def memory_build_context(
        session_id: str,
        max_facts: int = 10,
        max_episodes: int = 5,
    ) -> dict[str, Any]:
        """Build a memory context string for the system prompt."""
        context = state.memory.build_context(
            session_id=session_id,
            max_facts=max_facts,
            max_episodes=max_episodes,
        )
        return {"context": context, "session_id": session_id}

    @app.post("/memory/prune")
    def memory_prune(
        session_id: str,
        retain_turns: int = 10,
    ) -> dict[str, Any]:
        """Prune old episodic entries."""
        deleted = state.memory.prune(session_id, retain_turns=retain_turns)
        return {"deleted": deleted, "session_id": session_id}

    # Register plugin API routes.
    try:
        from dbk.plugins import get_plugin_registry
        plugin_reg = get_plugin_registry()
        if not getattr(plugin_reg, "_loaded", False):
            plugin_reg.discover()
        for path, method, route_kwargs in plugin_reg.get_api_routes():
            handler = route_kwargs.pop("handler", None)
            if handler is None:
                continue
            if method.upper() == "GET":
                app.get(path, **route_kwargs)(handler)
            elif method.upper() == "POST":
                app.post(path, **route_kwargs)(handler)
            elif method.upper() == "PUT":
                app.put(path, **route_kwargs)(handler)
            elif method.upper() == "DELETE":
                app.delete(path, **route_kwargs)(handler)
            else:
                app.add_api_route(path, handler, methods=[method.upper()], **route_kwargs)
    except Exception:  # noqa: BLE001
        pass  # Plugin system unavailable — skip route registration.

    return app


# ----------------------------------------------------------------------
# Internal helpers.
# ----------------------------------------------------------------------


_ARCHIVE_INTERVAL: int | None = None  # Lazily resolved from config.


def _archive_interval() -> int:
    global _ARCHIVE_INTERVAL
    if _ARCHIVE_INTERVAL is None:
        from dbk.config import agent_archive_interval
        _ARCHIVE_INTERVAL = agent_archive_interval()
    return _ARCHIVE_INTERVAL


def _maybe_archive_to_memory(state: AppState, result: dict[str, Any]) -> None:
    """Archive chat turns to episodic memory periodically."""
    session_id = result.get("session_id", "")
    turn_count = result.get("turn_count", 0)
    if turn_count % _archive_interval() != 0:
        return
    try:
        s = state.agent.get_session(session_id)
        if s and s.conversation_history:
            # Archive the last turn pair.
            turns = s.conversation_history[-2:]
            for turn in turns:
                state.memory.archive_turn(
                    session_id=session_id,
                    turn_count=turn_count,
                    role=turn.get("role", "user"),
                    content=turn.get("content", ""),
                )
    except Exception:  # noqa: BLE001
        pass  # Memory archiving should never break the chat flow.


# ----------------------------------------------------------------------
# Server runner (CLI entry point).
# ----------------------------------------------------------------------


def run_server(
    host: str | None = None,
    port: int | None = None,
    workers: int | None = None,
    log_level: str | None = None,
) -> None:
    """Run the API server with uvicorn.

    Uses config values as defaults when arguments are None (i.e. when called
    from code without explicit arguments). CLI arguments always override config.
    """
    from dbk.config import api_server_host, api_server_log_level, api_server_port, api_server_workers

    uvicorn = __import__("uvicorn").run
    app = create_app()
    uvicorn(
        app,
        host=host if host is not None else api_server_host(),
        port=port if port is not None else api_server_port(),
        workers=workers if workers is not None else api_server_workers(),
        log_level=log_level if log_level is not None else api_server_log_level(),
    )


if __name__ == "__main__":
    run_server()
