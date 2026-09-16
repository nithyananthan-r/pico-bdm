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

// -- formatting helpers --------------------------------------------------
const hex = (n, w = 2) =>
  n === null || n === undefined
    ? "—"
    : "0x" + n.toString(16).toUpperCase().padStart(w, "0");
const addr16 = (n) =>
  n === null || n === undefined
    ? "—"
    : "$" + n.toString(16).toUpperCase().padStart(4, "0");

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function fact(parent, key, value, cls) {
  const f = el("div", "fact" + (cls ? " " + cls : ""));
  f.appendChild(el("div", "k", key));
  f.appendChild(el("div", "v", value));
  parent.appendChild(f);
  return f;
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

// -- advanced (Memory/Scope/Registers/Options) toggle ---------------------
const ADVANCED_KEY = "pico-bdm-advanced";
const benchEl = document.querySelector(".bench");
const advancedToggle = $("advanced-toggle");
function setAdvanced(show) {
  benchEl.classList.toggle("show-advanced", show);
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

// Every state-changing request must carry this header. A cross-origin page
// cannot set it without triggering a CORS preflight this server will not
// satisfy, so it's what stops a malicious tab from POSTing /api/mass_erase.
// Must match CSRF_HEADER/CSRF_VALUE in app.py.
const CSRF_HEADER = "X-Requested-With";
const CSRF_VALUE = "pico-bdm";

async function api(path, opts) {
  const o = Object.assign({}, opts);
  o.headers = Object.assign({}, o.headers || {}, { [CSRF_HEADER]: CSRF_VALUE });
  const res = await fetch(path, o);
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "request failed");
  return data;
}

// Same, but hand back the body even on a failure: several routes answer 400
// with a FULL report (which pages verified before the failure, which wipe
// step failed). Throwing that away and showing only the error string is
// exactly the "generic flash failed" message this UI is meant not to show.
async function apiRaw(path, opts) {
  const o = Object.assign({}, opts);
  o.headers = Object.assign({}, o.headers || {}, { [CSRF_HEADER]: CSRF_VALUE });
  const res = await fetch(path, o);
  return await res.json();
}

function jsonPost(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

// Single numeric parser used by EVERY user-supplied numeric field in this
// UI: decimal unless explicitly prefixed with 0x.
function parseNum(s) {
  const t = String(s).trim();
  const n = parseInt(t, t.toLowerCase().startsWith("0x") ? 16 : 10);
  if (Number.isNaN(n)) throw new Error(`"${s}" is not a number`);
  return n;
}

// =======================================================================
// Session state
// =======================================================================
let connected = false;
let synced = false;
let identity = null;      // last /api/identify payload
let flashStart = 0xe000;  // corrected from identity once the part is probed
let ramBytes = 512;
let badCells = null;      // measured, never assumed -- see Finding 106
let controlHasBeenRead = false;
let flashing = false;

function setConnected(v) {
  connected = v;
  dot.classList.toggle("connected", v);
  $("disconnect-btn").disabled = !v;
  $("connect-btn").disabled = v;
  $("sync-btn").disabled = !v;
  $("relink-btn").disabled = !v;
  $("reset-target-btn").disabled = !v;
  $("power-btn").disabled = !v;
  $("scope-run-btn").disabled = !v;
  if (!v) setSynced(false);
}

function setSynced(v) {
  synced = v;
  if (!v) controlHasBeenRead = false;
  const off = !v;
  [
    "mem-read-btn", "wb-btn", "flash-btn", "verify-btn", "dump-btn",
    "mass-erase-btn", "blank-check-btn", "go-btn", "halt-btn", "step-btn",
    "tagged-go-btn", "bkpt-write-btn", "bkpt-clear-btn", "bkpt-read-btn",
    "reg-read-btn", "reg-write-btn", "control-read-btn",
    "identify-btn", "deep-probe-btn", "sec-read-btn", "sec-lock-btn",
    "sec-wipe-btn", "memmap-scan-btn", "memmap-scanram-btn",
  ].forEach((id) => { $(id).disabled = off; });
  $("control-write-btn").disabled = off || !controlHasBeenRead;
  if (!v) {
    stopPolling();
    $("poll-btn").disabled = true;
  } else {
    $("poll-btn").disabled = false;
  }
}

// =======================================================================
// Connection
// =======================================================================
async function refreshPorts() {
  const data = await api("/api/ports");
  const select = $("port-select");
  const previous = select.value;
  select.innerHTML = "";
  for (const p of data.ports) {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = p;
    select.appendChild(opt);
  }
  if (previous && data.ports.includes(previous)) select.value = previous;
  if (data.ports.length === 0) log("no serial ports found", "err");
}

$("refresh-ports").addEventListener("click", () =>
  refreshPorts().catch((e) => log(e.message, "err"))
);

$("connect-btn").addEventListener("click", async () => {
  const port = $("port-select").value;
  if (!port) return log("no port selected", "err");
  try {
    const d = await jsonPost("/api/connect", { port });
    setConnected(true);
    log(
      d.already_connected
        ? `already connected to ${port} (the server never dropped it)`
        : `connected to ${port}`,
      "ok"
    );
    // The server may still be synced from before a page reload. Probe for
    // it rather than assuming either way -- if the link answers, the chip
    // panel fills in immediately, which is the point of this panel.
    await probeExistingLink();
  } catch (e) {
    log(e.message, "err");
  }
});

async function probeExistingLink() {
  try {
    const info = await api("/api/chip_info?identify=1&ram_probe=1");
    if (info.id_ok) {
      setSynced(true);
      $("sync-readout").textContent = info.bus_clock_hz
        ? `link live — ${(info.bus_clock_hz / 1e6).toFixed(3)} MHz`
        : "link live";
      renderChipInfo(info);
      log(
        "the BDC link is already live (the server was synced before this " +
          "page loaded) — Sync target if FLASH operations report a missing " +
          "clock divider",
        "warn"
      );
      return true;
    }
  } catch (e) {
    /* expected when the target has not been entered yet */
  }
  $("chip-sub").textContent = "sync the target to identify the part";
  return false;
}

$("disconnect-btn").addEventListener("click", async () => {
  try {
    await api("/api/disconnect", { method: "POST" });
    setConnected(false);
    $("sync-readout").textContent = "not synced";
    log("disconnected");
  } catch (e) {
    log(e.message, "err");
  }
});

$("sync-btn").addEventListener("click", async () => {
  dot.classList.add("working");
  try {
    const data = await jsonPost("/api/sync");
    const mhz = (data.target_freq_hz / 1e6).toFixed(3);
    $("sync-readout").textContent = `target clock ~${mhz} MHz`;
    setSynced(true);
    $("bus-freq").value = String(Math.round(data.target_freq_hz));
    log(`synced — target clock ~${mhz} MHz (bus clock field updated)`, "ok");
    await loadChipInfo(false);
    await readSecurity(true);
  } catch (e) {
    log(e.message, "err");
  } finally {
    dot.classList.remove("working");
  }
});

$("relink-btn").addEventListener("click", relink);
$("link-relink-btn").addEventListener("click", relink);

async function relink() {
  dot.classList.add("working");
  try {
    const d = await jsonPost("/api/relink");
    if (d.validated) {
      setSynced(true);
      const mhz = (d.bit_clock_hz / 1e6).toFixed(3);
      $("sync-readout").textContent = `relinked — ${mhz} MHz`;
      $("bus-freq").value = String(d.bit_clock_hz);
      log(
        `relinked at ${mhz} MHz without resetting the target ` +
          `(SYNC measured ${(d.sync_raw_hz / 1e6).toFixed(3)} MHz raw)`,
        "ok"
      );
      $("link-banner").hidden = true;
    } else {
      log(
        "relink did NOT validate — SDIDL never read back 0x14, at the " +
          "measured rate or half of it. A power-on re-entry (Sync target) " +
          "is the remaining option, but it restarts the target's program.",
        "err"
      );
    }
  } catch (e) {
    log(e.message, "err");
  } finally {
    dot.classList.remove("working");
  }
}

$("reset-target-btn").addEventListener("click", async () => {
  try {
    await jsonPost("/api/reset_target");
    setSynced(false);
    $("sync-readout").textContent = "not synced";
    log("target reset (plain RESET pulse — running its own code)", "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("power-btn").addEventListener("click", async () => {
  try {
    const p = await api("/api/power");
    const out = $("power-readout");
    out.hidden = false;
    out.textContent =
      `VDD driven from GPIO${p.vdd_pin} — currently ` +
      `${p.vdd_driven_high === null ? "unknown" : p.vdd_driven_high ? "HIGH (on)" : "LOW (off)"}\n` +
      `BKGD GPIO${p.bkgd_pin}   RESET GPIO${p.reset_pin}\n` +
      `voltage sensing: NOT AVAILABLE — ${p.reason}\n` +
      `witness: ${p.witness}`;
    log("read target power state", "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

// =======================================================================
// Chip identification
// =======================================================================
async function loadChipInfo(deep) {
  dot.classList.add("working");
  $("chip-sub").textContent = deep
    ? "probing RAM, scanning this die for defective cells, blank-checking…"
    : "probing…";
  try {
    const q = deep
      ? "/api/chip_info?identify=1&ram_probe=1&defect_scan=1&blank_check=1"
      : "/api/chip_info?identify=1&ram_probe=1";
    const info = await api(q);
    renderChipInfo(info);
    log(
      `identified ${info.identity?.name || info.identity?.family || "unknown part"}` +
        (deep ? " (deep probe)" : ""),
      "ok"
    );
  } catch (e) {
    $("chip-sub").textContent = "identification failed — " + e.message;
    log(e.message, "err");
  } finally {
    dot.classList.remove("working");
  }
}

$("identify-btn").addEventListener("click", () => loadChipInfo(false));
$("deep-probe-btn").addEventListener("click", () => loadChipInfo(true));

function renderChipInfo(info) {
  const id = info.identity || {};
  identity = id;

  const name = id.name || id.family || "unknown part";
  const nameEl = $("chip-name");
  nameEl.textContent = name;
  nameEl.classList.toggle("unknown", !id.name);
  $("header-chip").textContent = id.name ? id.name : "";

  const conf = id.confidence || "id-only";
  let sub = "";
  if (conf === "probed" && id.evidence) {
    sub = `identity PROBED, not assumed — ${id.evidence}`;
  } else if (conf === "id-only") {
    sub =
      "SDID identifies the family only; the SG8 and SG4 share one part ID. " +
      "Press Identify to probe RAM and settle which one is in the socket.";
  } else {
    sub = "no answer from the part — the link read the idle pull-up";
  }
  $("chip-sub").textContent = sub;

  if (id.flash_start !== undefined && id.flash_start !== null) {
    flashStart = id.flash_start;
  }
  if (id.ram_bytes) ramBytes = id.ram_bytes;

  const g = $("chip-facts");
  g.innerHTML = "";
  fact(g, "part ID", `${id.part_id_hex || hex(info.part_id, 3)} (SDID)`);
  fact(g, "mask rev", hex(info.rev ?? id.rev, 1));
  fact(
    g, "ID check",
    info.id_ok ? "SDIDL = 0x14 ✓" : "unexpected SDID",
    info.id_ok ? "good" : "bad"
  );
  if (id.flash_kb) fact(g, "FLASH", `${id.flash_kb} KB @ ${addr16(id.flash_start)}`);
  if (id.ram_bytes) fact(g, "RAM", `${id.ram_bytes} B @ $0060`);

  const sec = info.security || {};
  fact(
    g, "security",
    sec.secured ? "LOCKED" : "unsecured",
    sec.secured ? "bad" : "good"
  );
  fact(g, "FOPT / NVOPT", `${hex(sec.fopt)} / ${hex(sec.nvopt)}`);

  const rs = info.reset_source || {};
  const sources = Object.keys(rs).filter((k) => rs[k]);
  fact(
    g, "reset source",
    sources.length
      ? sources.map((s) => s.toUpperCase()).join(" + ")
      : "none flagged",
    "warnv"
  );
  fact(g, "SRS", hex(info.srs));

  if (info.blank === true) fact(g, "blank check", "chip is ERASED", "warnv");
  else if (info.blank === false) fact(g, "blank check", "programmed", "good");
  else if (info.blank_error) fact(g, "blank check", "failed: " + info.blank_error, "bad");
  else fact(g, "blank check", "not run (Deep probe)", "na");

  fact(
    g, "bus clock",
    info.bus_clock_hz
      ? (info.bus_clock_hz / 1e6).toFixed(3) + " MHz"
      : "unknown (CLKSW=0)",
    info.bus_clock_hz ? undefined : "na"
  );
  fact(g, "FCDIV / FPROT", `${hex(info.fcdiv)} / ${hex(info.fprot)}`);
  fact(g, "FSTAT", hex(info.fstat));
  fact(g, "BDCSCR", hex(info.bdcscr));

  if (id.bad_ram_cells) {
    badCells = id.bad_ram_cells;
    fact(
      g, "defective RAM cells",
      badCells.length
        ? `${badCells.length} in $0060–$006F (measured now)`
        : "none found in $0060–$006F",
      badCells.length ? "bad" : "good"
    );
    $("chip-note").textContent =
      badCells.length
        ? "defect map is measured live on every deep probe — this die's " +
          "damage has moved between sessions, so nothing here is hardcoded"
        : "";
    markBadCells();
  }

  renderSecurity(sec);
  buildMemMap();
}

// =======================================================================
// Memory map
// =======================================================================
const mapBody = $("map-body");
let mapRegions = [];   // {key,name,start,end,gran,cells:[{elt,start,end}],rows:[]}
const scanned = {};    // key -> {start, bytes:Uint8Array}

function regionSpec() {
  return [
    { key: "reg", name: "Direct-page registers", start: 0x0000, end: 0x005f,
      gran: 1, cls: "rg-reg", cols: 32 },
    { key: "ram", name: "RAM", start: 0x0060, end: 0x0060 + ramBytes - 1,
      gran: 1, cls: "rg-ram", cols: 32 },
    { key: "hreg", name: "High-page registers", start: 0x1800, end: 0x185f,
      gran: 1, cls: "rg-reg", cols: 32 },
    { key: "flash", name: "FLASH", start: flashStart, end: 0xffff,
      gran: 16, cls: "rg-flash", cols: 32, pages: true },
  ];
}

function subRegionClass(start, end) {
  // NVM option / backdoor key block, then the vector table. Both live
  // inside FLASH and both matter enough to be told apart on sight.
  if (start <= 0xffbf && end >= 0xffb0) return "rg-nvm";
  if (start >= 0xffc0) return "rg-vector";
  return null;
}

function buildMemMap() {
  mapBody.innerHTML = "";
  mapRegions = [];
  const legend = $("map-legend");
  legend.innerHTML = "";
  [
    ["rg-reg", "registers"], ["rg-ram", "RAM"], ["rg-flash", "FLASH"],
    ["rg-nvm scanned", "NVM option / backdoor key $FFB0–$FFBF"],
    ["rg-vector scanned", "vector table $FFC0–$FFFF"],
    ["scanned blank", "blank 0xFF (read back)"],
    ["scanned zero", "all 0x00 (read back)"],
    ["bad", "defective cell (measured on this die)"],
    ["written", "programmed in this run"],
  ].forEach(([cls, text]) => {
    const s = el("span");
    const i = el("i", "mc " + cls);
    i.style.opacity = 1;
    s.appendChild(i);
    s.appendChild(document.createTextNode(text));
    legend.appendChild(s);
  });

  for (const spec of regionSpec()) {
    const region = Object.assign({}, spec, { cells: [], rows: [] });
    const wrap = el("div", "map-region");
    const head = el("div", "map-region-head");
    head.appendChild(el("span", "rt", region.name));
    head.appendChild(
      el("span", "rr",
        `${addr16(region.start)}–${addr16(region.end)} · ` +
        `${region.end - region.start + 1} B · ` +
        `${region.gran} B/cell`)
    );
    wrap.appendChild(head);

    const rows = el("div", "map-rows");
    const rowBytes = region.cols * region.gran;
    for (let base = region.start; base <= region.end; base += rowBytes) {
      const row = el("div", "map-row");
      row.appendChild(el("div", "rlabel", addr16(base)));
      const rc = el("div", "rcells");
      const rowCells = [];
      for (let c = 0; c < region.cols; c++) {
        const cs = base + c * region.gran;
        if (cs > region.end) break;
        const ce = Math.min(cs + region.gran - 1, region.end);
        const cell = el("div", "mc " + (subRegionClass(cs, ce) || region.cls));
        cell.dataset.start = cs;
        cell.dataset.end = ce;
        rc.appendChild(cell);
        const rec = { elt: cell, start: cs, end: ce };
        rowCells.push(rec);
        region.cells.push(rec);
      }
      row.appendChild(rc);
      rows.appendChild(row);
      region.rows.push({ elt: row, base, cells: rowCells });
    }
    wrap.appendChild(rows);
    mapBody.appendChild(wrap);
    mapRegions.push(region);
  }
  markBadCells();
  applyAllScans();
}

mapBody.addEventListener("mouseover", (e) => {
  const c = e.target.closest(".mc");
  if (!c) return;
  const s = +c.dataset.start;
  const en = +c.dataset.end;
  let txt = s === en ? addr16(s) : `${addr16(s)}–${addr16(en)}`;
  const note = c.dataset.note;
  if (note) txt += "  " + note;
  else txt += "  (not read yet — press Scan to colour by actual content)";
  $("map-tip").textContent = txt;
});

function cellsIn(lo, hi) {
  const out = [];
  for (const r of mapRegions) {
    if (hi < r.start || lo > r.end) continue;
    for (const c of r.cells) {
      if (c.end >= lo && c.start <= hi) out.push(c);
    }
  }
  return out;
}

function markBadCells() {
  if (!badCells) return;
  for (const b of badCells) {
    for (const c of cellsIn(b.addr, b.addr)) {
      c.elt.classList.add("bad");
      c.elt.dataset.note =
        `DEFECTIVE: writing 0xFF reads back ${hex(b.mask)} ` +
        `(fixed AND mask on this die, measured this session)`;
    }
  }
}

function classifyBytes(bytes) {
  let allFF = true, allZero = true;
  for (const b of bytes) {
    if (b !== 0xff) allFF = false;
    if (b !== 0x00) allZero = false;
  }
  if (allFF) return "blank";
  if (allZero) return "zero";
  return "data";
}

function applyScan(key) {
  const s = scanned[key];
  if (!s) return;
  const region = mapRegions.find((r) => r.key === key);
  if (!region) return;
  for (const c of region.cells) {
    const off = c.start - s.start;
    if (off < 0 || off + (c.end - c.start) >= s.bytes.length) continue;
    const slice = s.bytes.subarray(off, off + (c.end - c.start) + 1);
    const kind = classifyBytes(slice);
    c.elt.classList.remove("blank", "zero", "data", "reading");
    c.elt.classList.add("scanned", kind);
    const preview = Array.from(slice.subarray(0, 8))
      .map((b) => b.toString(16).toUpperCase().padStart(2, "0"))
      .join(" ");
    c.elt.dataset.note =
      `${kind === "blank" ? "blank (all 0xFF)" : kind === "zero" ? "all 0x00 — a SECURED part reads like this" : "programmed"}` +
      `  [${preview}${slice.length > 8 ? " …" : ""}]`;
  }
}

function applyAllScans() {
  Object.keys(scanned).forEach(applyScan);
}

// Anything that changes the chip out from under a previous read has to
// throw that read away. Leaving stale bytes coloured in is the same class
// of lie as animating registers on a running target.
function discardScans(why) {
  Object.keys(scanned).forEach((k) => delete scanned[k]);
  buildMemMap();
  $("memmap-status").textContent = why + " — press Scan to read the chip back";
}

async function scanRange(key, start, len) {
  const d = await api(`/api/dump?addr=${start}&len=${len}&format=hex`);
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    bytes[i] = parseInt(d.hex.substr(i * 2, 2), 16);
  }
  const prev = scanned[key];
  if (prev && prev.start === start && prev.bytes.length >= len) {
    prev.bytes.set(bytes, 0);
  } else if (prev && prev.start <= start) {
    prev.bytes.set(bytes, start - prev.start);
  } else {
    scanned[key] = { start, bytes };
  }
  applyScan(key);
  return d;
}

async function scanRamAndRegs() {
  $("memmap-status").textContent = "reading registers and RAM…";
  await scanRange("reg", 0x0000, 0x60);
  await scanRange("ram", 0x0060, ramBytes);
  await scanRange("hreg", 0x1800, 0x60);
  $("memmap-status").textContent =
    `registers + RAM read at ${new Date().toLocaleTimeString()} — FLASH not scanned`;
}

$("memmap-scanram-btn").addEventListener("click", async () => {
  if (flashing) return log("a programming job is running — scan afterwards", "err");
  disableScan(true);
  try {
    await scanRamAndRegs();
    log("scanned registers + RAM into the memory map", "ok");
  } catch (e) {
    $("memmap-status").textContent = "scan failed — " + e.message;
    log(e.message, "err");
  } finally {
    disableScan(false);
  }
});

$("memmap-scan-btn").addEventListener("click", async () => {
  if (flashing) return log("a programming job is running — scan afterwards", "err");
  disableScan(true);
  const flashRegion = mapRegions.find((r) => r.key === "flash");
  try {
    await scanRamAndRegs();
    // FLASH one page at a time: ~1.6 ms/byte on this link, so a page is
    // about 0.8 s. Reading it page-wise means the map fills in as the data
    // actually arrives instead of freezing for 13 s and then jumping.
    scanned.flash = {
      start: flashRegion.start,
      bytes: new Uint8Array(flashRegion.end - flashRegion.start + 1),
    };
    const pages = flashRegion.rows.length;
    for (let i = 0; i < pages; i++) {
      const row = flashRegion.rows[i];
      const base = row.base;
      const len = Math.min(512, flashRegion.end - base + 1);
      row.cells.forEach((c) => c.elt.classList.add("reading"));
      $("memmap-status").textContent =
        `reading FLASH ${addr16(base)} — page ${i + 1} of ${pages}`;
      await scanRange("flash", base, len);
      row.cells.forEach((c) => c.elt.classList.remove("reading"));
    }
    $("memmap-status").textContent =
      `whole chip read at ${new Date().toLocaleTimeString()} — cells coloured by actual content`;
    log("scanned the whole address space into the memory map", "ok");
  } catch (e) {
    $("memmap-status").textContent = "scan failed — " + e.message;
    log(e.message, "err");
    mapCells(".mc.reading").forEach((c) =>
      c.classList.remove("reading")
    );
  } finally {
    disableScan(false);
  }
});

function disableScan(v) {
  $("memmap-scan-btn").disabled = v || !synced;
  $("memmap-scanram-btn").disabled = v || !synced;
}

// =======================================================================
// Live state & debugger
// =======================================================================
let pollTimer = null;
let pollBusyStreak = 0;

function pollInterval() {
  const v = parseInt($("opt-poll-ms").value, 10);
  return Number.isFinite(v) && v >= 80 ? v : 250;
}

function startPolling() {
  if (pollTimer) return;
  $("poll-btn").textContent = "Stop polling";
  $("poll-btn").classList.add("on");
  const tick = async () => {
    await pollOnce();
    if (pollTimer) pollTimer = setTimeout(tick, pollInterval());
  };
  pollTimer = setTimeout(tick, 0);
}

function stopPolling() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
  $("poll-btn").textContent = "Start polling";
  $("poll-btn").classList.remove("on");
  $("poll-rate").textContent = "stopped";
  document.querySelectorAll(".pin").forEach((p) => p.classList.add("stale"));
}

$("poll-btn").addEventListener("click", () =>
  pollTimer ? stopPolling() : startPolling()
);

async function pollOnce() {
  try {
    const s = await api("/api/live_state");
    if (s.busy) {
      pollBusyStreak++;
      $("poll-rate").textContent =
        "link busy with another operation — not polling (" + pollBusyStreak + ")";
      document.querySelectorAll(".pin").forEach((p) => p.classList.add("stale"));
      return;
    }
    pollBusyStreak = 0;
    renderLiveState(s);
  } catch (e) {
    $("poll-rate").textContent = "poll error: " + e.message;
    showLinkLost(e.message);
  }
}

function showLinkLost(msg) {
  const b = $("link-banner");
  b.hidden = false;
  $("link-banner-msg").textContent = msg;
  const pill = $("run-state");
  pill.textContent = "link lost";
  pill.className = "state-pill lost";
  // Honesty rule 2: a dead link is NOT rendered as data. Blank the register
  // and pin views rather than leaving the last good values on screen looking
  // current, and stop any pin animation.
  $("reg-grid").innerHTML = "";
  $("regs-note").textContent = "no link — nothing to show";
  document.querySelectorAll(".pin").forEach((p) => p.classList.add("stale"));
  $("bdcscr-readout").textContent = "BDCSCR — (no answer)";
  $("bdcscr-bits").innerHTML = "";
}

function renderLiveState(s) {
  if (!s.link_ok) {
    showLinkLost(
      s.link_error ||
        "the target stopped answering at the current bit rate"
    );
    return;
  }
  $("link-banner").hidden = true;
  $("poll-rate").textContent = `polling every ${pollInterval()} ms`;

  const sc = s.bdcscr || {};
  const pill = $("run-state");
  pill.textContent = s.halted ? "halted (active BDM)" : "running";
  pill.className = "state-pill " + (s.halted ? "halted" : "running");
  $("bdcscr-readout").textContent = `BDCSCR ${hex(sc.value)}`;

  const bits = $("bdcscr-bits");
  bits.innerHTML = "";
  [
    ["ENBDM", sc.enbdm, false], ["BDMACT", sc.bdmact, false],
    ["BKPTEN", sc.bkpten, false], ["FTS", sc.fts, false],
    ["CLKSW", sc.clksw, false], ["WS", sc.ws, true],
    ["WSF", sc.wsf, true], ["DVF", sc.dvf, true],
  ].forEach(([n, on, sticky]) => {
    const b = el("div", "bit" + (on ? " on" + (sticky ? " sticky" : "") : ""), n);
    b.title = sticky
      ? "sticky status flag — set by a failed/aborted transfer, cleared by writing BDCSCR"
      : "";
    bits.appendChild(b);
  });

  if (s.bkpt) {
    $("bkpt-readout").textContent =
      `${addr16(s.bkpt.addr)} ${s.bkpt.enabled ? "ARMED" : "disarmed"}` +
      (s.bkpt.enabled ? (s.bkpt.tag_mode ? " (tag)" : " (force)") : "");
  }

  renderRegs(s.cpu_regs, s.halted);
  renderPorts(s.ports);
}

function renderRegs(regs, halted) {
  const g = $("reg-grid");
  g.innerHTML = "";
  if (!regs) {
    $("regs-note").textContent = "not requested";
    return;
  }
  $("regs-note").textContent = halted
    ? "CPU halted in background mode — these are real"
    : "UNAVAILABLE while the CPU is executing (BDCSCR.BDMACT=0)";
  for (const name of ["PC", "A", "CCR", "HX", "SP"]) {
    const r = regs[name];
    if (!r) continue;
    const width = name === "A" || name === "CCR" ? 2 : 4;
    const box = el("div", "reg");
    box.appendChild(el("div", "rn", name === "HX" ? "H:X" : name));
    if (!r.available) {
      // Never a number here. Showing one would be showing noise as data.
      box.classList.add("unavailable");
      box.appendChild(el("div", "rv", "unavailable"));
      box.appendChild(
        el("div", "rnote",
          halted ? r.reason || "" : "target is running")
      );
      box.title = r.reason || "";
    } else {
      box.appendChild(el("div", "rv", hex(r.value, width)));
      if (r.ambiguous) {
        box.classList.add("ambiguous");
        box.appendChild(el("div", "rnote", "all-ones — also what a silent link returns"));
        box.title = r.reason || "";
      }
    }
    g.appendChild(box);
  }
}

function renderPorts(ports) {
  const g = $("port-grid");
  if (!ports) {
    g.innerHTML = "<span class='hint'>ports not requested</span>";
    return;
  }
  for (const pname of ["A", "B", "C"]) {
    const pins = ports[pname];
    if (!pins) continue;
    let row = g.querySelector(`[data-port="${pname}"]`);
    if (!row) {
      row = el("div", "port-row");
      row.dataset.port = pname;
      row.appendChild(el("div", "pname", "PT" + pname));
      for (let i = 7; i >= 0; i--) {
        const p = el("div", "pin");
        p.dataset.bit = i;
        p.appendChild(el("div", "lv", "-"));
        p.appendChild(el("div", "dir", ""));
        row.appendChild(p);
      }
      g.appendChild(row);
    }
    for (const pin of pins) {
      const i = parseInt(pin.pin.slice(-1), 10);
      const e = row.querySelector(`.pin[data-bit="${i}"]`);
      e.classList.remove("stale");
      e.classList.toggle("hi", pin.level === 1);
      e.classList.toggle("out", pin.direction === "out");
      e.querySelector(".lv").textContent = String(pin.level);
      e.querySelector(".dir").textContent = pin.direction;
      e.title = `${pin.pin} — ${pin.direction === "out" ? "output driving" : "input reading"} ${pin.level}`;
    }
  }
  if (!g.querySelector(".port-note")) {
    const n = el("span", "hint port-note", ports.note || "");
    g.appendChild(n);
  }
}

// -- debugger controls ----------------------------------------------------
$("halt-btn").addEventListener("click", async () => {
  try {
    const d = await jsonPost("/api/halt");
    log(
      d.halted
        ? `halted — BDCSCR ${hex(d.bdcscr)} (BDMACT=1, active background mode)`
        : `BACKGROUND sent but BDCSCR reads ${hex(d.bdcscr)} — BDMACT is NOT ` +
          `set, so the CPU did not halt (ENBDM must already be 1)`,
      d.halted ? "ok" : "err"
    );
    if (!pollTimer) await pollOnce();
  } catch (e) {
    log(e.message, "err");
  }
});

$("go-btn").addEventListener("click", async () => {
  try {
    await jsonPost("/api/go");
    log("target resumed (GO)", "ok");
    if (!pollTimer) await pollOnce();
  } catch (e) {
    log(e.message, "err");
  }
});

$("step-btn").addEventListener("click", async () => {
  try {
    await jsonPost("/api/step");
    const s = await api("/api/live_state");
    if (s.link_ok && s.cpu_regs && s.cpu_regs.PC && s.cpu_regs.PC.available) {
      log(`stepped one instruction (TRACE1) — PC = ${addr16(s.cpu_regs.PC.value)}`, "ok");
    } else {
      log("stepped one instruction (TRACE1)", "ok");
    }
    if (!s.busy) renderLiveState(s);
  } catch (e) {
    log(e.message, "err");
  }
});

$("tagged-go-btn").addEventListener("click", async () => {
  try {
    await jsonPost("/api/tagged_go");
    log("target resumed (TAGGO — identical to GO on HCS08)", "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("bkpt-write-btn").addEventListener("click", async () => {
  try {
    const addr = parseNum($("bkpt-addr").value);
    await jsonPost("/api/bkpt", { addr });
    log(
      `BKPT set to ${addr16(addr)} and armed (BKPTEN=1, force mode) — ` +
        `resume with Go to run until it is reached`,
      "ok"
    );
    if (!pollTimer) await pollOnce();
  } catch (e) {
    log(e.message, "err");
  }
});

$("bkpt-clear-btn").addEventListener("click", async () => {
  try {
    // BKPTEN is bit 5 of BDCSCR. Read-modify-write so CLKSW and ENBDM are
    // preserved -- clearing CLKSW would break the calibrated bit timing.
    const cur = await api("/api/control");
    const value = cur.value & ~0x20;
    await jsonPost("/api/control", { value });
    log(`breakpoint disarmed (BDCSCR ${hex(cur.value)} → ${hex(value)})`, "ok");
    if (!pollTimer) await pollOnce();
  } catch (e) {
    log(e.message, "err");
  }
});

// =======================================================================
// Programming
// =======================================================================
let progressTimer = null;
let plannedPageRows = [];

function flashPageRow(page) {
  const r = mapRegions.find((x) => x.key === "flash");
  if (!r) return null;
  return r.rows.find((row) => row.base === page) || null;
}

// Every cell query is scoped to #map-body on purpose: the legend is drawn
// with the same .mc classes so its swatches match the map exactly, and a
// document-wide query would strip the legend's own colours the first time a
// programming run cleaned up after itself.
function mapCells(sel) {
  return mapBody.querySelectorAll(sel);
}

function clearFlashDecor() {
  mapCells(".mc.queued, .mc.inflight, .mc.written, .mc.failedcell").forEach(
    (c) => {
      c.classList.remove("queued", "inflight", "written", "failedcell");
      c.style.animationDelay = "";
    }
  );
  mapCells(".map-row.page-inflight, .map-row.page-done, .map-row.page-failed")
    .forEach((r) =>
      r.classList.remove("page-inflight", "page-done", "page-failed")
    );
}

// Apply ONE page's confirmed report to the map. Split out because the live
// path and the synchronous path must draw the same thing from the same
// facts.
function applyPageResult(entry, chunks, stagger) {
  const row = flashPageRow(entry.page);
  if (!row) return;
  row.elt.classList.remove("page-inflight");
  const okPage = !!entry.verified;
  row.elt.classList.add(okPage ? "page-done" : "page-failed");

  const written = new Set(writtenCellsFor(entry.page, chunks).map((c) => c.elt));
  let i = 0;
  for (const c of row.cells) {
    c.elt.classList.remove("queued", "inflight");
    if (written.has(c.elt)) {
      if (stagger) c.elt.style.animationDelay = i * 16 + "ms";
      c.elt.classList.add(okPage ? "written" : "failedcell");
      c.elt.dataset.note = okPage
        ? "programmed and verified by the target in this run"
        : "this page failed to verify";
      i++;
    } else if (entry.erased) {
      // A page erase blanks the WHOLE 512-byte page, not just the bytes the
      // file covers. The target confirmed the erase, so these bytes are
      // known to be 0xFF now -- saying so is a measurement, not a guess.
      c.elt.classList.remove("data", "zero");
      c.elt.classList.add("scanned", "blank");
      c.elt.dataset.note =
        "blank 0xFF — erased by this run (a page erase blanks all 512 bytes, " +
        "not only the bytes the file covers)";
    }
  }
}

function writtenCellsFor(page, chunks) {
  // Which cells of this page's row correspond to bytes the file actually
  // covers. Derived from the chunk list the server reports for this job, so
  // the animation lights exactly the bytes being programmed and nothing
  // more.
  const row = flashPageRow(page);
  if (!row) return [];
  return row.cells.filter((c) =>
    (chunks || []).some((ch) => ch.addr <= c.end && ch.addr + ch.len - 1 >= c.start)
  );
}

$("flash-btn").addEventListener("click", async () => {
  const fileInput = $("srec-file");
  if (!fileInput.files.length) return log("choose a .s19 file first", "err");
  if (flashing) return log("a programming job is already running", "err");

  const form = new FormData();
  form.append("file", fileInput.files[0]);
  form.append("bus_freq_hz", $("bus-freq").value);
  form.append("erase_mode", $("erase-mode").value);
  const live = $("opt-live-progress").checked;
  if (live) form.append("async", "1");

  clearFlashDecor();
  $("flash-progress").hidden = false;
  $("page-list").innerHTML = "";
  $("flash-bar-fill").className = "flash-bar-fill";
  $("flash-bar-fill").style.width = "0%";
  $("flash-status").className = "flash-status";
  $("flash-status").textContent = "starting…";
  flashing = true;
  $("flash-btn").disabled = true;
  dot.classList.add("working");

  try {
    log(`programming ${fileInput.files[0].name}…`);
    // Raw fetch (FormData sets its own Content-Type boundary), so the CSRF
    // header has to be added by hand here.
    const res = await fetch("/api/flash_srec", {
      method: "POST",
      headers: { [CSRF_HEADER]: CSRF_VALUE },
      body: form,
    });
    const data = await res.json();
    if (!data.ok && !data.pages) throw new Error(data.error || "flash failed");

    if (live && data.async) {
      markPlanned(data.pages_planned);
      await watchProgress();
    } else {
      renderFlashReport(data);
      finishFlash(data.ok, data);
    }
  } catch (e) {
    $("flash-status").className = "flash-status failed";
    $("flash-status").textContent = e.message;
    log(e.message, "err");
    flashing = false;
    $("flash-btn").disabled = !synced;
    dot.classList.remove("working");
  }
});

function markPlanned(planned) {
  plannedPageRows = [];
  for (const p of planned || []) {
    const row = flashPageRow(p.page);
    if (!row) continue;
    plannedPageRows.push(row);
    row.cells.forEach((c) => c.elt.classList.add("queued"));
  }
}

function watchProgress() {
  return new Promise((resolve) => {
    const seen = new Set();
    const tick = async () => {
      let job;
      try {
        job = await api("/api/flash_progress");
      } catch (e) {
        $("flash-status").className = "flash-status failed";
        $("flash-status").textContent = "progress poll failed: " + e.message;
        return resolve();
      }
      const total = (job.pages_planned || []).length || 1;
      const done = (job.pages || []).length;

      // Confirmed pages: apply the target's OWN report for each.
      for (const entry of job.pages || []) {
        if (seen.has(entry.page)) continue;
        seen.add(entry.page);
        applyPageResult(entry, job.chunks, true);
        addPageLine(entry);
      }

      // The page currently ON THE WIRE. Nothing about its result is drawn:
      // only "this one is being worked on right now".
      if (job.current_page !== null && job.current_page !== undefined &&
          !seen.has(job.current_page)) {
        const row = flashPageRow(job.current_page);
        if (row && !row.elt.classList.contains("page-inflight")) {
          mapCells(".map-row.page-inflight").forEach((r) =>
            r.classList.remove("page-inflight")
          );
          row.elt.classList.add("page-inflight");
          writtenCellsFor(job.current_page, job.chunks).forEach((c) => {
            c.elt.classList.remove("queued");
            c.elt.classList.add("inflight");
          });
        }
      }

      $("flash-bar-fill").style.width = (100 * done) / total + "%";
      if (job.state === "running") {
        $("flash-status").textContent =
          job.phase === "mass_erase"
            ? "mass erasing the whole array…"
            : job.phase === "nvopt_restore"
            ? "restoring NVOPT at $FFBF (closing the self-secure window)…"
            : `page ${done + 1} of ${total} — ${addr16(job.current_page)} ` +
              `on the wire · ${job.total_bytes} B confirmed · ` +
              `${(job.elapsed || 0).toFixed(1)} s`;
        progressTimer = setTimeout(tick, 120);
        return;
      }

      // Terminal.
      mapCells(".mc.inflight").forEach((c) => {
        c.classList.remove("inflight", "queued");
      });
      mapCells(".map-row.page-inflight").forEach((r) =>
        r.classList.remove("page-inflight")
      );
      if (job.state === "failed") {
        const row = flashPageRow(job.failed_page);
        if (row) {
          row.elt.classList.add("page-failed");
          writtenCellsFor(job.failed_page, job.chunks).forEach((c) =>
            c.elt.classList.add("failedcell")
          );
        }
      }
      finishFlash(job.state === "done", job);
      resolve();
    };
    tick();
  });
}

function addPageLine(entry) {
  const li = el("li", entry.verified ? "ok" : "failed");
  li.appendChild(el("span", "pa", addr16(entry.page)));
  li.appendChild(
    el("span", "",
      `${entry.bytes} B · ${entry.erased ? "erased" : "not erased"} · ` +
      `programmed ${entry.programmed}` +
      (entry.nvopt_restored !== undefined
        ? ` · NVOPT→${hex(entry.nvopt_restored)}`
        : "") +
      ` · ${entry.verified ? "VERIFIED" : "VERIFY FAILED"}`)
  );
  if (entry.mismatches && entry.mismatches.length) {
    li.appendChild(
      el("span", "",
        " first mismatch " + addr16(entry.mismatches[0][0]) +
        ` wrote ${hex(entry.mismatches[0][1])} read ${hex(entry.mismatches[0][2])}`)
    );
  }
  $("page-list").appendChild(li);
}

async function finishFlash(ok, data) {
  flashing = false;
  $("flash-btn").disabled = !synced;
  dot.classList.remove("working");
  if (progressTimer) clearTimeout(progressTimer);
  progressTimer = null;
  // Any page that was planned but never reached (a run that stopped early)
  // must not stay outlined as if it were still coming.
  mapCells(".mc.queued, .mc.inflight").forEach((c) =>
    c.classList.remove("queued", "inflight")
  );

  const bar = $("flash-bar-fill");
  const st = $("flash-status");
  const pages = data.pages || [];
  const good = pages.filter((p) => p.verified);

  if (ok) {
    bar.className = "flash-bar-fill done";
    bar.style.width = "100%";
    st.className = "flash-status done";
    st.textContent =
      `complete — ${data.total_bytes ?? data.bytes_written ?? 0} bytes across ` +
      `${pages.length} page(s), every page verified on the target` +
      (data.elapsed ? ` · ${data.elapsed.toFixed(1)} s` : "");
    log(
      `programmed ${data.total_bytes ?? data.bytes_written} bytes across ` +
        `${pages.length} page(s); each page erased, programmed and verified ` +
        `before the next was touched`,
      "ok"
    );
  } else {
    bar.className = "flash-bar-fill failed";
    st.className = "flash-status failed";
    const failedAt = data.failed_page !== undefined && data.failed_page !== null
      ? addr16(data.failed_page)
      : (pages.find((p) => !p.verified)
          ? addr16(pages.find((p) => !p.verified).page)
          : "unknown page");
    st.textContent =
      `FAILED at page ${failedAt}. ${good.length} page(s) before it are ` +
      `confirmed erased, programmed and verified: ` +
      (good.map((p) => addr16(p.page)).join(", ") || "none") +
      `. ${data.error || ""}`;
    log(
      `programming FAILED at page ${failedAt} — ${data.error || "no error text"}`,
      "err"
    );
    log(
      `pages confirmed good before the failure: ` +
        (good.map((p) => addr16(p.page)).join(", ") || "none"),
      "warn"
    );
  }

  if (data.security_risk) {
    const w =
      "CHIP MAY BE LEFT IN A SELF-SECURING STATE: page $FE00 was erased and " +
      "NVOPT at $FFBF was not confirmed rewritten. DO NOT POWER-CYCLE THE " +
      "TARGET — rewrite $FFBF now (Security panel), or the part secures " +
      "itself at the next reset and needs a full wipe to recover.";
    st.textContent += " " + w;
    log(w, "err");
  }

  // Fold the real result into the map's content model so the cells that
  // were just programmed are shown as programmed, not as "unknown".
  const flashRegion = mapRegions.find((r) => r.key === "flash");
  if (flashRegion && data.chunks) {
    $("memmap-status").textContent =
      `FLASH pages updated from the programming report at ` +
      `${new Date().toLocaleTimeString()} — press Scan to read the whole chip back`;
  }

  $("flash-output").textContent = JSON.stringify(
    { chunks: data.chunks, pages }, null, 1
  );

  if (ok && $("opt-run-after").checked) {
    try {
      await jsonPost("/api/go");
      log("target resumed (GO)", "ok");
    } catch (e) {
      log(e.message, "err");
    }
  }
  await readSecurity(true).catch(() => {});
}

function renderFlashReport(data) {
  // Synchronous mode: the whole report arrives at once, so this is a replay
  // of measured results, not live progress. The page list says so.
  for (const entry of data.pages || []) {
    applyPageResult(entry, data.chunks, true);
    addPageLine(entry);
  }
  $("flash-bar-fill").style.width = "100%";
}

// -- verify --------------------------------------------------------------
$("verify-btn").addEventListener("click", async () => {
  const fileInput = $("srec-file");
  if (!fileInput.files.length)
    return log("choose the .s19 to verify against first", "err");
  const form = new FormData();
  form.append("file", fileInput.files[0]);
  dot.classList.add("working");
  try {
    log(`verifying against ${fileInput.files[0].name}…`);
    const data = await apiRaw("/api/verify", {
      method: "POST",
      headers: { [CSRF_HEADER]: CSRF_VALUE },
      body: form,
    });
    if (!data.ok && data.error) throw new Error(data.error);
    const out = $("flash-output");
    if (data.match) {
      out.textContent =
        `VERIFY OK — ${data.bytes_checked} bytes read back off the chip ` +
        `match the file exactly (independent read-back, not the programmer's ` +
        `own claim).`;
      log(`verify OK — ${data.bytes_checked} bytes match`, "ok");
    } else {
      out.textContent =
        `VERIFY FAILED — ${data.mismatch_count} of ${data.bytes_checked} ` +
        `bytes differ${data.truncated ? " (list capped at 64)" : ""}:\n` +
        data.mismatches
          .map(
            (m) =>
              `  ${addr16(m.addr)}  expected ${hex(m.expected)}  read ${hex(m.actual)}`
          )
          .join("\n");
      log(
        `verify FAILED — ${data.mismatch_count} byte(s) differ, first at ` +
          addr16(data.mismatches[0].addr),
        "err"
      );
    }
  } catch (e) {
    log(e.message, "err");
  } finally {
    dot.classList.remove("working");
  }
});

// -- dump / backup --------------------------------------------------------
$("dump-btn").addEventListener("click", async () => {
  const len = 0x10000 - flashStart;
  dot.classList.add("working");
  $("flash-output").textContent =
    `reading ${len} bytes from ${addr16(flashStart)} — about ` +
    `${(len * 0.0016).toFixed(0)} s at ~1.6 ms/byte…`;
  try {
    const d = await api(
      `/api/dump?addr=${flashStart}&len=${len}&format=hex&save=1`
    );
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) bytes[i] = parseInt(d.hex.substr(i * 2, 2), 16);
    scanned.flash = { start: flashStart, bytes };
    applyScan("flash");
    $("memmap-status").textContent =
      `FLASH read back at ${new Date().toLocaleTimeString()} (from the backup dump)`;
    $("flash-output").textContent =
      `dumped ${d.length} bytes from ${addr16(d.addr)}\n` +
      `saved to ${d.saved}\n` +
      (d.blank ? "every byte is 0xFF — the array is blank\n" : "") +
      (d.all_zero ? "WARNING: " + d.note + "\n" : "") +
      `first 32 bytes: ${d.hex.slice(0, 64).toUpperCase().match(/../g).join(" ")}`;
    log(`dumped ${d.length} bytes to ${d.saved}`, "ok");
  } catch (e) {
    $("flash-output").textContent = "dump failed — " + e.message;
    log(e.message, "err");
  } finally {
    dot.classList.remove("working");
  }
});

$("blank-check-btn").addEventListener("click", async () => {
  try {
    const data = await jsonPost("/api/blank_check");
    log(
      data.blank
        ? "blank check: the FLASH module reports the array erased"
        : "blank check: the FLASH module reports the array NOT blank",
      data.blank ? "ok" : "warn"
    );
  } catch (e) {
    log(e.message, "err");
  }
});

$("mass-erase-btn").addEventListener("click", () => {
  openConfirm({
    title: "Mass erase the FLASH array",
    body:
      "<p>This erases the whole FLASH array in one command. Anything " +
      "programmed on the part is gone.</p>" +
      "<div class='danger-note'>A mass erase also blanks NVOPT at $FFBF to " +
      "0xFF, which is SEC = 1:1 — <b>the part will secure itself at the next " +
      "reset</b> unless $FFBF is rewritten in this same power cycle. " +
      "Programming an image afterwards does that automatically; erasing and " +
      "walking away does not.</div>",
    word: "ERASE",
    steps: null,
    go: async () => {
      await jsonPost("/api/mass_erase");
      log("mass erase complete", "ok");
      await readSecurity(true);
      discardScans("FLASH was mass-erased — the previous read is stale");
      return { ok: true, text: "Mass erase complete." };
    },
  });
});

// =======================================================================
// Security / code protect
// =======================================================================
async function readSecurity(quiet) {
  try {
    const s = await api("/api/security");
    renderSecurity(s);
    if (!quiet) log(`security: ${s.secured ? "LOCKED" : "unsecured"}`, s.secured ? "warn" : "ok");
    return s;
  } catch (e) {
    if (!quiet) log(e.message, "err");
    return null;
  }
}

$("sec-read-btn").addEventListener("click", () => readSecurity(false));

function renderSecurity(s) {
  if (!s || s.fopt === undefined) return;
  const st = $("sec-state");
  st.textContent = s.secured ? "LOCKED — code protect active" : "UNSECURED — open";
  st.className = "sec-state " + (s.secured ? "locked" : "open");
  $("sec-sub").textContent = s.secured
    ? "every FLASH and RAM access over BDM returns 0x00 after a reset. The " +
      "only way back is a full wipe: mass erase + erase-verify."
    : "FLASH and RAM are readable over BDM. SEC bits come from FOPT ($1821), " +
      "a peripheral register that answers even on a locked part.";

  const g = $("sec-facts");
  g.innerHTML = "";
  fact(g, "FOPT ($1821)", hex(s.fopt));
  fact(g, "SEC bits", `${s.sec} (${s.secured ? "secured" : "unsecured"})`,
       s.secured ? "bad" : "good");
  fact(g, "NVOPT ($FFBF)", hex(s.nvopt),
       s.nvopt_trustworthy ? undefined : "na");
  fact(g, "NVOPT trustworthy",
       s.nvopt_trustworthy
         ? "yes"
         : "no — a locked part reads FLASH as 0x00",
       s.nvopt_trustworthy ? "good" : "warnv");
  fact(g, "KEYEN (backdoor)", s.keyen ? "enabled" : "disabled");
  fact(g, "FNORED", s.fnored ? "1" : "0");
  if (s.suspect_link) fact(g, "link", "SUSPECT — values may not be real", "bad");
}

$("sec-lock-btn").addEventListener("click", () => {
  openConfirm({
    title: "Lock the chip (set code protect)",
    body:
      "<p>This programs <b>NVOPT at $FFBF</b> to <b>0xFC</b> (SEC = 0:0). " +
      "After the next reset the part refuses every FLASH and RAM access over " +
      "BDM — reads come back 0x00.</p>" +
      "<p>0xFC is chosen deliberately: it is reachable by clearing bits, so " +
      "page $FE00 is <b>never erased</b> and there is no window in which a " +
      "power loss leaves the part self-securing.</p>" +
      "<div class='danger-note'>Recovering a locked part means <b>erasing " +
      "everything on it</b>. There is no unlock that keeps your program.<br>" +
      "The server takes an automatic backup of the FLASH array into " +
      "<code>host/backups/</code> before writing anything.</div>",
    word: "SECURE",
    steps: null,
    go: async () => {
      const d = await apiRaw("/api/security", {
        method: "POST",
        headers: { "Content-Type": "application/json", [CSRF_HEADER]: CSRF_VALUE },
        body: JSON.stringify({ confirm: "SECURE", backup: true }),
      });
      if (!d.ok) throw new Error(d.error || "failed to set security");
      await readSecurity(true);
      const b = d.backup || {};
      const text =
        `NVOPT ${hex(d.before)} → ${hex(d.wrote)}, read back ${hex(d.after)} ` +
        `(${d.written ? "verified" : "NOT VERIFIED"}).\n` +
        `Takes effect: ${d.takes_effect}.\n` +
        `FOPT now ${hex(d.fopt_now)} — still reads unsecured until that reset.\n` +
        (b.taken
          ? `Backup taken: ${b.path} (${b.length} bytes)`
          : `Backup NOT taken: ${b.reason}`);
      log(text.replace(/\n/g, " | "), d.written ? "warn" : "err");
      showSecResult(text, d.written);
      return { ok: true, text };
    },
  });
});

$("sec-wipe-btn").addEventListener("click", () => {
  openConfirm({
    title: "Unsecure & wipe — forgot-passcode recovery",
    body:
      "<p>This is the recovery for a chip you can no longer read: it " +
      "<b>mass-erases the entire FLASH array</b> and then runs the FLASH " +
      "module's own blank check.</p>" +
      "<p>Both steps are needed. On HCS08 a mass erase <b>alone does not " +
      "release security</b> — measured on this hardware, FOPT stays at 0xC0 " +
      "after the erase and only moves to 0xC2 when the erase-verify runs. " +
      "NVOPT is then restored to 0xFE, because a freshly erased NVOPT reads " +
      "0xFF = SEC 1:1 and would re-secure the part at the next reset.</p>" +
      "<div class='danger-note'>Everything on the chip is destroyed. The " +
      "server takes a backup first, but a part that is already locked reads " +
      "0x00 everywhere — there is nothing readable to back up, and it will " +
      "say so rather than writing a file full of zeros.</div>",
    word: "WIPE",
    steps: [
      ["read_security", "read the current security state"],
      ["flash_init_clock", "write FCDIV (volatile — required by every FLASH command)"],
      ["mass_erase", "mass erase the array (does NOT release security on its own)"],
      ["blank_check", "erase-verify — THIS is what releases security"],
      ["flash_readable", "confirm FLASH reads 0xFF again"],
      ["security_released", "confirm FOPT moved to unsecured"],
      ["restore_nvopt", "write NVOPT = 0xFE so it does not re-secure at reset"],
    ],
    go: async () => {
      const d = await apiRaw("/api/unsecure", {
        method: "POST",
        headers: { "Content-Type": "application/json", [CSRF_HEADER]: CSRF_VALUE },
        body: JSON.stringify({ confirm: "WIPE", backup: true }),
      });
      if (d.error && !d.steps) throw new Error(d.error);
      renderWipeSteps(d.steps || []);
      await readSecurity(true);
      const b = d.backup || {};
      const text =
        (d.complete
          ? "RECOVERED — the part is erased and unsecured."
          : "WIPE DID NOT COMPLETE: " + (d.error || "unknown failure")) +
        `\nsecured before: ${d.secured_before} → after: ${d.secured_after}` +
        `\n` +
        (b.taken
          ? `Backup taken: ${b.path}`
          : `Backup NOT taken: ${b.reason}`);
      log(text.replace(/\n/g, " | "), d.complete ? "ok" : "err");
      showSecResult(text, d.complete);
      // Every previous read is now stale: FLASH was erased, and the part
      // was power-cycled around the sequence, so RAM is not what it was
      // either. Drop the whole scan rather than leaving old bytes on
      // screen looking current.
      discardScans("chip was wiped — every previous read is stale");
      return { ok: d.complete, text };
    },
  });
});

function showSecResult(text, good) {
  const r = $("sec-result");
  r.hidden = false;
  r.className = "readout-block " + (good ? "good" : "bad");
  r.textContent = text;
}

function renderWipeSteps(steps) {
  const c = $("confirm-checklist");
  c.innerHTML = "";
  for (const s of steps) {
    const d = el("div", "st " + (s.ok ? "ok" : "bad"));
    d.appendChild(el("span", "mark", s.ok ? "✓" : "✗"));
    d.appendChild(el("span", "", s.step));
    const extras = Object.keys(s)
      .filter((k) => k !== "step" && k !== "ok")
      .map((k) => `${k}=${typeof s[k] === "number" ? hex(s[k]) : s[k]}`)
      .join("  ");
    if (extras) d.appendChild(el("span", "detail", extras));
    c.appendChild(d);
  }
}

// -- shared confirmation modal -------------------------------------------
const confirmModal = $("confirm-modal");
let confirmAction = null;

function openConfirm({ title, body, word, steps, go }) {
  $("confirm-title").textContent = title;
  $("confirm-body").innerHTML = body;
  $("confirm-word-label").textContent = word;
  $("confirm-word").value = "";
  $("confirm-go-btn").disabled = true;
  $("confirm-go-btn").textContent = "Proceed";
  const c = $("confirm-checklist");
  c.innerHTML = "";
  if (steps) {
    for (const [key, label] of steps) {
      const d = el("div", "st pending");
      d.appendChild(el("span", "mark", "·"));
      d.appendChild(el("span", "", key));
      d.appendChild(el("span", "detail", label));
      c.appendChild(d);
    }
    c.insertAdjacentHTML(
      "afterbegin",
      "<div class='hint' style='margin-bottom:6px'>The programmer runs the " +
        "whole sequence on the Pico and reports every step when it finishes " +
        "— these are the steps it will run, not live ticks.</div>"
    );
  }
  confirmAction = { word, go };
  confirmModal.hidden = false;
  $("confirm-word").focus();
}

function closeConfirm() {
  confirmModal.hidden = true;
  confirmAction = null;
}

$("confirm-word").addEventListener("input", (e) => {
  $("confirm-go-btn").disabled =
    !confirmAction || e.target.value.trim() !== confirmAction.word;
});
$("confirm-close-btn").addEventListener("click", closeConfirm);
$("confirm-cancel-btn").addEventListener("click", closeConfirm);
confirmModal.addEventListener("click", (e) => {
  if (e.target === confirmModal) closeConfirm();
});

$("confirm-go-btn").addEventListener("click", async () => {
  if (!confirmAction) return;
  const btn = $("confirm-go-btn");
  btn.disabled = true;
  btn.textContent = "working…";
  dot.classList.add("working");
  try {
    await confirmAction.go();
    btn.textContent = "done";
    setTimeout(() => {
      if (!confirmModal.hidden) closeConfirm();
    }, 2500);
  } catch (e) {
    btn.textContent = "failed";
    log(e.message, "err");
    showSecResult(e.message, false);
  } finally {
    dot.classList.remove("working");
  }
});

// =======================================================================
// Advanced panels: raw memory, raw registers, scope
// =======================================================================
$("mem-read-btn").addEventListener("click", async () => {
  try {
    const addr = parseNum($("mem-addr").value);
    const len = parseNum($("mem-len").value);
    const data = await api(`/api/read_block?addr=${addr}&len=${len}`);
    const bytes = data.hex.match(/../g) || [];
    $("mem-output").textContent = bytes.map((b) => b.toUpperCase()).join(" ");
    log(`read ${bytes.length} byte(s) from ${addr16(addr)}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("wb-btn").addEventListener("click", async () => {
  try {
    const addr = parseNum($("wb-addr").value);
    const value = parseNum($("wb-value").value);
    await jsonPost("/api/write_byte", { addr, value });
    log(`wrote ${hex(value)} to ${addr16(addr)}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("reg-read-btn").addEventListener("click", async () => {
  const reg = $("reg-select").value;
  try {
    const data = await api(`/api/reg?reg=${encodeURIComponent(reg)}`);
    const width = reg === "A" || reg === "CCR" ? 2 : 4;
    $("reg-value").value = hex(data.value, width);
    log(`${reg} = ${hex(data.value, width)}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("reg-write-btn").addEventListener("click", async () => {
  const reg = $("reg-select").value;
  try {
    const value = parseNum($("reg-value").value);
    await jsonPost("/api/reg", { reg, value });
    log(`wrote ${reg} = ${hex(value)}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("bkpt-read-btn").addEventListener("click", async () => {
  try {
    const data = await api("/api/bkpt");
    $("bkpt-addr").value = hex(data.addr, 4);
    log(`BKPT = ${addr16(data.addr)}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("control-read-btn").addEventListener("click", async () => {
  try {
    const data = await api("/api/control");
    $("control-value").value = hex(data.value);
    controlHasBeenRead = true;
    $("control-write-btn").disabled = false;
    $("control-write-btn").title =
      "write BDCSCR directly — careful: clearing CLKSW (bit 3) breaks the " +
      "bit timing calibrated by Sync";
    log(`BDCSCR = ${hex(data.value)}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("control-write-btn").addEventListener("click", async () => {
  try {
    const value = parseNum($("control-value").value);
    await jsonPost("/api/control", { value });
    log(`wrote BDCSCR = ${hex(value)}`, "ok");
  } catch (e) {
    log(e.message, "err");
  }
});

$("scope-test").addEventListener("change", () => {
  const showBit = $("scope-test").value === "write_bit";
  $("scope-bit-label").style.display = showBit ? "" : "none";
  $("scope-bit").style.display = showBit ? "" : "none";
});

function themeColor(name, fallback) {
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  return v || fallback;
}

function drawWaveform(samples, samplePeriodNs) {
  const canvas = $("scope-canvas");
  const ctx = canvas.getContext("2d");
  const W = canvas.width;
  const H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  const gridColor = themeColor("--panel-border", "#1c212a");
  const traceColor = themeColor("--accent", "#e8a33d");
  const labelColor = themeColor("--text-dim", "#838d9e");

  ctx.strokeStyle = gridColor;
  ctx.lineWidth = 1;
  for (let x = 0; x < W; x += 60) {
    ctx.beginPath();
    ctx.moveTo(x + 0.5, 0);
    ctx.lineTo(x + 0.5, H);
    ctx.stroke();
  }

  if (!samples || samples.length === 0) return;

  const highY = 20;
  const lowY = H - 20;
  const stepX = W / samples.length;

  ctx.strokeStyle = traceColor;
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

  ctx.fillStyle = labelColor;
  ctx.font = "11px JetBrains Mono, monospace";
  ctx.fillText("1", 4, highY + 4);
  ctx.fillText("0", 4, lowY + 4);
  const totalUs = ((samples.length * samplePeriodNs) / 1000).toFixed(2);
  ctx.fillText(`${totalUs} µs total window`, W - 140, H - 6);
}

$("scope-run-btn").addEventListener("click", async () => {
  const test = $("scope-test").value;
  const bit = parseInt($("scope-bit").value, 10);
  const sampleCount = parseInt($("opt-sample-count").value, 10) || 256;
  try {
    const data = await jsonPost("/api/capture", {
      test, sample_count: sampleCount, bit,
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

// =======================================================================
// init
// =======================================================================
async function init() {
  buildMemMap();
  await refreshPorts().catch((e) => log(e.message, "err"));
  try {
    const status = await api("/api/status");
    if (status.connected) {
      setConnected(true);
      log("server still has the serial port open");
      await probeExistingLink();
    } else {
      setConnected(false);
    }
  } catch (e) {
    log(e.message, "err");
  }
  // If a programming job from a previous page load is still running, pick
  // it up rather than showing an idle panel over live hardware.
  try {
    const job = await api("/api/flash_progress");
    if (job.state === "running") {
      flashing = true;
      $("flash-progress").hidden = false;
      markPlanned(job.pages_planned);
      log("a programming job started before this page load is still running", "warn");
      watchProgress();
    }
  } catch (e) {
    /* nothing to resume */
  }
}

init();
