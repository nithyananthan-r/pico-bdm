"""
app.py — Flask host application for pico-bdm.

Run with:
    python app.py
then open http://127.0.0.1:5000
"""

import os
import threading
import time
import traceback

from flask import Flask, jsonify, request, render_template
from werkzeug.exceptions import HTTPException

from bdm_client import client, BdmClientError, BdmBusyError
from srec import parse_srec, merge_contiguous, build_srec

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
# An .s19 for an 8 KB part is a few tens of KB of ASCII. 512 KB is already
# very generous; without a cap, /api/flash_srec reads an arbitrarily large
# upload fully into memory.
MAX_UPLOAD_BYTES = 512 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Reads go over BDC one byte at a time, so a big block genuinely takes a
# long time on the Pico -- long enough to blow past bdm_client's serial
# timeout while the Pico is still legitimately working, which used to
# desynchronise the connection. Cap it at something that comfortably fits.
MAX_READ_BLOCK_LEN = 1024

# 16-bit address space on this part.
ADDR_MAX = 0xFFFF

# FLASH array of the MC9S08SG8 in the socket: 8 KB at the top of the map.
# (The SG4's 4 KB build starts at $F000; both end at $FFFF.) Used as the
# default region for dumps and for the automatic backup taken before a
# destructive operation.
FLASH_START = 0xE000
FLASH_END = 0xFFFF

# Where automatic pre-destruction backups land. Kept next to the app so a
# bench user can find them without being told.
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

# One read_block round trip per this many bytes when dumping. Measured on
# hardware: ~1.6 ms/byte, so 256 bytes is ~0.4 s per call -- comfortably
# inside the serial timeout, and 8 KB (the whole FLASH array) is ~13 s.
DUMP_CHUNK = 256

# FLASH page size on the SG8/SG4 (see firmware/bdc.py PAGE_SIZE). Kept in
# sync manually; used here to compute the union of pages an S-record file
# touches so they can be erased exactly once.
FLASH_PAGE_SIZE = 512

# ---------------------------------------------------------------------------
# CSRF mitigation
# ---------------------------------------------------------------------------
# This app drives real hardware: a POST to /api/mass_erase wipes a chip.
# A malicious page open in another tab can issue a cross-origin POST with
# no body, or a multipart/form-data POST, without any preflight -- both are
# CORS-"simple" requests. It CANNOT set a custom header, because that forces
# a preflight, and this server sends no permissive CORS headers, so the
# preflight fails and the real request never happens.
#
# So: every state-changing request must carry this header. app.js sends it
# on every call (including the raw fetch() used for the file upload).
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "pico-bdm"


@app.before_request
def require_csrf_header():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if not request.path.startswith("/api/"):
        return None
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "missing or wrong %s header -- state-changing "
                    "requests must come from the pico-bdm UI"
                    % CSRF_HEADER,
                }
            ),
            403,
        )
    return None


@app.errorhandler(HTTPException)
def handle_http_exception(e):
    """Return JSON for every /api/ error, including the ones Flask raises
    before our own handlers run (415 on a bad Content-Type, 413 on an
    oversized upload, 404, ...). Without this the browser gets an HTML
    error page and app.js's res.json() fails with a confusing
    "Unexpected token '<'" instead of showing the real problem."""
    if request.path.startswith("/api/"):
        return (
            jsonify({"ok": False, "error": "%s: %s" % (e.name, e.description)}),
            e.code or 500,
        )
    return e


@app.errorhandler(Exception)
def handle_unexpected_exception(e):
    """Same contract as the HTTPException handler above, for the errors that
    are NOT HTTPExceptions.

    Hit for real this session: pyserial raised
    `SerialException: Cannot configure port ... PermissionError(13)` from
    inside /api/chip_info after the Pico's USB CDC re-enumerated. Nothing
    caught it (the routes catch BdmClientError, and SerialException is an
    OSError), so Flask rendered the debugger's HTML page, app.js's
    res.json() choked on it, and the UI reported
    "Unexpected token '<'" -- which says nothing about a dropped USB device.

    The traceback still goes to the console, where a bench user wants it.
    """
    if isinstance(e, HTTPException):
        return handle_http_exception(e)
    if not request.path.startswith("/api/"):
        raise e
    traceback.print_exc()
    msg = "%s: %s" % (type(e).__name__, e)
    if isinstance(e, OSError):
        # SerialException subclasses IOError/OSError.
        msg += (
            " -- the serial handle to the Pico is no longer usable. The port "
            "most likely re-enumerated (Pico reset or replugged). Disconnect "
            "and Connect again, then Sync target."
        )
    return jsonify({"ok": False, "error": msg}), 500


def api_error(exc, status=400):
    return jsonify({"ok": False, "error": str(exc)}), status


# ---------------------------------------------------------------------------
# Small parsing/validation helpers. Every route that takes a number from the
# user runs it through these, so an out-of-range or negative value fails
# loudly here instead of being silently truncated by the bit-shifting in
# firmware/bdc.py.
# ---------------------------------------------------------------------------
def json_body():
    """Body as a dict, tolerating a missing or unparseable body.

    request.json raises in modern Flask when the Content-Type is wrong or
    the body doesn't parse, so `request.json or {}` never actually reached
    the `or {}`; get_json(silent=True) does.
    """
    return request.get_json(silent=True) or {}


def to_int(value, what):
    if isinstance(value, bool) or value is None:
        raise ValueError("%s must be a number" % what)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 0)
        except ValueError:
            raise ValueError("%s: %r is not a number" % (what, value))
    raise ValueError("%s must be a number" % what)


def check_range(value, what, lo, hi):
    if not (lo <= value <= hi):
        raise ValueError(
            "%s out of range: %d (allowed %d..%d)" % (what, value, lo, hi)
        )
    return value


def addr_arg(value, what="addr"):
    return check_range(to_int(value, what), what, 0, ADDR_MAX)


def byte_arg(value, what="value"):
    return check_range(to_int(value, what), what, 0, 0xFF)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ports")
