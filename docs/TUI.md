# DBK Agent — Textual TUI

## Overview

The DBK Agent Text User Interface (TUI) provides a terminal-based chat experience
with session management, workflow stage visualization, and tool result display.

## Requirements

```bash
pip install dbk[full]   # includes textual
# or
pip install textual
```

## Running

```bash
dbk tui
```

Or programmatically:

```python
from dbk.tui import run_tui

run_tui()
```

## Layout

```
┌─────────────────────────────────────────────────────────┐
│ DBK Agent  ·  provider: mock  ·  model: mock            │
├──────────────┬──────────────────────────────────────────┤
│ STAGE        │ [USER] Hello                            │
│ ○ Requirements│                                          │
│ ○ Design     │ [ASSISTANT] Hello! How can I help...    │
│ ● Implement  │                                          │
│ ○ Test       │                                          │
│ ○ Runtime    │                                          │
│ ○ Doc        │                                          │
│ ○ Ops        │                                          │
│ ○ Done       │                                          │
├──────────────┤                                          │
│ SESSIONS     │                                          │
│ > sid-abc123 │                                          │
│   sid-xyz789 │                                          │
├──────────────┤                                          │
│ ACTIONS      │                                          │
│ [Health]     │                                          │
│ [Metrics]    │                                          │
│ [Diagnose]   │                                          │
└──────────────┴──────────────────────────────────────────┘
│ dbk> _                                                 │
└─────────────────────────────────────────────────────────┘
```

- **Left sidebar (25%)**: Workflow stage stepper, session list, quick-action buttons
- **Main area**: Scrollable message log with tool result panels
- **Input bar**: Multi-line input at the bottom

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Enter` | Send message |
| `Shift+Enter` | Newline in input |
| `Ctrl+L` | Clear chat |
| `Ctrl+Q` | Quit |
| `Tab` | Cycle through workflow stages |
| `↑` / `↓` | Navigate session list |

## Features

### Workflow Stage Stepper

Shows all 8 workflow stages (Requirements → Design → Implement → Test →
Runtime → Doc → Ops → Done). The current stage is highlighted. Click a stage
to advance the session to that stage.

### Session Management

- Sessions are persisted to the SQLite store automatically
- Click a session in the sidebar to switch
- New sessions are created automatically on first message

### Quick Actions

Pre-built buttons in the sidebar send common prompts:

- **Health** — "Run a full health check on the PostgreSQL instance"
- **Metrics** — "Show recent database metrics"
- **Diagnose** — "Run a diagnosis for any recent incidents"

### Tool Result Panels

When the agent calls tools, results appear as collapsible panels in the message
stream. Click a tool result to expand/collapse it.

## Configuration

The TUI inherits the same provider configuration as the rest of DBK:

```bash
# Environment variables
export DBK_PROVIDER=mock       # mock | openai | anthropic
export DBK_MODEL=gpt-4o-mini
export DBK_ANTHROPIC_API_KEY=sk-...
export ANTHROPIC_AUTH_TOKEN=sk-ant-...
```

Or via `~/.dbk/dbkaictl.toml`:

```toml
[agent]
provider = "mock"
model = "mock"
```
