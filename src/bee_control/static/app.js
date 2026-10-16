const connDot = document.getElementById("conn-dot");
const connLabel = document.getElementById("conn-label");
const scanBtn = document.getElementById("scan-btn");
const deviceSelect = document.getElementById("device-select");
const connectBtn = document.getElementById("connect-btn");
const disconnectBtn = document.getElementById("disconnect-btn");
const controlsEl = document.getElementById("controls");
const logEl = document.getElementById("log");
const captureStartBtn = document.getElementById("capture-start-btn");
const captureStopBtn = document.getElementById("capture-stop-btn");
const captureStatus = document.getElementById("capture-status");

let connected = false;
let capturing = false;

function setConnected(isConnected, address) {
  connected = isConnected;
  connDot.className = "dot " + (isConnected ? "connected" : "disconnected");
  connLabel.textContent = isConnected ? `Connected (${address})` : "Not connected";
  connectBtn.disabled = isConnected || deviceSelect.options.length === 0;
  disconnectBtn.disabled = !isConnected;
  document.querySelectorAll("[data-control]").forEach((el) => {
    el.disabled = !isConnected;
  });
  captureStartBtn.disabled = !isConnected || capturing;
  captureStopBtn.disabled = !capturing;
}

function filename(path) {
  return path ? path.split("/").pop() : "";
}

function setCapturing(isCapturing, path, frames) {
  capturing = isCapturing;
  captureStartBtn.disabled = !connected || isCapturing;
  captureStopBtn.disabled = !isCapturing;
  if (isCapturing) {
    captureStatus.textContent = `recording (${frames || 0} frames) -> ${filename(path)}`;
  } else if (path) {
    captureStatus.textContent = `saved ${frames || 0} frames to ${filename(path)}`;
  } else {
    captureStatus.textContent = "idle";
  }
}

function appendLog(entry) {
  const line = document.createElement("div");
  line.className = `log-line ${entry.level}`;
  const ts = new Date(entry.ts * 1000).toLocaleTimeString();
  line.innerHTML = `<span class="ts">${ts}</span>${escapeHtml(entry.message)}`;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

async function api(path, opts) {
  const res = await fetch(path, {
    method: opts?.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts?.body ? JSON.stringify(opts.body) : undefined,
  });
  return res.json();
}

scanBtn.addEventListener("click", async () => {
  scanBtn.disabled = true;
  scanBtn.textContent = "Scanning...";
  try {
    const devices = await api("/api/scan", { method: "POST" });
    deviceSelect.innerHTML = "";
    for (const d of devices) {
      const opt = document.createElement("option");
      opt.value = d.address;
      opt.textContent = `${d.name || "(unnamed)"} ${d.address}`;
      deviceSelect.appendChild(opt);
    }
    connectBtn.disabled = devices.length === 0 || connected;
  } finally {
    scanBtn.disabled = false;
    scanBtn.textContent = "Scan";
  }
});

connectBtn.addEventListener("click", async () => {
  const address = deviceSelect.value;
  if (!address) return;
  connectBtn.disabled = true;
  const result = await api("/api/connect", { method: "POST", body: { address } });
  connectBtn.disabled = connected;
  if (!result.ok) {
    alert("Connect failed: " + result.error);
  }
});

disconnectBtn.addEventListener("click", async () => {
  await api("/api/disconnect", { method: "POST" });
});

captureStartBtn.addEventListener("click", async () => {
  captureStartBtn.disabled = true;
  const result = await api("/api/capture/start", { method: "POST" });
  if (!result.ok) {
    alert("Capture start failed: " + result.error);
    captureStartBtn.disabled = !connected || capturing;
  }
});

captureStopBtn.addEventListener("click", async () => {
  captureStopBtn.disabled = true;
  const result = await api("/api/capture/stop", { method: "POST" });
  if (!result.ok) {
    alert("Capture stop failed: " + result.error);
    captureStopBtn.disabled = !capturing;
  }
});

async function runControl(control, value) {
  const result = await api(`/api/command/${control.id}`, { method: "POST", body: { value } });
  if (!result.ok) {
    appendLog({ ts: Date.now() / 1000, level: "error", message: `${control.label} failed: ${result.error}` });
  }
}

function renderControls(controls) {
  const groups = {};
  for (const c of controls) {
    (groups[c.group] ||= []).push(c);
  }
  controlsEl.innerHTML = "";
  for (const [groupName, items] of Object.entries(groups)) {
    const groupEl = document.createElement("div");
    groupEl.className = "group";
    const h3 = document.createElement("h3");
    h3.textContent = groupName;
    groupEl.appendChild(h3);

    for (const c of items) {
      const row = document.createElement("div");
      row.className = "control-row";

      const labelEl = document.createElement("div");
      labelEl.className = "control-label";
      labelEl.innerHTML = `${escapeHtml(c.label)} <span class="badge ${c.status}">${c.status.replace("_", " ")}</span>`;
      row.appendChild(labelEl);

      const bodyEl = document.createElement("div");
      bodyEl.className = "control-body";

      if (c.kind === "button") {
        const btn = document.createElement("button");
        btn.textContent = "Run";
        btn.dataset.control = c.id;
        btn.disabled = !connected;
        btn.addEventListener("click", () => runControl(c, null));
        bodyEl.appendChild(btn);
      } else if (c.kind === "slider") {
        const input = document.createElement("input");
        input.type = "range";
        input.min = c.min;
        input.max = c.max;
        input.step = c.step || 1;
        input.value = c.default ?? c.min;
        input.dataset.control = c.id;
        input.disabled = !connected;
        const valueEl = document.createElement("span");
        valueEl.className = "slider-value";
        valueEl.textContent = input.value;
        input.addEventListener("input", () => {
          valueEl.textContent = input.value;
        });
        input.addEventListener("change", () => runControl(c, Number(input.value)));
        bodyEl.appendChild(input);
        bodyEl.appendChild(valueEl);
      }
      row.appendChild(bodyEl);

      if (c.note) {
        const noteEl = document.createElement("div");
        noteEl.className = "control-note";
        noteEl.textContent = c.note;
        row.appendChild(noteEl);
      }

      groupEl.appendChild(row);
    }
    controlsEl.appendChild(groupEl);
  }
}

function connectWebSocket() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "log") {
      appendLog(msg);
    } else if (msg.type === "connection") {
      setConnected(msg.connected, msg.address);
    } else if (msg.type === "capture") {
      setCapturing(msg.capturing, msg.path, msg.frames);
    }
  };
  ws.onclose = () => {
    setTimeout(connectWebSocket, 2000);
  };
}

async function init() {
  const controls = await api("/api/controls");
  renderControls(controls);
  const state = await api("/api/state");
  setCapturing(state.capturing, state.capture_path, state.capture_frames);
  setConnected(state.connected, state.address);
  for (const entry of state.log) appendLog(entry);
  connectWebSocket();
}

init();
