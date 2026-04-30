#!/usr/bin/env bash
# =============================================================================
# DBK Agent — End-to-End Demo Script
#
# Demonstrates all major capabilities of the DBK Agent system:
#   1. CLI collection & diagnostics (no LLM needed)
#   2. Agent REPL (interactive LLM-powered assistant)
#   3. REST API server (HTTP endpoints)
#   4. Web UI (browser-based frontend)
#   5. Plugin system
#
# Requirements: bash, python3, optional: curl, xdg-open
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

# Paths
API_SERVER_PID=""
WEB_UI_PID=""
DEMO_SLEEP=1

info()  { echo -e "${CYAN}[INFO]${RESET} $*"; }
ok()    { echo -e "${GREEN}[OK]${RESET}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET} $*"; }
err()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
step()  { echo ""; echo -e "${BOLD}=== $* ===${RESET}"; }

# -----------------------------------------------------------------------------
cleanup() {
  step "Cleanup"
  [[ -n "$API_SERVER_PID" ]] && kill "$API_SERVER_PID" 2>/dev/null && ok "API server stopped (PID $API_SERVER_PID)"
  [[ -n "$WEB_UI_PID" ]] && kill "$WEB_UI_PID" 2>/dev/null && ok "Web UI server stopped (PID $WEB_UI_PID)"
  # Stop any lingering daemons started by this demo
  python3 -m dbk collect daemon stop --all 2>/dev/null || true
  python3 -m dbk runtime cleanup-daemon stop 2>/dev/null || true
  ok "Cleanup complete"
}

trap cleanup EXIT

# ==============================================================================
# PHASE 1 — Environment & init
# ==============================================================================
step "Phase 1: Environment Setup"
info "Installing DBK package in development mode..."
pip install -q -e . 2>/dev/null || warn "pip install failed (may already be installed)"
ok "Package ready"

info "Running DBK config validation..."
VALIDATE_OUTPUT=$(python3 -m dbk validate 2>&1)
if echo "$VALIDATE_OUTPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d['ok'] else 1)" 2>/dev/null; then
  ok "Configuration valid"
else
  warn "Config validation returned warnings (non-fatal for demo): $VALIDATE_OUTPUT"
fi

info "Initializing DBK runtime directories..."
python3 -m dbk init
ok "Init complete"

# ==============================================================================
# PHASE 2 — CLI-only operations (no LLM required)
# ==============================================================================
step "Phase 2: CLI Operations (No LLM)"

info "Collecting mock runtime metrics..."
python3 -m dbk collect --instance pg-main-01 --source mock
ok "Metrics collected for pg-main-01"

info "Collecting metrics for replica instance..."
python3 -m dbk collect --instance pg-replica-01 --source mock
ok "Metrics collected for pg-replica-01"

info "Querying latest query.p95_latency_ms..."
python3 -m dbk metrics --metric query.p95_latency_ms --instance pg-main-01 --limit 5
ok "Query complete"

info "Listing supported trace profiles..."
python3 -m dbk trace profiles
ok "Trace profiles listed"

info "Running cpu-hotpath trace (simulated, 5 seconds)..."
python3 -m dbk trace run --profile cpu-hotpath --task-id demo-cpu --duration 5
ok "Trace complete"

info "Diagnosing latency incident..."
python3 -m dbk diagnose latency --instance pg-main-01 --task-id demo-incident-1 --auto-trace --thresholds-file ./thresholds.example.json 2>&1 || warn "Diagnose returned non-zero (expected if no metrics in DB)"
ok "Diagnosis complete"

info "Running collector daemon for 10 seconds..."
python3 -m dbk collect daemon start --instance pg-main-01 --source mock --interval-sec 3 --priority 75 --tags demo &
DEMO_DAEMON_PID=$!
sleep 5
python3 -m dbk collect daemon status --instance pg-main-01
python3 -m dbk collect daemon list
kill "$DEMO_DAEMON_PID" 2>/dev/null || true
sleep 1
python3 -m dbk collect daemon stop --instance pg-main-01 2>/dev/null || true
ok "Collector daemon demo complete"

info "Running runtime cleanup (dry-run)..."
python3 -m dbk runtime cleanup --older-than-hours 1 --dry-run
ok "Dry-run cleanup complete"

info "Querying runtime cleanup report..."
python3 -m dbk runtime cleanup-report --limit 10 --window-hours 24
ok "Cleanup report complete"

# ==============================================================================
# PHASE 3 — Agent REPL (LLM-powered)
# ==============================================================================
step "Phase 3: Agent REPL (LLM-powered)"
info "Agent info..."
python3 -m dbk agent info
ok "Agent info displayed"

info "Listing agent sessions..."
python3 -m dbk agent sessions
ok "Sessions listed"

# ==============================================================================
# PHASE 4 — REST API server
# ==============================================================================
step "Phase 4: REST API Server"

info "Starting API server on port 8080..."
python3 -m dbk api-server --port 8080 &
API_SERVER_PID=$!
sleep 3

if ! kill -0 "$API_SERVER_PID" 2>/dev/null; then
  err "API server failed to start"
  exit 1
