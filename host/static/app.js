const $ = (id) => document.getElementById(id);

const dot = $("conn-dot");
const logEl = $("log");

function log(msg, cls) {
  const line = document.createElement("div");
  if (cls) line.className = cls;
  line.textContent = $("opt-timestamps").checked
    ? `[${new Date().toLocaleTimeString()}] ${msg}`
    : msg;
  logEl.appendChild(line);
  if ($("opt-autoscroll").checked) logEl.scrollTop = logEl.scrollHeight;
}

// -- theme switcher -----------------------------------------------------
const THEME_KEY = "pico-bdm-theme";
function applyTheme(name) {
  document.documentElement.setAttribute("data-theme", name);
  localStorage.setItem(THEME_KEY, name);
}
$("theme-select").value = localStorage.getItem(THEME_KEY) || "amber";
applyTheme($("theme-select").value);
$("theme-select").addEventListener("change", (e) => applyTheme(e.target.value));

// -- advanced (Memory/Scope) toggle ---------------------------------------
const ADVANCED_KEY = "pico-bdm-advanced";
const benchEl = document.querySelector(".bench");
const advancedToggle = $("advanced-toggle");
function setAdvanced(show) {
  benchEl.classList.toggle("show-advanced", show);
  document.body.classList.toggle("advanced-scroll", show);
  advancedToggle.textContent = show ? "Hide advanced ▾" : "Show advanced ▸";
  advancedToggle.setAttribute("aria-pressed", String(show));
  localStorage.setItem(ADVANCED_KEY, show ? "1" : "0");
}
setAdvanced(localStorage.getItem(ADVANCED_KEY) === "1");
advancedToggle.addEventListener("click", () =>
  setAdvanced(!benchEl.classList.contains("show-advanced"))
);

// -- "how to use this page" guide modal ----------------------------------
const guideModal = $("guide-modal");
const guideBtn = $("guide-btn");
function openGuide() {
  guideModal.hidden = false;
  guideBtn.classList.add("active");
}
function closeGuide() {
  guideModal.hidden = true;
  guideBtn.classList.remove("active");
}
guideBtn.addEventListener("click", openGuide);
$("guide-close-btn").addEventListener("click", closeGuide);
guideModal.addEventListener("click", (e) => {
  if (e.target === guideModal) closeGuide();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !guideModal.hidden) closeGuide();
});

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "request failed");
  return data;
}

function setConnected(connected) {
  dot.classList.toggle("connected", connected);
  $("disconnect-btn").disabled = !connected;
  $("connect-btn").disabled = connected;
  $("sync-btn").disabled = !connected;
  $("reset-target-btn").disabled = !connected;
  // scope-run-btn stays disabled regardless of connection state -- capture
  // currently hangs the Pico, see CONTEXT.md.
}

function setSynced(synced) {
  $("mem-read-btn").disabled = !synced;
  $("wb-btn").disabled = !synced;
  $("flash-btn").disabled = !synced;
  $("mass-erase-btn").disabled = !synced;
  $("blank-check-btn").disabled = !synced;
  $("go-btn").disabled = !synced;
  $("reg-read-btn").disabled = !synced;
  $("reg-write-btn").disabled = !synced;
  $("bkpt-read-btn").disabled = !synced;
  $("bkpt-write-btn").disabled = !synced;
  $("step-btn").disabled = !synced;
  $("tagged-go-btn").disabled = !synced;
  $("control-read-btn").disabled = !synced;
  $("control-write-btn").disabled = !synced;
}

async function refreshPorts() {
  const data = await api("/api/ports");
  const select = $("port-select");
  select.innerHTML = "";
  for (const p of data.ports) {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = p;
    select.appendChild(opt);
  }
  if (data.ports.length === 0) {
    log("no serial ports found", "err");
  }
}

$("refresh-ports").addEventListener("click", () =>
  refreshPorts().catch((e) => log(e.message, "err"))
);

