const $ = (id) => document.getElementById(id);

const dot = $("conn-dot");
const logEl = $("log");

function log(msg, cls) {
  const line = document.createElement("div");
  if (cls) line.className = cls;
  const ts = new Date().toLocaleTimeString();
  line.textContent = `[${ts}] ${msg}`;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
}

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
  $("scope-run-btn").disabled = !connected;
}

function setSynced(synced) {
  $("mem-read-btn").disabled = !synced;
  $("wb-btn").disabled = !synced;
  $("flash-btn").disabled = !synced;
  $("mass-erase-btn").disabled = !synced;
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

$("flash-btn").addEventListener("click", async () => {
  const fileInput = $("srec-file");
  if (!fileInput.files.length) return log("choose a .s19 file first", "err");
  const busFreq = $("bus-freq").value;

  const form = new FormData();
  form.append("file", fileInput.files[0]);
  form.append("bus_freq_hz", busFreq);

  try {
    log(`programming ${fileInput.files[0].name}...`);
    const res = await fetch("/api/flash_srec", { method: "POST", body: form });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    $("flash-output").textContent = JSON.stringify(data.chunks, null, 2);
    log(`programmed ${data.total_bytes} bytes across ${data.chunks.length} region(s)`, "ok");
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
  try {
    const data = await api("/api/capture", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ test, sample_count: 256, bit }),
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

refreshPorts().catch((e) => log(e.message, "err"));
