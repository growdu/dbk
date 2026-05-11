# DBK Agent — Web UI

## Overview

The DBK Agent Web UI is a browser-based chat interface for the DBK Agent REST API.
It provides all the capabilities of the Agent REPL in a graphical environment with:
session management, streaming responses, tool result display, memory inspection,
and quick-action buttons.

## Running the Web UI

You need two processes running simultaneously:

```bash
# Terminal 1: Start the REST API server
python3 -m dbk api-server --port 8080

# Terminal 2: Start a static file server for the frontend
cd frontend && python3 -m http.server 8081
```

Then open your browser at:
```
http://localhost:8081/?api=http://localhost:8080
```

### Configuring the API URL

The API URL is configurable via URL parameter:
```
http://localhost:8081/?api=http://your-server:8080
```

To allow the browser to connect to a different host/port (CORS), start the API server with:
```bash
python3 -m dbk api-server --host 0.0.0.0 --port 8080
```

> **Note:** The Web UI sends API requests directly from the browser. Configure your
> firewall/network settings accordingly.

## Features

### Chat

- Natural-language input to the DBK Agent
- Shift+Enter for newlines, Enter to send
- Streaming responses (toggle with the "Stream" checkbox)
- Tool result display with JSON viewer
- Intent detection shown as badges

### Session Management

- Auto-creates a session on first message
- Create / load / switch sessions from the sidebar
- Conversation history persists across page reloads (via API)

### Quick Actions

Pre-built prompts for common tasks:
- **Collect Metrics** — Collects mock metrics for `pg-main-01`
- **Diagnose Latency** — Runs latency diagnosis
- **Health Check** — Runs collector health check
- **Daemon Status** — Lists running collector daemons

### Tool Registry

The right panel shows all tools currently registered with the agent,
including any loaded from plugins.

### Memory Panel

Click **Memory** in the header to open a modal showing:
- Stored facts (semantic memory)
- Conversation summaries
- Recent episodic memory entries

### Session History

Click **History** in the header to view the full conversation history
for the current session.

### Info Modal

Click **Info** in the header to see the agent's configuration,
registered tools, and loaded plugins.

## Architecture

The Web UI is a purely client-side single-page application (SPA):

```
frontend/
  index.html   — HTML structure
  app.css      — Dark theme stylesheet
  app.js       — Vanilla JS, ES6, no framework dependencies
```

### API Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `GET /info` | Load agent info and tool list |
| `POST /sessions` | Create new session |
| `GET /sessions` | List sessions |
| `GET /sessions/{id}` | Load session details |
| `GET /sessions/{id}/history` | Load conversation history |
| `POST /chat` | Send message (blocking) |
| `POST /chat/stream` | Send message (SSE streaming) |
| `POST /memory/facts` | Store fact |
| `GET /memory/facts` | Recall facts |
| `GET /memory/summaries` | Recall summaries |
| `GET /memory/episodes` | Recall episodic memory |
| `GET /memory/context` | Build context string |
| `GET /metrics` | Prometheus metrics (plugin) |

### Browser Compatibility

Tested with modern browsers: Chrome 90+, Firefox 90+, Safari 15+.
No external JS dependencies — pure HTML/CSS/JS.

## Deployment

For production deployment, serve the frontend files with any static HTTP server:

```nginx
location / {
    root /path/to/dbk/frontend;
    try_files $uri $uri/ =404;
}
```

Or serve both the API and frontend from the same domain by mounting the frontend
files into the FastAPI app (requires a small `app.mount()` change in `api_server.py`).

## Customization

The UI uses CSS custom properties for easy theming. Override in `app.css`:

```css
:root {
  --bg: #0f1117;        /* Background */
  --surface: #171923;   /* Panel backgrounds */
  --accent: #38bdf8;    /* Primary accent (links, buttons) */
  --green: #34d399;     /* Success / OK states */
  --red: #f87171;       /* Error states */
  --purple: #a78bfa;    /* Intent badges */
}
```
