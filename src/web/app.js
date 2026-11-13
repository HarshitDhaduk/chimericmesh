/**
 * ChimericMesh: Frontend Engine
 * Includes WebGL ambient grid canvas, real-time telemetry streaming, and Kiro agent status updates.
 */

// 1. Ambient WebGL / Canvas Cyber Mesh
function initCanvas() {
  const canvas = document.getElementById("bg-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  function resize() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
  }
  window.addEventListener("resize", resize);
  resize();

  let time = 0;
  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "rgba(0, 210, 106, 0.04)";
    ctx.lineWidth = 1;

    const step = 48;
    const wave = Math.sin(time) * 4;

    // Horizontal grid lines
    for (let y = 0; y < canvas.height; y += step) {
      ctx.beginPath();
      ctx.moveTo(0, y + wave);
      ctx.lineTo(canvas.width, y + wave);
      ctx.stroke();
    }

    // Vertical grid lines
    for (let x = 0; x < canvas.width; x += step) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, canvas.height);
      ctx.stroke();
    }

    time += 0.02;
    requestAnimationFrame(draw);
  }
  draw();
}

// 2. Telemetry and Event Stream Controller
let autoStreamInterval = null;

async function fetchStatus() {
  try {
    const res = await fetch("/api/status");
    if (!res.ok) return;
    const data = await res.json();
    document.getElementById("stat-total-events").textContent = data.total_events;
    document.getElementById("stat-mcp-actions").textContent = data.mcp_actions_taken;
    const badge = document.getElementById("system-health-badge");
    badge.textContent = data.health;
    badge.className = data.health === "OPERATIONAL" ? "status-indicator" : "status-indicator warning";
  } catch (err) {
    console.debug("Status fetch pause:", err);
  }
}

async function triggerSimulateEvent() {
  try {
    const res = await fetch("/api/simulate", { method: "POST" });
    if (!res.ok) return;
    const event = await res.json();
    renderEvent(event);
    fetchStatus();
  } catch (err) {
    console.error("Simulation error:", err);
  }
}

function renderEvent(evt) {
  const stream = document.getElementById("terminal-stream");
  if (!stream) return;

  const row = document.createElement("div");
  row.className = `event-row ${evt.severity}`;
  if (evt.mcp_action) row.classList.add("mcp-fixed");

  let remediationHtml = "";
  if (evt.incident) {
    remediationHtml = `
      <div class="event-remediation">
        ⚡ <strong>Kiro Crew Action:</strong> ${evt.incident} → 
        <code>${evt.mcp_tool}()</code>: ${evt.mcp_action ? evt.mcp_action.status : "Dispatched"}
      </div>
    `;
  }

  row.innerHTML = `
    <div class="event-meta">
      <span>[${evt.protocol.toUpperCase()}] ${evt.subsystem} (${evt.source_host})</span>
      <span>${new Date(evt.timestamp).toLocaleTimeString()}</span>
    </div>
    <div class="event-msg">${escapeHtml(evt.clean_message)}</div>
    ${remediationHtml}
  `;

  stream.insertBefore(row, stream.firstChild);

  // Keep max 40 items in UI
  while (stream.children.length > 40) {
    stream.removeChild(stream.lastChild);
  }
}

function toggleAutoStream() {
  const btn = document.getElementById("btn-toggle-stream");
  if (autoStreamInterval) {
    clearInterval(autoStreamInterval);
    autoStreamInterval = null;
    btn.textContent = "Start Live Stream";
  } else {
    autoStreamInterval = setInterval(triggerSimulateEvent, 2500);
    btn.textContent = "Pause Stream";
    triggerSimulateEvent();
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// 3. Initialization
window.addEventListener("DOMContentLoaded", () => {
  initCanvas();
  fetchStatus();
  document.getElementById("btn-simulate").addEventListener("click", triggerSimulateEvent);
  document.getElementById("btn-toggle-stream").addEventListener("click", toggleAutoStream);

  // Seed with 3 initial events
  triggerSimulateEvent();
  setTimeout(triggerSimulateEvent, 400);
  setTimeout(triggerSimulateEvent, 800);
});
