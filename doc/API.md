# DBK Agent — REST API Reference

Base URL: `http://127.0.0.1:8080`

All endpoints accept and return `application/json` unless noted.

---

## Health & Info

### `GET /health`

Liveness probe. Returns `200 OK` if the server is running.

**Response:**
```json
{ "status": "ok" }
```

---

### `GET /ready`

Readiness probe. Checks the agent and store are reachable.

**Response (200):**
```json
{
  "ready": true,
  "agent": {
    "provider": "mock",
    "is_mock": true,
    "model": "mock",
    "tool_count": 12,
    "tools": ["collect_metrics", "query_metrics", ...],
    "plugins": [...],
    "plugin_count": 0
  }
}
```

**Response (non-200):**
```json
{ "ready": false, "error": "..." }
```

---

### `GET /info`

Returns agent configuration, capabilities, and loaded plugins.

**Response:**
```json
{
  "agent": {
    "provider": "mock",
    "is_mock": true,
    "model": "mock",
    "tool_count": 12,
    "tools": ["collect_metrics", "query_metrics", ...],
    "plugins": [...],
    "plugin_count": 0
  },
  "memory_backend": "SQLiteMemoryBackend"
}
```

---

## Sessions

### `POST /sessions`

Create a new agent session.

**Query Parameters:**
| Name | Type | Description |
|------|------|-------------|
| `session_id` | string | Optional. Provide to use a specific ID. |
| `goal` | string | Optional. Session goal/description. |

**Request body (JSON):**
```json
{
  "goal": "Investigate latency on pg-main-01"
}
```