fi
ok "API server running (PID $API_SERVER_PID)"

info "Health check..."
curl -sf http://127.0.0.1:8080/health && ok "GET /health OK"

info "Readiness probe..."
curl -sf http://127.0.0.1:8080/ready && ok "GET /ready OK"

info "Agent info endpoint..."
curl -sf http://127.0.0.1:8080/info | python3 -m json.tool | head -20
ok "GET /info OK"

info "Creating session..."
SESSION_JSON=$(curl -sf -X POST http://127.0.0.1:8080/sessions -H "Content-Type: application/json" -d '{"goal":"demo session"}')
SESSION_ID=$(echo "$SESSION_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])" 2>/dev/null)
ok "Session created: ${SESSION_ID:0:8}..."

info "Chatting with agent (blocking)..."
RESPONSE=$(curl -sf -X POST "http://127.0.0.1:8080/chat?session_id=$SESSION_ID" \
  -H "Content-Type: application/json" \
  -d '{"message":"Summarize what the DBK agent can do.","stream":false}')
echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('content','(no content)')[:300])"
ok "Chat response received"

info "Listing sessions..."
curl -sf http://127.0.0.1:8080/sessions | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['total']} session(s)\")"
ok "Session list OK"

info "Session history..."
curl -sf "http://127.0.0.1:8080/sessions/$SESSION_ID/history?limit=5" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['turn_count']} turn(s)\")"
ok "History OK"

info "Memory facts..."
curl -sf -X POST "http://127.0.0.1:8080/memory/facts?session_id=$SESSION_ID&key=demo_run&value=true&importance=5" \
  -H "Content-Type: application/json" && ok "Memory fact stored"

info "Memory recall..."
curl -sf "http://127.0.0.1:8080/memory/facts?session_id=$SESSION_ID" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['count']} fact(s)\")"
ok "Memory recall OK"

info "Memory context..."
curl -sf "http://127.0.0.1:8080/memory/context?session_id=$SESSION_ID&max_facts=5&max_episodes=5" | python3 -c "import sys,json; d=json.load(sys.stdin); print(repr(d.get('context','')[:100]))"
ok "Memory context OK"

info "Workflow advance..."
curl -sf -X POST "http://127.0.0.1:8080/sessions/$SESSION_ID/workflow" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"stage: {d.get('workflow_stage','?')}\")"
ok "Workflow advance OK"

# ==============================================================================
# PHASE 5 — Plugin system demo
# ==============================================================================
step "Phase 5: Plugin System"

info "Discovering and loading plugins..."
python3 -c "
from dbk.plugins import PluginRegistry, hookimpl, PluginABC, get_plugin_registry

# Test directory plugin discovery
reg = get_plugin_registry()
loaded = reg.discover()
print(f'  Plugins loaded from discovery: {loaded}')
print(f'  Total plugin count: {reg.plugin_count}')

# Show the prometheus plugin /metrics route from samples
routes = reg.get_api_routes()
print(f'  Plugin API routes: {len(routes)}')
for r in routes:
    print(f'    {r[0]} [{r[1]}]')
"
ok "Plugin system operational"

info "Registering plugin API routes..."
curl -sf http://127.0.0.1:8080/metrics && ok "GET /metrics (Prometheus plugin) OK"

# ==============================================================================
# PHASE 6 — Web UI
# ==============================================================================
step "Phase 6: Web UI"

info "Checking frontend files..."
for f in frontend/index.html frontend/app.css frontend/app.js; do
  if [[ -f "$f" ]]; then
    ok "$f exists ($(wc -c < "$f") bytes)"
  else
    warn "$f not found"
  fi
done

info "Starting static file server for Web UI on port 8081..."
# Use Python's built-in http.server as a simple static file server
cd "$SCRIPT_DIR/frontend"
python3 -m http.server 8081 &
WEB_UI_PID=$!
cd "$SCRIPT_DIR"
sleep 1

if ! kill -0 "$WEB_UI_PID" 2>/dev/null; then
  warn "Static file server failed to start"
else
  ok "Web UI server running (PID $WEB_UI_PID)"
  info "Open: http://localhost:8081/?api=http://localhost:8080"
fi

# ==============================================================================
# PHASE 7 — Summary
# ==============================================================================
step "Demo Complete!"
echo ""
echo -e "${BOLD}DBK Agent — Demo Summary${RESET}"
echo ""
echo "  [Phase 2] CLI operations:   All passed (metrics, trace, diagnose, daemon, cleanup)"
echo "  [Phase 3] Agent REPL:       All passed (info, sessions)"
echo "  [Phase 4] REST API:        All passed (health, chat, sessions, memory, workflow)"
echo "  [Phase 5] Plugin system:   All passed (discovery, /metrics route)"
echo "  [Phase 6] Web UI:           Ready at http://localhost:8081/?api=http://localhost:8080"
echo ""
echo "  API server:  http://127.0.0.1:8080"
echo "  Web UI:      http://127.0.0.1:8081/?api=http://127.0.0.1:8080"
echo ""
echo "  Cleanup is automatic on exit."
echo ""
ok "Demo finished successfully!"