def api_ports():
    return jsonify({"ok": True, "ports": client.list_ports()})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    try:
        # Inside the try: with a missing/wrong Content-Type, request.json
        # raises UnsupportedMediaType, which outside the try became a 415
        # HTML page instead of the intended JSON error.
        port = json_body().get("port")
        if not port:
            return api_error("missing 'port'")
        # Already on this port? Don't reopen it. Windows gives exclusive
        # access to a COM port, so a second open of the SAME port fails with
        # "Access is denied" -- and the old error path then disconnected the
        # perfectly good connection it was holding, which turned a harmless
        # duplicate connect into "not connected to a Pico" for everything
        # afterwards. Measured, this session.
        if client.is_connected and client.port == port:
            client.ping()
            return jsonify({"ok": True, "already_connected": True})
        was_connected = client.is_connected
        client.connect(port)
        client.ping()
        return jsonify({"ok": True, "already_connected": False})
    except (BdmClientError, OSError, ValueError) as e:
        # Only tear down a connection this call actually created. connect()
        # leaves an existing good connection alone on failure, so closing it
        # here would be this handler destroying working state.
        if client.is_connected and not was_connected:
            client.disconnect()
        return api_error(e)


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    client.disconnect()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    return jsonify({"ok": True, "connected": client.is_connected})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Enter BDM and calibrate the bit rate.

    Entry is a POWER-ON reset, not a RESET pulse. This route used to call
    hw_reset_to_bdm(), which cannot work on this part: datasheet
    Sec 17.1.1 says external-pin and internally-generated resets ignore
    BKGD, and only a power-on reset latches active background mode. Every
    successful BDM entry this project has ever made went through
    power_on_reset_to_bdm(); see CONTEXT.md Finding 63 / Finding 90.

    The SYNC measurement is then VALIDATED, because it is not always
    right: against a free-running target it comes back exactly 2x high,
    and every transfer at that rate silently decodes to garbage
    (CONTEXT.md Finding 99). SDIDL at $1807 must read 0x14 on this part,
    so read it; if it doesn't, halve the bit clock and read it again.
    """
    try:
        # One lock for the whole sequence -- nothing may interleave between
        # the power-cycle, the SYNC probe and the validating read.
        with client.transaction():
            client.power_on_reset_to_bdm()
            freq = client.sync()["target_freq_hz"]
            used, ok = freq, False
            for candidate in (freq, freq // 2):
                try:
                    client.set_bit_clock(candidate)
                    if client.read_byte(0x1807) == 0x14:
                        used, ok = candidate, True
                        break
                except BdmClientError:
                    continue
            if not ok:
                return api_error(
                    "SYNC measured %d Hz but the target's part ID at $1807 "
                    "does not read back as 0x14 at that rate or at half of "
                    "it -- the link is not decoding" % freq
                )
        # CLKSW = 1 on this part, so the rate we just validated is the bus
        # clock -- remember it for FCDIV. See _ensure_flash_clock().
        global _last_bus_hz
        _last_bus_hz = int(used)
        return jsonify(
            {"ok": True, "target_freq_hz": used, "sync_raw_hz": freq}
        )
    except (BdmClientError, KeyError) as e:
        return api_error(e)


# ---------------------------------------------------------------------------
# FCDIV bookkeeping.
#
# Every FLASH command -- erase, program, blank check, mass erase -- is
# rejected with FACCERR unless FCDIV has been written since the last
# power-up, and FCDIV is volatile. /api/sync POWER-CYCLES the target, so it
# clears FCDIV every time it runs. /api/blank_check and /api/mass_erase
# used to issue their commands without ever writing FCDIV, which meant they
# could not work at all: measured against real hardware they returned
# "FLASH access error (FACCERR set), FSTAT=0xD0" every time.
#
# The bus clock FCDIV needs does not have to be guessed. BDCSCR reads 0xC8
# on this part, i.e. CLKSW = 1, which selects the MCU bus clock as the BDC
# clock -- so the frequency SYNC measures IS fBus (CONTEXT.md Finding 94).
# /api/sync records it here; /api/flash_srec still takes an explicit
# bus_freq_hz from the form, because that path is also used for parts whose
# CLKSW is 0.
_last_bus_hz = None


def _ensure_flash_clock():
    """Write FCDIV if we know the bus clock. Call inside a transaction."""
    if _last_bus_hz is None:
        raise BdmClientError(
            "the target's FLASH clock divider has not been set -- run SYNC "
            "first so the bus clock is known"
        )
    client.flash_init_clock(_last_bus_hz)


@app.route("/api/read_byte")
def api_read_byte():
    try:
        addr = addr_arg(request.args["addr"])
        value = client.read_byte(addr)
        return jsonify({"ok": True, "addr": addr, "value": value})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/write_byte", methods=["POST"])
def api_write_byte():
    try:
        body = json_body()
        addr = addr_arg(body["addr"])
        value = byte_arg(body["value"])
        client.write_byte(addr, value)
        return jsonify({"ok": True})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/read_block")
def api_read_block():
    try:
        addr = addr_arg(request.args["addr"])
        length = check_range(
            to_int(request.args["len"], "len"), "len", 1, MAX_READ_BLOCK_LEN
        )
        if addr + length - 1 > ADDR_MAX:
            return api_error("read would run past the end of the address space")
        data = client.read_block(addr, length)
        return jsonify({"ok": True, "addr": addr, "hex": data.hex()})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/mass_erase", methods=["POST"])
def api_mass_erase():
    try:
        with client.transaction():
            _ensure_flash_clock()
            client.mass_erase()
        return jsonify({"ok": True})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/halt", methods=["POST"])
def api_halt():
    """BDC BACKGROUND -- stop a running target and enter active background
    mode, without resetting it.

    -> {"ok":true,"bdcscr":200,"halted":true}

    `halted` is read back from BDCSCR.BDMACT rather than assumed: BACKGROUND
    is ignored unless ENBDM is already 1 (Finding 100), and a debugger panel
    must not claim a halt it did not get.
    """
    try:
        with client.transaction():
            client.background()
            status = client.read_status()
        return jsonify({"ok": True, "bdcscr": status,
                        "halted": bool(status != 0xFF and status & 0x40)})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/go", methods=["POST"])
def api_go():
    try:
        client.go()
        return jsonify({"ok": True})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/step", methods=["POST"])
def api_step():
    try:
        client.step()
        return jsonify({"ok": True})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/tagged_go", methods=["POST"])
def api_tagged_go():
    try:
        client.tagged_go()
        return jsonify({"ok": True})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/reset_target", methods=["POST"])
def api_reset_target():
    try:
        client.reset_target()
        return jsonify({"ok": True})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/blank_check", methods=["POST"])
def api_blank_check():
    try:
        with client.transaction():
            _ensure_flash_clock()
            blank = client.blank_check()
        return jsonify({"ok": True, "blank": blank})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/control", methods=["GET", "POST"])
def api_control():
    """GET reads BDCSCR (same register read_status exposes); POST writes it
    -- e.g. to clear sticky WS/WSF/DVF flags or change CLKSW."""
    try:
        if request.method == "GET":
            return jsonify({"ok": True, "value": client.read_status()})
        value = byte_arg(json_body()["value"], "BDCSCR value")
        client.write_control(value)
        return jsonify({"ok": True})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/bkpt", methods=["GET", "POST"])
def api_bkpt():
    try:
        if request.method == "GET":
            return jsonify({"ok": True, "addr": client.read_bkpt()})
        addr = addr_arg(json_body()["addr"])
        client.write_bkpt(addr)
        return jsonify({"ok": True})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/reg")
def api_reg_read():
    try:
        reg = request.args["reg"]
        return jsonify({"ok": True, "reg": reg, "value": client.read_reg(reg)})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/reg", methods=["POST"])
def api_reg_write():
    try:
        body = json_body()
        reg = body["reg"]
        # PC/HX/SP are 16-bit, A/CCR 8-bit; the firmware masks the 8-bit
        # ones, so validate against the widest and let it narrow.
        value = check_range(to_int(body["value"], "value"), "value", 0, 0xFFFF)
        client.write_reg(reg, value)
        return jsonify({"ok": True})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/capture", methods=["POST"])
def api_capture():
    try:
        body = json_body()
        test = body.get("test", "sync")
        sample_count = to_int(body.get("sample_count", 256), "sample_count")
        check_range(sample_count, "sample_count", 32, 4096)
        if sample_count % 32 != 0:
            return api_error("sample_count must be a multiple of 32")
        bit = check_range(to_int(body.get("bit", 1), "bit"), "bit", 0, 1)
        result = client.capture(test, sample_count=sample_count, bit=bit)
        return jsonify({"ok": True, **result})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/capture_pin", methods=["POST"])
def api_capture_pin():
    """THIRTEENTH session: record an arbitrary Pico GPIO passively.

    POST {"gpio":16,"sample_count":256,"window_us":2000}
    -> {"ok":true,"samples":[0,1,...],"sample_period_ns":7812,
        "triggered":true,"gpio":16}

    This is the only honest way to get a waveform off a target pin that is
    not BKGD: nothing on this board is wired to PTA/PTB/PTC, so the pin has
    to be patched to a spare GPIO first. `triggered` is false when the
    falling-edge arm never fired -- the UI must show that as "no measurement"
    rather than drawing the partial buffer as a flat line.
    """
    try:
        body = json_body()
        gpio = check_range(to_int(body.get("gpio"), "gpio"), "gpio", 0, 28)
        sample_count = to_int(body.get("sample_count", 256), "sample_count")
        check_range(sample_count, "sample_count", 32, 4096)
        if sample_count % 32 != 0:
            return api_error("sample_count must be a multiple of 32")
        window_us = float(body.get("window_us", 2000))
        if window_us <= 0:
            return api_error("window_us must be positive")
        result = client.capture_pin(gpio, sample_count=sample_count,
                                    window_us=window_us)
        return jsonify({"ok": True, **result})
    except (BdmClientError, KeyError, ValueError, TypeError) as e:
        return api_error(e)


@app.route("/api/probe_pin", methods=["POST"])
def api_probe_pin():
    """Cheap activity measurement on any Pico GPIO: low duty and edge count
    over `ms` milliseconds of plain SIO sampling.

    POST {"gpio":16,"ms":30}
    -> {"ok":true,"pin":16,"samples":1231,"low_pct":16.5,"transitions":408}
    """
    try:
        body = json_body()
        gpio = check_range(to_int(body.get("gpio"), "gpio"), "gpio", 0, 28)
        ms = check_range(to_int(body.get("ms", 30), "ms"), "ms", 1, 500)
        result = client.probe_pin(gpio, ms=ms)
        result.setdefault("ms", ms)
        return jsonify({"ok": True, **result})
    except (BdmClientError, KeyError, ValueError, TypeError) as e:
        return api_error(e)


# The union-of-pages computation that used to live here (_pages_touched) is
# gone: firmware's flash_program_image() builds the page map from all chunks
# itself, so the "chunk B's erase wipes chunk A" hazard it worked around
# cannot arise -- a page is erased exactly once, then every byte belonging to
# it, from whichever chunk, is programmed and verified before the next page.


# ---------------------------------------------------------------------------
# TWELFTH session: real-time programming progress.
#
# The Pico answers ONE flash_image command when the WHOLE image is done, so
# a synchronous /api/flash_srec cannot report anything until it is over --
# and a UI animating "progress" during that silence would be inventing it.
#
# So the optional async path here drives the SAME firmware routine one page
# at a time (firmware's flash_program_image builds its page map from the
# chunks it is given; giving it exactly one page's worth makes each round
# trip one page's erase -> program -> verify) and publishes each page's REAL
# report the moment the Pico returns it. Everything the UI animates is
# therefore a measurement, not a timer: a page is "in flight" from the
# moment its command goes out, and becomes erased/programmed/verified only
# when the target says so.
#
# The synchronous path is untouched and is still the default.
# ---------------------------------------------------------------------------
_flash_job = {"id": 0, "state": "idle"}
_flash_job_lock = threading.Lock()


def _job_update(**kw):
    with _flash_job_lock:
        _flash_job.update(kw)


def _job_snapshot():
    with _flash_job_lock:
        return dict(_flash_job)


def _page_map(chunks):
    """(page addr) -> {addr: byte}, exactly as firmware builds it."""
    pages = {}
    for addr, data in chunks:
        for i in range(len(data)):
            a = addr + i
            pages.setdefault(a - (a % FLASH_PAGE_SIZE), {})[a] = data[i]
    return pages


def _page_chunks(cells):
    """{addr: byte} -> list of (addr, bytes) contiguous runs."""
    out = []
    run_start = None
    run = bytearray()
    for a in sorted(cells):
        if run_start is not None and a == run_start + len(run):
            run.append(cells[a])
            continue
        if run_start is not None:
            out.append((run_start, bytes(run)))
        run_start, run = a, bytearray([cells[a]])
    if run_start is not None:
        out.append((run_start, bytes(run)))
    return out


def _flash_worker(job_id, chunks, bus_freq_hz, erase_mode, nvopt):
    pages = _page_map(chunks)
    order = sorted(pages)
    started = time.time()
    try:
        with client.transaction():
            if erase_mode == "mass":
                _job_update(phase="mass_erase", current_page=None)
                client.flash_init_clock(bus_freq_hz)
                client.mass_erase()

            done = []
            written = 0
            for idx, page in enumerate(order):
                cells = pages[page]
                _job_update(
                    phase="page", current_page=page, current_index=idx,
                    current_bytes=len(cells), pages=list(done),
                    total_bytes=written, elapsed=time.time() - started,
                )
                rep = client.flash_image(
                    _page_chunks(cells),
                    bus_freq_hz=bus_freq_hz,
                    erase=(erase_mode == "pages"),
                    nvopt=nvopt,
                )
                entry = (rep.get("pages") or [{"page": page}])[0]
                done.append(entry)
                written += rep.get("bytes_written", 0)
                _job_update(
                    pages=list(done), total_bytes=written,
                    security_risk=bool(rep.get("security_risk")),
                    elapsed=time.time() - started,
                )
                if not rep.get("complete"):
                    # Stop at the first failing page. Every page before it
                    # is confirmed good and is already in `pages`, so the UI
                    # can name exactly what landed and what did not.
                    _job_update(
                        state="failed", phase="failed",
                        error=rep.get("error") or "page $%04X failed" % page,
                        failed_page=page, complete=False,
                        elapsed=time.time() - started,
                    )
                    return

            if erase_mode == "mass" and not any(
                a <= 0xFFBF < a + len(d) for a, d in chunks
            ):
                _job_update(phase="nvopt_restore")
                sec = client.set_security(nvopt=nvopt, bus_freq_hz=None)
                _job_update(
                    nvopt_restored=sec.get("after"),
                    nvopt_restore_verified=bool(sec.get("written")),
                    security_risk=not sec.get("written", False),
                )

        _job_update(state="done", phase="done", complete=True, error=None,
                    failed_page=None, current_page=None,
                    elapsed=time.time() - started)
    except Exception as e:  # BdmClientError, serial errors, anything
        _job_update(state="failed", phase="failed", complete=False,
                    error="%s: %s" % (type(e).__name__, e),
                    elapsed=time.time() - started)


@app.route("/api/flash_progress")
def api_flash_progress():
    """Poll the async programming job started by
    POST /api/flash_srec with form field `async=1`.

    -> {"ok":true,"id":3,"state":"running"|"done"|"failed"|"idle",
        "phase":"page","current_page":57344,"current_index":0,
        "pages_planned":[{"page":57344,"bytes":192},...],
        "pages":[{"page":57344,"bytes":192,"erased":true,"programmed":192,
                  "verified":true}],
        "total_bytes":192,"complete":false,"error":null,
        "security_risk":false,"elapsed":1.2}

    `pages` only ever contains pages the TARGET has confirmed; a page that
    is in flight appears as `current_page` and nowhere else.
    """
    return jsonify({"ok": True, **_job_snapshot()})


@app.route("/api/flash_srec", methods=["POST"])
def api_flash_srec():
    """
    Accepts a multipart-form upload with an .s19 file under 'file', plus a
    'bus_freq_hz' form field for FCDIV setup.

    PROGRAMMING IS NOW PAGE AT A TIME: erase page -> program that page ->
    verify that page -> move on, all inside the Pico (a host-driven byte
    costs ~17 ms of USB round trip). That is the achievable form of
    power-loss protection on this wiring -- there is no VDD sense line to
    interrupt on, so instead every page is confirmed good before the next
    one is touched, and a failure names the page and address it stopped at
    rather than leaving an uncharacterised half-written array.

    The $FE00/$FFBF hazard (CONTEXT.md Finding 95) is handled inside that
    loop: erasing the last page blanks NVOPT and the part re-secures itself
    at the next reset, so NVOPT is reprogrammed immediately after that
    page's erase, before any other byte of it, and `security_risk` in the
    response is True only if the run died inside that window.

    Form fields:
      file          the .s19                                    (required)
      bus_freq_hz   for FCDIV (default 8000000; SYNC's measured rate is
                    fBus on this part, CLKSW=1)
      erase_mode    pages (default) | mass | none
      nvopt         NVOPT byte to restore if page $FE00 is erased and the
                    image does not itself contain $FFBF (default 0xFE =
                    unsecured)

    -> {"ok":true,"complete":true,"security_risk":false,"error":null,
        "total_bytes":195,"nvopt":254,"erase_mode":"pages","pages_erased":2,
        "pages":[{"page":57344,"bytes":192,"erased":true,"programmed":192,
                  "verified":true},
                 {"page":65024,"bytes":3,"erased":true,"nvopt_restored":254,
                  "programmed":2,"verified":true}],
        "chunks":[{"addr":57344,"len":192}, ...]}
    On a failure the same shape comes back with HTTP 400, "complete":false,
    an "error" string, and the pages that DID verify still listed.
    """
    try:
        bus_freq_hz = to_int(request.form.get("bus_freq_hz", "8000000"), "bus_freq_hz")
        check_range(bus_freq_hz, "bus_freq_hz", 1, 100_000_000)
        erase_mode = request.form.get("erase_mode", "pages")  # pages | mass | none
        if erase_mode not in ("pages", "mass", "none"):
            return api_error("erase_mode must be 'pages', 'mass', or 'none'")
        if "file" not in request.files:
            return api_error("no file uploaded")
        f = request.files["file"]
        try:
            text = f.read().decode()
        except UnicodeDecodeError:
            return api_error("uploaded file is not text -- expected an S-record file")
        chunks = merge_contiguous(parse_srec(text))
        if not chunks:
            return api_error("no data records found in file")
        for addr, data in chunks:
            check_range(addr, "record address", 0, ADDR_MAX)
            check_range(addr + len(data) - 1, "record end address", 0, ADDR_MAX)

        nvopt = byte_arg(request.form.get("nvopt", 0xFE), "nvopt")

        # Opt-in async mode: run the same page-at-a-time programming on a
        # worker thread and publish each page's real report to
        # /api/flash_progress as the target confirms it. The default path
        # below is unchanged.
        if _bool_arg(request.form.get("async")):
            snap = _job_snapshot()
            if snap.get("state") == "running":
                return api_error("a programming job is already running")
            pages = _page_map(chunks)
            planned = [{"page": p, "bytes": len(pages[p])} for p in sorted(pages)]
            job_id = snap.get("id", 0) + 1
            with _flash_job_lock:
                _flash_job.clear()
                _flash_job.update({
                    "id": job_id, "state": "running", "phase": "starting",
                    "pages_planned": planned, "pages": [], "total_bytes": 0,
                    "erase_mode": erase_mode, "nvopt": nvopt,
                    "chunks": [{"addr": a, "len": len(d)} for a, d in chunks],
                    "complete": False, "error": None, "security_risk": False,
                    "current_page": None, "failed_page": None, "elapsed": 0.0,
                })
            threading.Thread(
                target=_flash_worker,
                args=(job_id, chunks, bus_freq_hz, erase_mode, nvopt),
                daemon=True,
            ).start()
            return jsonify({"ok": True, "async": True, "job_id": job_id,
                            "pages_planned": planned}), 202

        # One lock for the whole programming operation. A FLASH command is a
        # multi-write sequence on the target; letting another tab interleave
        # a read in the middle of it is exactly how you provoke a FACCERR.
        with client.transaction():
            if erase_mode == "mass":
                # A mass erase blanks NVOPT too, and the part would
                # re-secure at the next reset -- so the image is programmed
                # with erase=False afterwards and NVOPT is restored as part
                # of that run only if the image covers page $FE00. Make sure
                # it does.
                client.flash_init_clock(bus_freq_hz)
                client.mass_erase()

            rep = client.flash_image(
                chunks,
                bus_freq_hz=bus_freq_hz,
                erase=(erase_mode == "pages"),
                nvopt=nvopt,
            )

            if erase_mode == "mass" and not any(
                a <= 0xFFBF < a + len(d) for a, d in chunks
            ):
                # The mass erase left NVOPT blank (0xFF = SEC 1:1) and the
                # image did not rewrite it. Close the window now, in this
                # same power cycle, or the chip locks itself at the next
                # reset (Finding 95).
                sec = client.set_security(nvopt=nvopt, bus_freq_hz=None)
                rep["nvopt_restored"] = sec.get("after")
                rep["nvopt_restore_verified"] = bool(sec.get("written"))
                rep["security_risk"] = not sec.get("written", False)

        payload = {
            "ok": bool(rep.get("complete")),
            "chunks": [{"addr": a, "len": len(d)} for a, d in chunks],
            "total_bytes": rep.get("bytes_written", 0),
            # "mass" when the whole array went in one command, otherwise the
            # number of pages this run erased individually.
            "pages_erased": ("mass" if erase_mode == "mass" else
                             sum(1 for p in rep.get("pages", [])
                                 if p.get("erased"))),
            "erase_mode": erase_mode,
            **rep,
        }
        if rep.get("security_risk"):
            payload["warning"] = (
                "CHIP MAY BE LEFT IN A SELF-SECURING STATE: page $FE00 was "
                "erased and NVOPT at $FFBF was not confirmed rewritten. DO "
                "NOT POWER-CYCLE THE TARGET -- rewrite $FFBF now (POST "
                "/api/security with the intended value), or the part will "
                "secure itself at the next reset and need a full wipe "
                "(/api/unsecure) to recover."
            )
        return jsonify(payload), (200 if rep.get("complete") else 400)
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


# ---------------------------------------------------------------------------
# ELEVENTH session: chip info, live state, security, recovery, verify, dump.
#
# Response shape convention for everything below: HTTP 200 with
# {"ok": true, ...} on success, HTTP 400 with {"ok": false, "error": "..."}
# on failure -- except the pollable route, which answers 200 with
# {"ok": true, "busy": true} when the link is held by a long operation
# rather than queueing behind it.
# ---------------------------------------------------------------------------
def _bool_arg(value, default=False):
    if value is None:
        return default
    return str(value).lower() in ("1", "true", "yes", "on")


def _dump_region(addr, length):
    """Read `length` bytes starting at `addr`. Call inside a transaction."""
    out = bytearray()
    while len(out) < length:
        n = min(DUMP_CHUNK, length - len(out))
        out += client.read_block(addr + len(out), n)
    return bytes(out)


def _auto_backup(reason, addr=FLASH_START, length=None):
    """Read the FLASH array to a timestamped .s19 before doing something
    destructive. Call inside a transaction.

    A secured part answers every FLASH read with 0x00 (Finding 95), so a
    "backup" taken then would be 8 KB of zeros masquerading as the chip's
    contents. That case is detected and reported as skipped, with the
    reason, instead of writing a worthless file.
    """
    if length is None:
        length = FLASH_END - addr + 1
    sec = client.security_state()
    if sec.get("secured"):
        return {
            "taken": False,
            "reason": "the part is secured: every FLASH read returns 0x00, "
                      "so its contents cannot be backed up. Nothing readable "
                      "is being destroyed.",
            "security": sec,
        }
    data = _dump_region(addr, length)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = "%s_%s_%04X-%04X.s19" % (
        time.strftime("%Y%m%d-%H%M%S"), reason, addr, addr + length - 1
    )
    path = os.path.join(BACKUP_DIR, name)
    with open(path, "w") as fh:
        fh.write(build_srec([(addr, data)], header="PICOBDM-BACKUP"))
    return {
        "taken": True,
        "path": path,
        "addr": addr,
        "length": length,
        "blank": all(b == 0xFF for b in data),
        "sha_prefix": data[:16].hex(),
    }


@app.route("/api/chip_info")
def api_chip_info():
    """What chip is this and what state is it in. Meant to run on connect.

    GET /api/chip_info[?blank_check=1]
    -> {"ok":true, "sdidh":160,"sdidl":20,"part_id":20,"rev":10,"id_ok":true,
        "srs":130,"reset_source":{"por":true,...},"sopt1":192,"spmsc1":28,
        "fcdiv":180,"fprot":255,"fstat":192,"bdcscr":200,"clksw":true,
        "bdc_clock_hz":9248555,"bus_clock_hz":9248555,
        "security":{"fopt":194,"sec":2,"secured":false,"keyen":true,
                    "fnored":true,"nvopt":254,"nvopt_trustworthy":true,
                    "suspect_link":false},
        "blank":false}
    blank_check runs the FLASH module's own erase-verify and therefore needs
    FCDIV, so it is opt-in and needs SYNC to have run first.
    """
    try:
        blank = _bool_arg(request.args.get("blank_check"))
        identify = _bool_arg(request.args.get("identify"), True)
        ram_probe = _bool_arg(request.args.get("ram_probe"), False)
        defect_scan = _bool_arg(request.args.get("defect_scan"))
        with client.transaction():
            info = client.chip_info(
                bus_freq_hz=_last_bus_hz if blank else None,
                blank_check=blank,
            )
            if identify:
                info["identity"] = client.identify(
                    ram_probe=ram_probe, defect_scan=defect_scan
                )
        return jsonify({"ok": True, **info})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/identify")
def api_identify():
    """Which MCU is in the socket, in words rather than hex.

    GET /api/identify[?ram_probe=1][&defect_scan=1]
    -> {"ok":true,"sdidh":160,"sdidl":20,"part_id":20,"part_id_hex":"0x014",
        "rev":10,"family":"MC9S08SG8 / MC9S08SG4 (HCS08 SG family)",
        "variants":["MC9S08SG8","MC9S08SG4"],
        "name":"MC9S08SG8","confidence":"probed",
        "evidence":"RAM at $0240 holds written data, so this part has the
                    SG8's 512 B array",
        "ram_bytes":512,"flash_kb":8,"flash_start":57344,
        "bad_ram_cells":[{"addr":96,"mask":127}, ...]}

    SDID gives the FAMILY only -- the SG8 and SG4 share a part ID -- so
    `confidence` is "id-only" until `ram_probe=1` settles the derivative by
    testing whether $0240 exists (it does on the SG8's 512 B array, not on
    the SG4's 256 B one). The probe writes and restores a single RAM byte,
    which is why it is opt-in rather than part of every status read;
    `defect_scan=1` does the same across $0060-$006F and reports this die's
    per-address AND masks.
    """
    try:
        with client.transaction():
            return jsonify({"ok": True, **client.identify(
                ram_probe=_bool_arg(request.args.get("ram_probe"), True),
                defect_scan=_bool_arg(request.args.get("defect_scan")),
            )})
    except BdmClientError as e:
        return api_error(e)


def _expand_port(data, ddr, name, pins=8):
    """Turn a (data, direction) register pair into per-pin facts.

    `level` is what the pin register reads; for an input that is the pin,
    for an output it is the value being driven. Saying which of the two it
    is, is the whole point of shipping `direction` alongside it.
    """
    if data is None or ddr is None:
        return None
    return [
        {
            "pin": "PT%s%d" % (name, i),
            "direction": "out" if (ddr >> i) & 1 else "in",
            "level": (data >> i) & 1,
        }
        for i in range(pins)
    ]


@app.route("/api/live_state")
def api_live_state():
    """Pollable snapshot for the live-debug panel. ~0.13 s per call on
    hardware, so a few polls a second is comfortable.

    GET /api/live_state[?cpu_regs=0][?ports=0]
    -> {"ok":true,"busy":false,
        "bdcscr":{"value":200,"enbdm":true,"bdmact":true,"bkpten":false,
                  "fts":false,"clksw":true,"ws":false,"wsf":false,"dvf":false},
        "bkpt":{"addr":0,"enabled":false,"tag_mode":false},
        "ports":{"raw":{"ptad":0,"ptadd":0,"ptbd":192,...},
                 "A":[{"pin":"PTA0","direction":"in","level":0}, ...],
                 "B":[...], "C":[...],
                 "note":"PTC is read anyway ..."},
        "cpu_regs":{"A":{"available":true,"value":0,"raw":0,
                         "ambiguous":false,"reason":null}, ...}}

    If a flash/erase operation holds the link, this returns
    {"ok":true,"busy":true} IMMEDIATELY instead of blocking -- see
    BdmClient.try_transaction. Polling therefore cannot pile up behind a
    long operation, and cannot deadlock against one either.
    """
    try:
        cpu_regs = _bool_arg(request.args.get("cpu_regs"), True)
        ports = _bool_arg(request.args.get("ports"), True)
        try:
            with client.try_transaction():
                state = client.live_state(cpu_regs=cpu_regs, ports=ports)
        except BdmBusyError:
            return jsonify({"ok": True, "busy": True})
        raw = state.get("ports")
        if raw:
            state["ports"] = {
                "raw": raw,
                "A": _expand_port(raw.get("ptad"), raw.get("ptadd"), "A"),
                "B": _expand_port(raw.get("ptbd"), raw.get("ptbdd"), "B"),
                "C": _expand_port(raw.get("ptcd"), raw.get("ptcdd"), "C"),
                "note": "PTA/PTB are bonded out on this package; PTC is read "
                        "and reported raw but may not exist on this part.",
            }
        return jsonify({"ok": True, "busy": False, **state})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/relink", methods=["POST"])
def api_relink():
    """Recover the BDC link to a target that is RUNNING, without resetting
    it. Use this when /api/live_state comes back {"link_ok": false}.

    POST /api/relink
    -> {"ok":true,"validated":true,"bit_clock_hz":9302326,
        "sync_raw_hz":18604652,"status":152,
        "tried":[{"hz":18604652,"value":82,"status":37},
                 {"hz":9302326,"value":20,"status":152}]}

    /api/sync cannot do this job: its BDM entry is a POWER-ON reset, which
    restarts whatever the target was doing. This only re-measures the bit
    rate (trying half, per Finding 99) and re-asserts ENBDM (Finding 100).
    `validated` is whether SDIDL read back 0x14 afterwards -- if it is
    false, the link is still not decoding and a power-on re-entry (/api/sync)
    is the remaining option.
    """
    try:
        with client.transaction():
            rep = client.relink()
        global _last_bus_hz
        if rep.get("validated") and rep.get("bit_clock_hz"):
            _last_bus_hz = int(rep["bit_clock_hz"])
        return jsonify({"ok": True, **rep})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/power")
def api_power():
    """What is known about target power, and what deliberately is not.

    GET /api/power
    -> {"ok":true,"vdd_pin":12,"vdd_driven_high":true,"bkgd_pin":15,
        "reset_pin":14,"can_sense_voltage":false,
        "reason":"target VDD is driven from GPIO12, which is not an
                  ADC-capable pin (the RP2040 ADC reaches GPIO26-29 only),
                  and no sense wire exists",
        "adc_capable_pins":[26,27,28,29],
        "witness":"a target that answers BDC commands is powered; that is
                   the only in-band evidence available"}

    There is no voltage measurement here and the endpoint says so, because
    the alternative -- a UI showing an invented 3.3 V -- would be worse than
    showing nothing. Power-loss protection on this hardware is therefore
    sequencing (page-at-a-time erase/program/verify), not detection.
    """
    try:
        with client.transaction():
            return jsonify({"ok": True, **client.power_state()})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/security")
def api_security_get():
    """GET /api/security
    -> {"ok":true,"fopt":194,"sec":2,"secured":false,"keyen":true,
        "fnored":true,"nvopt":254,"nvopt_trustworthy":true,
        "suspect_link":false}

    `secured` comes from FOPT ($1821), a peripheral register that answers
    even on a locked part. `nvopt` is FLASH and reads 0x00 on a locked
    part, which is why `nvopt_trustworthy` exists.
    """
    try:
        with client.transaction():
            return jsonify({"ok": True, **client.security_state()})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/security", methods=["POST"])
def api_security_set():
    """Lock the part. THIS IS DESTRUCTIVE IN EFFECT: after the next reset
    the chip refuses every FLASH and RAM access over BDM until it is wiped
    with /api/unsecure, which erases everything on it.

    POST /api/security {"confirm":"SECURE", "backup":true, "nvopt":252}
    -> {"ok":true,"before":254,"wrote":252,"after":252,"written":true,
        "fopt_now":194,"secured_now":false,
        "takes_effect":"at the next reset / power cycle",
        "backup":{"taken":true,"path":"...","length":8192,...},
        "warning":"..."}

    `confirm` is mandatory and must be the exact string "SECURE" -- this is
    the one operation in the app that can make a chip unusable without
    erasing it, and a mis-click should not be enough. The default nvopt
    0xFC sets SEC = 0:0 by clearing a bit, so page $FE00 is NOT erased and
    the Finding 95 hazard is never opened.
    """
    try:
        body = json_body()
        if body.get("confirm") != "SECURE":
            return api_error(
                'refusing to secure the part without {"confirm":"SECURE"} -- '
                "after the next reset it will reject all FLASH/RAM access "
                "until a full wipe (/api/unsecure) is run"
            )
        nvopt = byte_arg(body.get("nvopt", 0xFC), "nvopt")
        want_backup = body.get("backup", True)
        with client.transaction():
            backup = _auto_backup("before-secure") if want_backup else \
                {"taken": False, "reason": "not requested"}
            result = client.set_security(nvopt=nvopt, bus_freq_hz=_last_bus_hz)
        return jsonify({
            "ok": True,
            "backup": backup,
            "warning": "the part locks itself at the NEXT reset; recovering "
                       "it requires /api/unsecure, which mass-erases the "
                       "whole FLASH array",
            **result,
        })
    except (BdmClientError, ValueError) as e:
        return api_error(e)


@app.route("/api/unsecure", methods=["POST"])
def api_unsecure():
    """Forgot-passcode / full-wipe recovery. ERASES THE ENTIRE CHIP.

    POST /api/unsecure {"confirm":"WIPE", "backup":true}
    -> {"ok":true,"complete":true,"secured_before":true,"secured_after":false,
        "erased":true,"error":null,
        "steps":[{"step":"read_security","ok":true,"fopt":195,...},
                 {"step":"flash_init_clock","ok":true,...},
                 {"step":"mass_erase","ok":true,"fstat":192,...},
                 {"step":"blank_check","ok":true,"blank":true,...},
                 {"step":"flash_readable","ok":true,"addr":57344,"value":255},
                 {"step":"security_released","ok":true,...},
                 {"step":"restore_nvopt","ok":true,"wrote":254,"read":254}],
        "backup":{...}}

    The steps are the point: mass erase ALONE DOES NOT UNSECURE AN HCS08
    (CONTEXT.md Finding 96) -- it is the blank check / erase-verify that
    releases security -- so each step reports separately and a failure is
    attributable to one of them. NVOPT is restored at the end because a
    freshly erased NVOPT reads 0xFF = SEC 1:1, which would re-secure the
    part at the next reset.
    """
    try:
        body = json_body()
        if body.get("confirm") != "WIPE":
            return api_error(
                'refusing to wipe without {"confirm":"WIPE"} -- this mass-'
                "erases the entire FLASH array, including any program on it"
            )
        want_backup = body.get("backup", True)
        with client.transaction():
            backup = _auto_backup("before-wipe") if want_backup else \
                {"taken": False, "reason": "not requested"}
            rep = client.unsecure(bus_freq_hz=_last_bus_hz)
        status = 200 if rep.get("complete") else 400
        return jsonify({"ok": bool(rep.get("complete")), "backup": backup,
                        **rep}), status
    except (BdmClientError, ValueError) as e:
        return api_error(e)


@app.route("/api/dump")
def api_dump():
    """Read a region back off the chip. Use before anything destructive.

    GET /api/dump[?addr=0xE000][&len=8192][&format=hex|s19][&save=1]
    -> {"ok":true,"addr":57344,"length":8192,"format":"hex",
        "hex":"8b899efe...", "blank":false,
        "saved":"D:\\...\\host\\backups\\20260915-231500_dump_E000-FFFF.s19"}
    format=s19 returns {"s19":"S0...\\nS1..."} instead of "hex".

    Defaults to the whole 8 KB FLASH array. ~1.6 ms/byte, so a full dump is
    about 13 s; it holds the serial lock for that time (the live-state
    poller reports busy rather than queueing).
    """
    try:
        addr = addr_arg(request.args.get("addr", FLASH_START))
        length = to_int(request.args.get("len", FLASH_END - addr + 1), "len")
        check_range(length, "len", 1, ADDR_MAX + 1)
        if addr + length - 1 > ADDR_MAX:
            return api_error("dump would run past the end of the address space")
        fmt = request.args.get("format", "hex")
        if fmt not in ("hex", "s19"):
            return api_error("format must be 'hex' or 's19'")
        with client.transaction():
            data = _dump_region(addr, length)
        resp = {"ok": True, "addr": addr, "length": length, "format": fmt,
                "blank": all(b == 0xFF for b in data),
                "all_zero": all(b == 0x00 for b in data)}
        if resp["all_zero"]:
            resp["note"] = ("every byte read 0x00, which is what a SECURED "
                            "part returns for all of FLASH and RAM -- check "
                            "/api/security before treating this as content")
        if fmt == "s19":
            resp["s19"] = build_srec([(addr, data)], header="PICOBDM-DUMP")
        else:
            resp["hex"] = data.hex()
        if _bool_arg(request.args.get("save")):
            os.makedirs(BACKUP_DIR, exist_ok=True)
            path = os.path.join(BACKUP_DIR, "%s_dump_%04X-%04X.s19" % (
                time.strftime("%Y%m%d-%H%M%S"), addr, addr + length - 1))
            with open(path, "w") as fh:
                fh.write(build_srec([(addr, data)], header="PICOBDM-DUMP"))
            resp["saved"] = path
        return jsonify(resp)
    except (BdmClientError, ValueError) as e:
        return api_error(e)


def _verify_chunks(chunks, max_report=64):
    """Read each chunk back and diff it. Call inside a transaction."""
    mismatches = []
    total = 0
    checked = 0
    for addr, expect in chunks:
        actual = _dump_region(addr, len(expect))
        checked += len(expect)
        for i, (e, a) in enumerate(zip(expect, actual)):
            if e != a:
                total += 1
                if len(mismatches) < max_report:
                    mismatches.append(
                        {"addr": addr + i, "expected": e, "actual": a}
                    )
    return {"bytes_checked": checked, "mismatches": mismatches,
            "mismatch_count": total, "match": total == 0,
            "truncated": total > len(mismatches)}


@app.route("/api/verify", methods=["POST"])
def api_verify():
    """Standalone read-back-and-compare. Works right after programming, and
    just as well months later to answer "did this chip's contents change".

    Two request forms:
      multipart/form-data with 'file' = an .s19      (compares every record)
      application/json {"addr":57344,"hex":"8b899e"} (compares a byte run)

    -> {"ok":true,"match":false,"bytes_checked":195,"mismatch_count":2,
        "truncated":false,
        "mismatches":[{"addr":57345,"expected":137,"actual":255}, ...],
        "chunks":[{"addr":57344,"len":192}, ...]}

    `match` is the answer; `mismatches` is capped at 64 entries with
    `mismatch_count` giving the true total, so a blank chip compared against
    a full image reports usefully instead of returning 8192 rows.
    """
    try:
        if "file" in request.files:
            try:
                text = request.files["file"].read().decode()
            except UnicodeDecodeError:
                return api_error("uploaded file is not text -- expected an "
                                 "S-record file")
            chunks = merge_contiguous(parse_srec(text))
            if not chunks:
                return api_error("no data records found in file")
        else:
            body = json_body()
            addr = addr_arg(body["addr"])
            hexstr = str(body["hex"]).strip().replace(" ", "")
            try:
                data = bytes.fromhex(hexstr)
            except ValueError:
                return api_error("'hex' is not a hex byte string")
            if not data:
                return api_error("'hex' is empty")
            chunks = [(addr, data)]
        for addr, data in chunks:
            check_range(addr, "record address", 0, ADDR_MAX)
            check_range(addr + len(data) - 1, "record end address", 0, ADDR_MAX)
        with client.transaction():
            result = _verify_chunks(chunks)
        return jsonify({
            "ok": True,
            "chunks": [{"addr": a, "len": len(d)} for a, d in chunks],
            **result,
        })
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


if __name__ == "__main__":
    # debug=True keeps Werkzeug's interactive traceback page, which is
    # genuinely useful on a local bench tool.
    #
    # use_reloader=False is deliberate: the reloader restarts the whole
    # process on any .py change, and this process OWNS a live serial
    # connection to the Pico. A restart silently drops that connection
    # (and the UI then shows "connected" against a dead handle), which is
    # the most likely reason this server kept failing to survive between
    # sessions. Tradeoff: you must restart it by hand after editing host
    # code. That is the right default for a tool holding hardware state.
    app.run(debug=True, use_reloader=False)
