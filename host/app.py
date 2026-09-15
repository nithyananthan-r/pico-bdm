"""
app.py — Flask host application for pico-bdm.

Run with:
    python app.py
then open http://127.0.0.1:5000
"""

from flask import Flask, jsonify, request, render_template
from werkzeug.exceptions import HTTPException

from bdm_client import client, BdmClientError
from srec import parse_srec, merge_contiguous

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
        client.connect(port)
        client.ping()
        return jsonify({"ok": True})
    except (BdmClientError, OSError, ValueError) as e:
        # Only tear down if we actually got the port open. connect() no
        # longer clobbers a previous good connection on failure, so an
        # unconditional disconnect() here would close a connection that is
        # still fine.
        if client.is_connected:
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


def _pages_touched(chunks, page_size=FLASH_PAGE_SIZE):
    """Union of FLASH pages covered by every chunk.

    This is the fix for the C3 hazard: erasing and programming chunk by
    chunk means that if two chunks land in the SAME 512-byte page (routine
    in real toolchain output), programming chunk A and then erasing for
    chunk B wipes A -- and the per-chunk verify never notices, because it
    only re-reads the chunk it just wrote. Erase the union once, up front,
    then program every chunk with erasing turned off.
    """
    pages = set()
    for addr, data in chunks:
        if not data:
            continue
        first = addr - (addr % page_size)
        last = addr + len(data) - 1
        last -= last % page_size
        for page in range(first, last + 1, page_size):
            pages.add(page)
    return sorted(pages)


@app.route("/api/flash_srec", methods=["POST"])
def api_flash_srec():
    """
    Accepts a multipart-form upload with an .s19 file under 'file', plus a
    'bus_freq_hz' form field for FCDIV setup. Parses it, merges contiguous
    runs, erases the union of pages those runs touch, and writes each run.
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

        # One lock for the whole programming operation. A FLASH command is a
        # multi-write sequence on the target; letting another tab interleave
        # a read in the middle of it is exactly how you provoke a FACCERR.
        with client.transaction():
            client.flash_init_clock(bus_freq_hz)

            erased_pages = []
            if erase_mode == "mass":
                client.mass_erase()
            elif erase_mode == "pages":
                erased_pages = _pages_touched(chunks)
                for page in erased_pages:
                    client.flash_erase_page(page)

            results = []
            total = 0
            for addr, data in chunks:
                # Always erase_pages=False: erasing already happened above,
                # as a union across all chunks. Per-chunk erasing here would
                # reintroduce the clobber bug described in _pages_touched().
                #
                # Verification is NOT repeated here on purpose. firmware's
                # flash_write_region() already reads every byte back and
                # compares before returning, and a BDC read is one byte per
                # round-trip -- re-reading from the host would exactly
                # double the slowest operation in the stack for no extra
                # coverage. The UI's "Verify after write" checkbox is now
                # informational only; the firmware verify is unconditional.
                n = client.flash_write(addr, data, erase_pages=False)
                total += n
                results.append({"addr": addr, "len": n})

        return jsonify(
            {
                "ok": True,
                "chunks": results,
                "total_bytes": total,
                "pages_erased": len(erased_pages),
                "erase_mode": erase_mode,
            }
        )
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