$("connect-btn").addEventListener("click", async () => {
  const port = $("port-select").value;
  if (!port) return log("no port selected", "err");
  try {
    await api("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ port }),
    });
    setConnected(true);
    log(`connected to ${port}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("disconnect-btn").addEventListener("click", async () => {
  await api("/api/disconnect", { method: "POST" });
  setConnected(false);
  setSynced(false);
  $("sync-readout").textContent = "not synced";
  log("disconnected");
});

$("sync-btn").addEventListener("click", async () => {
  dot.classList.add("working");
  try {
    const data = await api("/api/sync", { method: "POST" });
    const mhz = (data.target_freq_hz / 1e6).toFixed(3);
    $("sync-readout").textContent = `target clock ~${mhz} MHz`;
    setSynced(true);
    log(`synced — target clock ~${mhz} MHz`, "ok");
  } catch (e) {
    log(e.message, "err");
  } finally {
    dot.classList.remove("working");
  }
});

$("reset-target-btn").addEventListener("click", async () => {
  try {
    await api("/api/reset_target", { method: "POST" });
    setSynced(false);
    $("sync-readout").textContent = "not synced";
    log("target reset (plain RESET pulse — running its own code)", "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("go-btn").addEventListener("click", async () => {
  try {
    await api("/api/go", { method: "POST" });
    log("target resumed (GO)", "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("mem-read-btn").addEventListener("click", async () => {
  const addr = $("mem-addr").value;
  const len = $("mem-len").value;
  try {
    const data = await api(
      `/api/read_block?addr=${encodeURIComponent(addr)}&len=${encodeURIComponent(len)}`
    );
    const bytes = data.hex.match(/../g) || [];
    $("mem-output").textContent = bytes
      .map((b, i) => b.toUpperCase())
      .join(" ");
    log(`read ${bytes.length} byte(s) from ${addr}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("wb-btn").addEventListener("click", async () => {
  const addr = $("wb-addr").value;
  const value = $("wb-value").value;
  try {
    await api("/api/write_byte", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        addr: parseInt(addr, 16) || parseInt(addr, 0),
        value: parseInt(value, 16) || parseInt(value, 0),
      }),
    });
    log(`wrote ${value} to ${addr}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("mass-erase-btn").addEventListener("click", async () => {
  if (!confirm("Mass erase the target's FLASH? This cannot be undone.")) return;
  try {
    await api("/api/mass_erase", { method: "POST" });
    log("mass erase complete", "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("blank-check-btn").addEventListener("click", async () => {
  try {
    const data = await api("/api/blank_check", { method: "POST" });
    log(data.blank ? "blank check: chip is erased" : "blank check: chip is NOT blank", data.blank ? "ok" : "err");
  } catch (e) {
    log(e.message, "err");
  }
});

$("flash-btn").addEventListener("click", async () => {
  const fileInput = $("srec-file");
  if (!fileInput.files.length) return log("choose a .s19 file first", "err");
  const busFreq = $("bus-freq").value;

  const form = new FormData();
  form.append("file", fileInput.files[0]);
  form.append("bus_freq_hz", busFreq);
  form.append("erase_mode", $("erase-mode").value);
  form.append("verify", $("opt-verify").checked ? "1" : "0");

  try {
    log(`programming ${fileInput.files[0].name}...`);
    const res = await fetch("/api/flash_srec", { method: "POST", body: form });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    $("flash-output").textContent = JSON.stringify(data.chunks, null, 2);
    log(`programmed ${data.total_bytes} bytes across ${data.chunks.length} region(s)`, "ok");

    if ($("opt-run-after").checked) {
      await api("/api/go", { method: "POST" });
      log("target resumed (GO)", "ok");
    }
  } catch (e) {
    log(e.message, "err");
  }
});

// -- Registers & breakpoint ---------------------------------------------

function parseNum(s) {
  return parseInt(s, s.trim().toLowerCase().startsWith("0x") ? 16 : 10);
}

$("reg-read-btn").addEventListener("click", async () => {
  const reg = $("reg-select").value;
  try {
    const data = await api(`/api/reg?reg=${encodeURIComponent(reg)}`);
    const width = reg === "A" || reg === "CCR" ? 2 : 4;
    $("reg-value").value = "0x" + data.value.toString(16).toUpperCase().padStart(width, "0");
    log(`${reg} = 0x${data.value.toString(16).toUpperCase()}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("reg-write-btn").addEventListener("click", async () => {
  const reg = $("reg-select").value;
  const value = parseNum($("reg-value").value);
  try {
    await api("/api/reg", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reg, value }),
    });
    log(`wrote ${reg} = 0x${value.toString(16).toUpperCase()}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("bkpt-read-btn").addEventListener("click", async () => {
  try {
    const data = await api("/api/bkpt");
    $("bkpt-addr").value = "0x" + data.addr.toString(16).toUpperCase().padStart(4, "0");
    log(`BKPT = 0x${data.addr.toString(16).toUpperCase()}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("bkpt-write-btn").addEventListener("click", async () => {
  const addr = parseNum($("bkpt-addr").value);
  try {
    await api("/api/bkpt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ addr }),
    });
    log(`BKPT set to 0x${addr.toString(16).toUpperCase()}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("step-btn").addEventListener("click", async () => {
  try {
    await api("/api/step", { method: "POST" });
    log("stepped one instruction", "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("tagged-go-btn").addEventListener("click", async () => {
  try {
    await api("/api/tagged_go", { method: "POST" });
    log("resumed with breakpoint tagging armed (TAGGO)", "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("control-read-btn").addEventListener("click", async () => {
  try {
    const data = await api("/api/control");
    $("control-value").value = "0x" + data.value.toString(16).toUpperCase().padStart(2, "0");
    log(`BDCSCR = 0x${data.value.toString(16).toUpperCase()}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("control-write-btn").addEventListener("click", async () => {
  const value = parseNum($("control-value").value);
  try {
    await api("/api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    log(`wrote BDCSCR = 0x${value.toString(16).toUpperCase()}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

// -- Scope / self-capture ---------------------------------------------------

$("scope-test").addEventListener("change", () => {
  const showBit = $("scope-test").value === "write_bit";
  $("scope-bit-label").style.display = showBit ? "" : "none";
  $("scope-bit").style.display = showBit ? "" : "none";
});

function drawWaveform(samples, samplePeriodNs) {
  const canvas = $("scope-canvas");
  const ctx = canvas.getContext("2d");
  const W = canvas.width;
  const H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  // grid
  ctx.strokeStyle = "#1c212a";
  ctx.lineWidth = 1;
  for (let x = 0; x < W; x += 60) {
    ctx.beginPath();
    ctx.moveTo(x + 0.5, 0);
    ctx.lineTo(x + 0.5, H);
    ctx.stroke();
  }

  if (!samples || samples.length === 0) return;

  const padTop = 20;
  const padBottom = 20;
  const highY = padTop;
  const lowY = H - padBottom;
  const stepX = W / samples.length;

  ctx.strokeStyle = "#e8a33d";
  ctx.lineWidth = 2;
  ctx.beginPath();
  let x = 0;
  let prevLevel = samples[0];
  ctx.moveTo(0, prevLevel ? highY : lowY);
  for (let i = 0; i < samples.length; i++) {
    const level = samples[i];
    const y = level ? highY : lowY;
    if (level !== prevLevel) {
      ctx.lineTo(x, prevLevel ? highY : lowY);
      ctx.lineTo(x, y);
    }
    x += stepX;
    prevLevel = level;
  }
  ctx.lineTo(x, prevLevel ? highY : lowY);
  ctx.stroke();

  // level labels
  ctx.fillStyle = "#838d9e";
  ctx.font = "11px JetBrains Mono, monospace";
  ctx.fillText("1", 4, highY + 4);
  ctx.fillText("0", 4, lowY + 4);

  // time axis (total window)
  const totalNs = samples.length * samplePeriodNs;
  const totalUs = (totalNs / 1000).toFixed(2);
  ctx.fillText(`${totalUs} \u00b5s total window`, W - 140, H - 6);
}

$("scope-run-btn").addEventListener("click", async () => {
  const test = $("scope-test").value;
  const bit = parseInt($("scope-bit").value, 10);
  const sampleCount = parseInt($("opt-sample-count").value, 10) || 256;
  try {
    const data = await api("/api/capture", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ test, sample_count: sampleCount, bit }),
    });
    drawWaveform(data.samples, data.sample_period_ns);
    let readout = `${data.samples.length} samples @ ${data.sample_period_ns} ns/sample`;
    if (data.bit_value !== null && data.bit_value !== undefined) {
      readout += ` — read bit = ${data.bit_value}`;
    }
    $("scope-readout").textContent = readout;
    log(`capture (${test}) complete`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("log-clear-btn").addEventListener("click", () => {
  logEl.innerHTML = "";
});

refreshPorts().catch((e) => log(e.message, "err"));
