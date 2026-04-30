/**
 * DBK Agent Web UI — app.js
 * Connects to the DBK Agent REST API (http://127.0.0.1:8080 by default).
 */

"use strict";

// ── Config ──────────────────────────────────────────────────────────────────
const API_BASE = (() => {
  // Allow override via URL param ?api=http://host:port
  const params = new URLSearchParams(window.location.search);
  return params.get("api") || "http://127.0.0.1:8080";
})();

// ── State ────────────────────────────────────────────────────────────────────
let state = {
  sessionId: null,
  provider: "mock",
  model: "-",
  tools: [],
  toolCalls: [],
  toolResults: [],
  intent: "",
  workflowStage: "requirements",
  messages: [],
};

let abortController = null;

// ── Helpers ──────────────────────────────────────────────────────────────────
async function api(path, options = {}) {
  const url = `${API_BASE}${path}`;
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }
  return res.json();
}

function esc(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function scrollChat() {
  const el = document.getElementById("chat-area");
  if (el) el.scrollTop = el.scrollHeight;
}

function showError(msg) {
  const el = document.getElementById("error-bar");
  if (el) { el.textContent = msg; el.style.display = "block"; }
  else console.error("[DBK UI]", msg);
}

function clearError() {
  const el = document.getElementById("error-bar");
  if (el) el.style.display = "none";
}

function showTyping(show) {
  const el = document.getElementById("typing-indicator");
  if (el) el.style.display = show ? "flex" : "none";
}

// ── Bootstrap ────────────────────────────────────────────────────────────────
async function init() {
  // Load agent info
  try {
    const info = await api("/info");
    state.provider = info.agent.provider;
    state.model = info.agent.model || "-";
    state.tools = info.agent.tools || [];
    document.getElementById("badge-provider").textContent = info.agent.provider;
    document.getElementById("badge-model").textContent = state.model;
    document.getElementById("badge-tools").textContent = info.agent.tool_count;
    renderTools();
  } catch (e) {
    console.warn("Agent info fetch failed:", e.message);
    document.getElementById("badge-provider").textContent = "offline";
  }

  // Load session list
  loadSessions();

  // Show welcome
  renderWelcome();
  updateWorkflowDisplay();
  updateContext();

  // Input: auto-resize textarea
  const inp = document.getElementById("msg-input");
  inp.addEventListener("input", () => {
    inp.style.height = "auto";
    inp.style.height = Math.min(inp.scrollHeight, 120) + "px";
  });
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
}

// ── Welcome ──────────────────────────────────────────────────────────────────
function renderWelcome() {
  const area = document.getElementById("chat-area");
  area.innerHTML = `
    <div class="welcome-msg">
      <h2>DBK Agent Web UI</h2>
      <p>Ask about metrics collection, latency diagnosis, traces, health checks, and more.</p>
      <p>Try: <em>Check the health of my PostgreSQL instance</em></p>
      <p>Or use Quick Actions in the sidebar.</p>
    </div>`;
}

// ── Session management ───────────────────────────────────────────────────────
async function createSession() {
  try {
    const payload = await api("/sessions", {
      method: "POST",
      body: JSON.stringify({ goal: "" }),
    });
    state.sessionId = payload.session_id;
    state.messages = [];
    state.workflowStage = payload.workflow_stage || "requirements";
    renderWelcome();
    updateSessionBadge();
    loadSessions();
    updateWorkflowDisplay();
    clearError();
  } catch (e) {
    showError("Failed to create session: " + e.message);
  }
}

async function loadSessions() {
  try {
    const data = await api("/sessions?limit=20");
    const list = document.getElementById("session-list");
    if (!data.sessions || data.sessions.length === 0) {
      list.innerHTML = '<div class="empty-hint">No sessions</div>';
      return;
    }
    list.innerHTML = data.sessions.map(s => {
      const active = s.session_id === state.sessionId ? "active" : "";
      const short = s.session_id.substring(0, 8) + "…";
      return `<div class="session-item ${active}" onclick="loadSession('${s.session_id}')" title="${esc(s.session_id)}">${esc(short)} <span style="color:var(--text-muted)">${esc(s.workflow_stage || "")}</span></div>`;
    }).join("");
  } catch (e) {
    console.warn("Session list failed:", e.message);
  }
}

async function loadSession(sid) {
  clearSessionLocal();
  state.sessionId = sid;
  try {
    const payload = await api(`/sessions/${sid}`);
    state.workflowStage = payload.workflow_stage || "requirements";
    const hist = await api(`/sessions/${sid}/history?limit=50`);
    if (hist.history && hist.history.length > 0) {
      hist.history.forEach(msg => addMessage(msg.role, msg.content, null, null, false));
    }
  } catch (e) {
    showError("Failed to load session: " + e.message);
  }
  updateSessionBadge();
  loadSessions();
  updateWorkflowDisplay();
  clearError();
}

function clearSessionLocal() {
  state.messages = [];
  state.intent = "";
  state.toolCalls = [];
  state.toolResults = [];
}

async function clearSession() {
  state.messages = [];
  state.intent = "";
  state.toolCalls = [];
  state.toolResults = [];
  renderWelcome();
  updateWorkflowDisplay();
  clearError();
}

function updateSessionBadge() {
  const badge = document.getElementById("badge-session");
  if (state.sessionId) {
    badge.textContent = state.sessionId.substring(0, 8);
  } else {
    badge.textContent = "no session";
  }
}

// ── Message rendering ─────────────────────────────────────────────────────────
function addMessage(role, content, intent, toolResults, shouldScroll = true) {
  const area = document.getElementById("chat-area");
  // Remove welcome on first real message
  const welcome = area.querySelector(".welcome-msg");
  if (welcome) welcome.remove();

  const id = "msg-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  const avatarChar = role === "user" ? "U" : "A";
  const row = document.createElement("div");
  row.className = `msg-row ${role}`;
  row.id = id;

  const intentTag = intent ? `<span class="msg-intent">${esc(intent)}</span>` : "";
  const toolBlocks = renderToolResults(toolResults);

  row.innerHTML = `
    <div class="msg-avatar ${role}">${avatarChar}</div>
    <div class="msg-body">
      <div class="msg-content">${esc(content)}</div>
      ${toolBlocks}
      <div class="msg-meta">${intentTag}</div>
    </div>`;

  area.appendChild(row);
  if (shouldScroll) scrollChat();
  return id;
}

function appendToMessage(role, token) {
  const area = document.getElementById("chat-area");
  // Find or create streaming message row
  let row = area.querySelector(".msg-row.assistant.streaming");
  if (!row) {
    row = document.createElement("div");
    row.className = "msg-row assistant streaming";
    row.innerHTML = `
      <div class="msg-avatar assistant">A</div>
      <div class="msg-body">
        <div class="msg-content" id="streaming-content"></div>
        <div class="msg-meta"><span class="msg-intent" id="streaming-intent"></span></div>
      </div>`;
    area.appendChild(row);
    // Remove welcome if present
    const welcome = area.querySelector(".welcome-msg");
    if (welcome) welcome.remove();
  }
  const content = document.getElementById("streaming-content");
  if (content) content.textContent += token;
  scrollChat();
}

function finishStreaming(intent, toolResults) {
  const area = document.getElementById("chat-area");
  const row = area.querySelector(".msg-row.assistant.streaming");
  if (row) {
    row.classList.remove("streaming");
    const intentEl = row.querySelector(".msg-intent");
    if (intentEl && intent) intentEl.textContent = intent;
    // Append tool results
    const body = row.querySelector(".msg-body");
    if (body) {
      const meta = body.querySelector(".msg-meta");
      const blocks = renderToolResults(toolResults);
      body.insertAdjacentHTML("beforeend", blocks);
    }
  }
}

function renderToolResults(toolResults) {
  if (!toolResults || toolResults.length === 0) return "";
  return toolResults.map(tr => {
    const name = esc(tr.tool || "?");
    const ok = tr.ok !== false;
    const cls = ok ? "ok" : "err";
    const label = ok ? `${name} OK` : `${name} ERROR`;
    let body = "";
    if (tr.ok && tr.result) {
      try {
        body = JSON.stringify(tr.result, null, 2);
      } catch { body = String(tr.result); }
    } else {
      body = tr.error || "unknown error";
    }
    return `<div class="tool-result">
      <div class="tool-result-header ${cls}">${label}</div>
      <div class="tool-result-body">${esc(body)}</div>
    </div>`;
  }).join("");
}

// ── Send message ─────────────────────────────────────────────────────────────
async function sendMessage() {
  const inp = document.getElementById("msg-input");
  const text = inp.value.trim();
  if (!text) return;

  // Auto-create session if none
  if (!state.sessionId) {
    try {
      const sp = await api("/sessions", { method: "POST", body: JSON.stringify({}) });
      state.sessionId = sp.session_id;
      updateSessionBadge();
      loadSessions();
    } catch (e) {
      showError("Session creation failed: " + e.message);
      return;
    }
  }

  clearError();
  inp.value = "";
  inp.style.height = "auto";

  // Add user message
  addMessage("user", text, null, null);

  const streamEnabled = document.getElementById("stream-toggle")?.checked;

  if (streamEnabled) {
    await streamChat(text);
  } else {
    await blockingChat(text);
  }
}

async function blockingChat(text) {
  showTyping(true);
  try {
    const data = await api("/chat", {
      method: "POST",
      body: JSON.stringify({
        message: text,
        session_id: state.sessionId,
        stream: false,
      }),
    });
    showTyping(false);
    addMessage("assistant", data.content || "", data.intent, data.tool_results);
    if (data.workflow_stage) {
      state.workflowStage = data.workflow_stage;
      updateWorkflowDisplay();
    }
    clearError();
  } catch (e) {
    showTyping(false);
    showError(e.message);
  }
}

async function streamChat(text) {
  // For streaming we use the non-streaming endpoint with SSE from /chat
  // but since the server uses a workaround, we simulate tokens from blocking call
  // and show them incrementally.
  showTyping(true);

  // Build the SSE request manually.
  const sid = state.sessionId;
  const params = new URLSearchParams({ message: text, session_id: sid, stream: "true" });
  abortController = new AbortController();

  try {
    const res = await fetch(`${API_BASE}/chat?${params}`, {
      method: "POST",
      signal: abortController.signal,
    });

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let done = false;

    while (!done) {
      const { value, done: d } = await reader.read();
      done = d;
      if (value) {
        buffer += decoder.decode(value, { stream: !done });
        // Process SSE lines: "data: <text>\n\n"
        const lines = buffer.split("\n");
        for (let i = 0; i < lines.length - 1; i++) {
          const line = lines[i].trim();
          if (line.startsWith("data: ")) {
            const raw = line.slice(6);
            if (raw === "[DONE]") {
              done = true;
              break;
            }
            appendToMessage("assistant", raw);
          }
        }
        buffer = lines[lines.length - 1];
      }
    }

    // Fallback: fetch blocking result for metadata
    const data = await api("/chat", {
      method: "POST",
      body: JSON.stringify({ message: text, session_id: sid, stream: false }),
    });
    finishStreaming(data.intent, data.tool_results);
    if (data.workflow_stage) {
      state.workflowStage = data.workflow_stage;
      updateWorkflowDisplay();
    }
  } catch (e) {
    if (e.name === "AbortError") {
      console.log("Stream aborted");
    } else {
      showError("Stream failed: " + e.message + " — falling back to blocking mode");
      await blockingChat(text);
    }
  } finally {
    showTyping(false);
    abortController = null;
  }
}

// ── Quick actions ────────────────────────────────────────────────────────────
async function quickCollect() {
  const msg = "Collect runtime metrics for instance pg-main-01 using the mock source";
  await sendQuickMessage(msg);
}

async function quickDiagnose() {
  const msg = "Diagnose latency incident for instance pg-main-01 with task ID webui-demo";
  await sendQuickMessage(msg);
}

async function quickHealth() {
  const msg = "Run a health check on the collector for pg-main-01";
  await sendQuickMessage(msg);
}

async function quickDaemonStatus() {
  const msg = "Show the status of all running collector daemons";
  await sendQuickMessage(msg);
}

async function sendQuickMessage(text) {
  if (!state.sessionId) {
    try {
      const sp = await api("/sessions", { method: "POST", body: JSON.stringify({}) });
      state.sessionId = sp.session_id;
      updateSessionBadge();
      loadSessions();
    } catch (e) { showError(e.message); return; }
  }
  clearError();
  addMessage("user", text, null, null);
  showTyping(true);
  try {
    const data = await api("/chat", {
      method: "POST",
      body: JSON.stringify({ message: text, session_id: state.sessionId }),
    });
    showTyping(false);
    addMessage("assistant", data.content || "", data.intent, data.tool_results);
    if (data.workflow_stage) {
      state.workflowStage = data.workflow_stage;
      updateWorkflowDisplay();
    }
  } catch (e) {
    showTyping(false);
    showError(e.message);
  }
}

// ── Tools panel ─────────────────────────────────────────────────────────────
function renderTools() {
  const list = document.getElementById("tools-list");
  if (!state.tools.length) {
    list.innerHTML = '<div class="empty-hint">No tools loaded</div>';
    return;
  }
  list.innerHTML = state.tools.map(t => `
    <div class="tool-item">
      <div class="tool-name">${esc(t.name)}</div>
      <div class="tool-desc">${esc(t.description || "").substring(0, 80)}</div>
      ${t.category ? `<span class="tool-cat">${esc(t.category)}</span>` : ""}
    </div>`).join("");
}

// ── Workflow display ─────────────────────────────────────────────────────────
function updateWorkflowDisplay() {
  const display = document.getElementById("workflow-display");
  const stages = ["requirements","design","implement","test","runtime","doc","ops","done"];
  const idx = stages.indexOf(state.workflowStage);
  const html = `
    <div class="workflow-stage">
      <span class="wstage">${esc(state.workflowStage)}</span>
    </div>
    <div class="workflow-progress">stage ${idx >= 0 ? idx + 1 : "?"}/8</div>`;
  if (display) display.innerHTML = html;
}

function updateContext() {
  const el = document.getElementById("context-info");
  if (el) {
    el.innerHTML = `
      <div>session: <span style="color:var(--accent)">${state.sessionId ? state.sessionId.substring(0, 8) : "-"}</span></div>
      <div>stage: <span style="color:var(--accent)">${esc(state.workflowStage)}</span></div>
      <div>turns: <span style="color:var(--accent)">${state.messages.length}</span></div>`;
  }
}

// ── Modals ───────────────────────────────────────────────────────────────────
function showModal(title, bodyHtml) {
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").innerHTML = bodyHtml;
  document.getElementById("modal-overlay").style.display = "flex";
}

function closeModal(e) {
  if (e && e.target !== e.currentTarget) return;
  document.getElementById("modal-overlay").style.display = "none";
}

async function showMemoryPanel() {
  if (!state.sessionId) { showError("No active session"); return; }
  try {
    const [facts, summaries, episodes] = await Promise.all([
      api(`/memory/facts?session_id=${state.sessionId}&limit=20`),
      api(`/memory/summaries?session_id=${state.sessionId}&limit=5`),
      api(`/memory/episodes?session_id=${state.sessionId}&limit=20`),
    ]);
    const html = `
      <h3 style="color:var(--accent);margin-bottom:0.5rem;">Facts (${facts.count || 0})</h3>
      ${(facts.facts || []).map(f => `<div style="margin-bottom:0.3rem;"><b>${esc(f.key)}</b>: ${esc(f.value).substring(0,100)} <span style="color:var(--text-muted);font-size:0.7rem;">[*${f.importance}]</span></div>`).join("") || '<div class="empty-hint">No facts stored</div>'}
      <h3 style="color:var(--accent);margin:0.75rem 0 0.5rem;">Summaries (${summaries.count || 0})</h3>
      ${(summaries.summaries || []).map(s => `<div style="margin-bottom:0.3rem;">${esc(s.summary)} <span style="color:var(--text-muted);font-size:0.7rem;">[${s.window_start}-${s.window_end}]</span></div>`).join("") || '<div class="empty-hint">No summaries</div>'}
      <h3 style="color:var(--accent);margin:0.75rem 0 0.5rem;">Episodes (${episodes.count || 0})</h3>
      ${(episodes.episodes || []).slice(-10).reverse().map(ep => `<div style="margin-bottom:0.2rem;font-size:0.75rem;"><b>${esc(ep.role || "?")[0].toUpperCase()}</b>: ${esc(ep.content || "").substring(0,120)}</div>`).join("") || '<div class="empty-hint">No episodes</div>'}`;
    showModal("Memory", html);
  } catch (e) {
    showError("Memory panel failed: " + e.message);
  }
}

async function showHistoryPanel() {
  if (!state.sessionId) { showError("No active session"); return; }
  try {
    const hist = await api(`/sessions/${state.sessionId}/history?limit=100`);
    const html = `
      <div style="display:flex;flex-direction:column;gap:0.3rem;">
        ${(hist.history || []).map(m => `
          <div style="border:1px solid var(--border);border-radius:6px;padding:0.4rem;background:var(--surface2);">
            <div style="font-size:0.65rem;color:var(--text-muted);margin-bottom:0.2rem;">${esc(m.role || "?")} | turn ${m.turn_count || "?"}</div>
            <div style="font-size:0.78rem;">${esc(m.content || "").substring(0, 300)}</div>
          </div>`).join("") || '<div class="empty-hint">No history</div>'}
      </div>`;
    showModal("Session History", html);
  } catch (e) {
    showError("History panel failed: " + e.message);
  }
}

async function showInfoModal() {
  try {
    const info = await api("/info");
    const html = `<pre style="white-space:pre-wrap;font-size:0.8rem;color:var(--text-dim);">${esc(JSON.stringify(info, null, 2))}</pre>`;
    showModal("Agent Info", html);
  } catch (e) {
    showError("Info failed: " + e.message);
  }
}

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", init);