**Response:**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "workflow_stage": "requirements",
  "workflow_goal": "Investigate latency on pg-main-01",
  "intent": null,
  "turn_count": 0,
  "created_at": "2026-04-30T02:40:00Z",
  "updated_at": "2026-04-30T02:40:00Z",
  "metadata": {}
}
```

---

### `GET /sessions`

List all persisted sessions.

**Query Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `limit` | int | 50 | Max sessions to return |
| `offset` | int | 0 | Pagination offset |

**Response:**
```json
{
  "sessions": [
    {
      "session_id": "...",
      "workflow_stage": "design",
      "turn_count": 3,
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "total": 1
}
```

---

### `GET /sessions/{session_id}`

Get details for a specific session.

**Response:**
```json
{
  "session_id": "...",
  "workflow_stage": "requirements",
  "workflow_goal": "...",
  "intent": null,
  "turn_count": 0,
  "created_at": "...",
  "updated_at": "...",
  "metadata": {}
}
```

Returns `404` if the session is not found.

---

### `GET /sessions/{session_id}/history`

Get conversation history for a session.

**Query Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `limit` | int | 50 | Max turns to return |

**Response:**
```json
{
  "session_id": "...",
  "turn_count": 2,
  "history": [
    { "role": "user", "content": "...", "turn_count": 1 },
    { "role": "assistant", "content": "...", "turn_count": 2 }
  ]
}
```

---

### `DELETE /sessions/{session_id}`

Delete a session from store and in-memory manager.

**Response:**
```json
{ "deleted": true, "session_id": "..." }
```

---

### `POST /sessions/{session_id}/workflow`

Advance the workflow stage for a session.

**Query Parameters:**
| Name | Type | Description |
|------|------|-------------|
| `stage` | string | Target stage name. Omit to auto-advance to the next stage. |

**Valid stages:** `requirements`, `design`, `implement`, `test`, `runtime`, `doc`, `ops`, `done`

**Response:**
```json
{
  "session_id": "...",
  "workflow_stage": "design",
  ...
}
```

---

## Chat

### `POST /chat`

Send a message and get a blocking response.

**Query Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `session_id` | string | auto | Session to use (auto-creates if omitted) |
| `stream` | bool | false | If `true`, use `/chat/stream` instead |

**Request body (JSON):**
```json
{
  "message": "Collect metrics for pg-main-01",
  "session_id": "...",
  "stream": false
}
```

**Response:**
```json
{
  "session_id": "...",
  "content": "I've collected metrics for pg-main-01...",
  "intent": "collect_metrics",
  "tool_calls": [
    { "name": "collect_metrics", "parameters": { "instance": "pg-main-01", "source": "mock" } }
  ],
  "tool_results": [
    {
      "tool": "collect_metrics",
      "ok": true,
      "result": { "collected": 12, "instance": "pg-main-01", "source": "mock" }
    }
  ],
  "workflow_stage": "requirements",
  "turn_count": 1
}
```

---

### `POST /chat/stream`

Send a message and receive a Server-Sent Events (SSE) stream.

**Query Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `session_id` | string | auto | Session to use |

**Response:** `text/event-stream`

```
data: token1
\n\n
data: token2
\n\n
...
data: [DONE]
\n\n
```

**Response headers:**
```
X-Session-ID: <session_id>
Content-Type: text/event-stream
```

---

## Memory

### `POST /memory/facts`

Store an important fact.

**Query Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `session_id` | string | required | Session this fact belongs to |
| `key` | string | required | Fact key/name |
| `value` | string | required | Fact value |
| `importance` | int | 5 | Importance (1-10, higher = more important) |
| `tags` | string | "" | Comma-separated tags |

**Response:**
```json
{
  "id": "...",
  "session_id": "...",
  "key": "instance_mentioned",
  "value": "pg-main-01",
  "importance": 4,
  "created_at": "...",
  "tags": ["observation"]
}
```

---

### `GET /memory/facts`

Recall facts with optional filters.

**Query Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `session_id` | string | null | Filter by session |
| `key_prefix` | string | null | Filter keys by prefix |
| `min_importance` | int | 0 | Minimum importance |
| `limit` | int | 50 | Max results |

**Response:**
```json
{
  "facts": [...],
  "count": 5
}
```

---

### `DELETE /memory/facts/{fact_id}`

Delete a fact by ID.

**Response:**
```json
{ "deleted": true, "fact_id": "..." }
```

---

### `POST /memory/summaries`

Record a conversation window summary.

**Request body (JSON):**
```json
{
  "session_id": "...",
  "summary": "User investigated latency on pg-main-01. Root cause identified as high lock contention.",
  "window_start": 0,
  "window_end": 5
}
```

**Response:** Summary object (same format as stored).

---

### `GET /memory/summaries`

Get recent summaries.

**Query Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `session_id` | string | null | Filter by session |
| `limit` | int | 5 | Max summaries |

**Response:**
```json
{
  "summaries": [...],
  "count": 2
}
```

---

### `GET /memory/episodes`

Recall episodic memory entries (raw conversation turns).

**Query Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `session_id` | string | required | Session to recall |
| `since_turn` | int | null | Only return turns after this number |
| `limit` | int | 20 | Max episodes |

**Response:**
```json
{
  "episodes": [
    { "id": 1, "session_id": "...", "turn_count": 1, "role": "user", "content": "...", ... }
  ],
  "count": 10
}
```

---

### `GET /memory/context`

Build a memory context string for injection into the system prompt.

**Query Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `session_id` | string | required | Session ID |
| `max_facts` | int | 10 | Max facts to include |
| `max_episodes` | int | 5 | Max episodes to include |

**Response:**
```json
{
  "context": "[Key facts learned]\n  - instance: pg-main-01\n[Recent turns]\n  U: Collect metrics...\n  A: Collected 12 metrics",
  "session_id": "..."
}
```

---

### `POST /memory/prune`

Prune old episodic entries, retaining the most recent N turns.

**Query Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `session_id` | string | required | Session to prune |
| `retain_turns` | int | 10 | Turns to keep |

**Response:**
```json
{ "deleted": 15, "session_id": "..." }
```

## SDK Reference

The DBK SDK (`from dbk import DBKClient`) provides a programmatic Python API for
embedding the DBK agent in external applications without going through the CLI.

### `DBKClient(config=None, auto_connect=True)`

Create a DBK client. `config` is an `SDKConfig` instance or `None` (reads
`~/.dbk/dbkaictl.toml` and environment variables).

```python
from dbk import DBKClient

client = DBKClient()  # uses default config
```

### `client.chat(message, session_id=None)`

Send a message to the agent and get a response.

```python
result = client.chat("Diagnose the latency spike on pg-main", session_id=None)
print(result["content"])
```

**Returns:** `dict` with keys: `content`, `session_id`, `intent`, `tool_calls`, `tool_results`, `workflow_stage`.

### `client.create_session(goal=None, session_id=None)`

Create a new session.

```python
state = client.create_session(goal="Investigate lock contention")
print(state.session_id)
```

**Returns:** `AgentState`.

### `client.health_check(source=None, dsn=None)`

Check PostgreSQL health and system status.

```python
health = client.health_check(source="postgres", dsn="postgresql://user:pass@host:5432/db")
print(health["status"])
```

**Returns:** `dict` with keys: `status`, `postgres_healthy`, `metrics_collected`, `alerts_firing`, `daemons_running`, `uptime_seconds`.

### `client.get_session(session_id)`

Retrieve a session by ID.

```python
state = client.get_session("550e8400-e29b-41d4-a716-446655440000")
print(state.workflow_stage)
```

**Returns:** `AgentState | None`.

### `client.get_workflow_status(session_id)`

Return workflow progress for a session.

```python
status = client.get_workflow_status("550e8400-e29b-41d4-a716-446655440000")
print(status["progress"]["percent"])
```

**Returns:** `dict` with keys: `session_id`, `current_stage`, `description`, `progress`.

### `client.start_daemons()`

Start all configured collector daemons (metrics, trace, cleanup).

### `client.stop_daemons()`

Stop all running collector daemons.

### `client.diagnose_incident(incident_type, instance="default")`

Run an automated incident diagnosis.

```python
result = client.diagnose_incident("latency", instance="pg-main")
print(result["summary"])
```

### `client.query_metrics(query, source=None, dsn=None, start=None, end=None)`

Query collected metrics with optional time range filter.

```python
metrics = client.query_metrics("pg_stat_bgwriter", start="2026-04-30T00:00:00Z")
for row in metrics:
    print(row)
```

### `client.get_daemon_status()`

Return status of all collector daemons.

```python
statuses = client.get_daemon_status()
for name, st in statuses.items():
    print(f"{name}: {st}")
```

---

## Plugin Routes

Plugins can register additional routes dynamically. See `doc/PLUGIN_SYSTEM.md`.

### `GET /metrics`

Provided by the `prometheus_exporter` sample plugin.

**Response (text/plain):**
```
# HELP dbk_requests_total Total chat requests processed
# TYPE dbk_requests_total counter
dbk_requests_total 42
# HELP dbk_requests_errors_total Requests that resulted in an error
# TYPE dbk_requests_errors_total counter
dbk_requests_errors_total 1
# HELP dbk_response_tokens_avg Average estimated response tokens
# TYPE dbk_response_tokens_avg gauge
dbk_response_tokens_avg 87.3
# HELP dbk_response_tokens_max Maximum estimated response tokens
# TYPE dbk_response_tokens_max gauge
dbk_response_tokens_max 203.0
```
