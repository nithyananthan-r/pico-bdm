"""
app.py — Flask host application for pico-bdm.

Run with:
    python app.py
then open http://127.0.0.1:5000
"""

from flask import Flask, jsonify, request, render_template
from bdm_client import client, BdmClientError
from srec import parse_srec, merge_contiguous

app = Flask(__name__)


def api_error(exc, status=400):
    return jsonify({"ok": False, "error": str(exc)}), status


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ports")
def api_ports():
    return jsonify({"ok": True, "ports": client.list_ports()})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    port = request.json.get("port")
    if not port:
        return api_error("missing 'port'")
    try:
        client.connect(port)
        client.ping()
        return jsonify({"ok": True})
    except (BdmClientError, OSError) as e:
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
    try:
        client.hw_reset_to_bdm()
        freq = client.sync()["target_freq_hz"]
        return jsonify({"ok": True, "target_freq_hz": freq})
    except BdmClientError as e:
        return api_error(e)


@app.route("/api/read_byte")
def api_read_byte():
    try:
        addr = int(request.args["addr"], 0)
        value = client.read_byte(addr)
        return jsonify({"ok": True, "addr": addr, "value": value})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/write_byte", methods=["POST"])
def api_write_byte():
    try:
        addr = int(request.json["addr"], 0) if isinstance(request.json["addr"], str) else request.json["addr"]
        value = request.json["value"]
        client.write_byte(addr, value)
        return jsonify({"ok": True})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/read_block")
def api_read_block():
    try:
        addr = int(request.args["addr"], 0)
        length = int(request.args["len"], 0)
        data = client.read_block(addr, length)
        return jsonify({"ok": True, "addr": addr, "hex": data.hex()})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/mass_erase", methods=["POST"])
def api_mass_erase():
    try:
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
        value = request.json["value"]
        client.write_control(value)
        return jsonify({"ok": True})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/bkpt", methods=["GET", "POST"])
def api_bkpt():
    try:
        if request.method == "GET":
            return jsonify({"ok": True, "addr": client.read_bkpt()})
        addr = request.json["addr"]
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
        reg = request.json["reg"]
        value = request.json["value"]
        client.write_reg(reg, value)
        return jsonify({"ok": True})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/capture", methods=["POST"])
def api_capture():
    try:
        body = request.json or {}
        test = body.get("test", "sync")
        sample_count = int(body.get("sample_count", 256))
        bit = int(body.get("bit", 1))
        result = client.capture(test, sample_count=sample_count, bit=bit)
        return jsonify({"ok": True, **result})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


@app.route("/api/flash_srec", methods=["POST"])
def api_flash_srec():
    """
    Accepts a multipart-form upload with an .s19 file under 'file', plus a
    'bus_freq_hz' form field for FCDIV setup. Parses it, merges contiguous
    runs, and writes each run via flash_write.
    """
    try:
        bus_freq_hz = int(request.form.get("bus_freq_hz", "8000000"))
        erase_mode = request.form.get("erase_mode", "pages")  # pages | mass | none
        if erase_mode not in ("pages", "mass", "none"):
            return api_error("erase_mode must be 'pages', 'mass', or 'none'")
        verify = request.form.get("verify", "1") not in ("0", "false", "")
        f = request.files["file"]
        text = f.read().decode()
        chunks = merge_contiguous(parse_srec(text))
        if not chunks:
            return api_error("no data records found in file")

        client.flash_init_clock(bus_freq_hz)

        if erase_mode == "mass":
            client.mass_erase()
        erase_pages = erase_mode == "pages"

        results = []
        total = 0
        for addr, data in chunks:
            n = client.flash_write(addr, data, erase_pages=erase_pages)
            total += n
            if verify:
                readback = client.read_block(addr, len(data))
                if bytes(readback) != bytes(data):
                    raise BdmClientError(
                        "verify failed: region at 0x%04X doesn't match after write" % addr
                    )
            results.append({"addr": addr, "len": n})

        return jsonify({"ok": True, "chunks": results, "total_bytes": total})
    except (BdmClientError, KeyError, ValueError) as e:
        return api_error(e)


if __name__ == "__main__":
    app.run(debug=True)
