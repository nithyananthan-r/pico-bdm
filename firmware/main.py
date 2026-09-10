"""
main.py — runs automatically on Pico boot (MicroPython). Reads
newline-delimited JSON commands from USB serial (stdin), dispatches them
to the Bdc driver, and writes newline-delimited JSON responses to stdout.

See /README.md at the repo root for the wire protocol.
"""

import sys
import ujson as json
import binascii

from bdc import Bdc, BdcError

# ---------------------------------------------------------------------------
# Pin assignment -- change to match your wiring.
# ---------------------------------------------------------------------------
BKGD_GPIO = 15
RESET_GPIO = 14

bdc = Bdc(bkgd_pin=BKGD_GPIO, reset_pin=RESET_GPIO)


def ok(**fields):
    fields["ok"] = True
    return fields


def err(message):
    return {"ok": False, "error": str(message)}


def handle(cmd):
    name = cmd.get("cmd")

    if name == "ping":
        return ok(pong=True)

    if name == "hw_reset_to_bdm":
        bdc.hardware_reset_to_bdm()
        return ok()

    if name == "sync":
        freq = bdc.sync()
        return ok(target_freq_hz=freq)

    if name == "read_byte":
        v = bdc.read_byte(cmd["addr"])
        return ok(value=v)

    if name == "write_byte":
        bdc.write_byte(cmd["addr"], cmd["value"])
        return ok()

    if name == "read_block":
        data = bdc.read_block(cmd["addr"], cmd["len"])
        return ok(data_b64=binascii.b2a_base64(data).decode().strip())

    if name == "write_block":
        data = binascii.a2b_base64(cmd["data_b64"])
        bdc.write_block(cmd["addr"], data)
        return ok()

    if name == "read_status":
        return ok(value=bdc.read_status())

    if name == "background":
        bdc.background()
        return ok()

    if name == "go":
        bdc.go()
        return ok()

    if name == "step":
        bdc.trace1()
        return ok()

    if name == "tagged_go":
        bdc.tagged_go()
        return ok()

    if name == "write_control":
        bdc.write_control(cmd["value"])
        return ok()

    if name == "read_bkpt":
        return ok(value=bdc.read_bkpt())

    if name == "write_bkpt":
        bdc.write_bkpt(cmd["addr"])
        return ok()

    if name == "read_reg":
        return ok(value=bdc.read_reg(cmd["reg"]))

    if name == "write_reg":
        bdc.write_reg(cmd["reg"], cmd["value"])
        return ok()

    if name == "reset_target":
        bdc.reset_target()
        return ok()

    if name == "blank_check":
        return ok(blank=bdc.flash_blank_check())

    if name == "flash_init_clock":
        bdc.flash_init_clock(cmd["bus_freq_hz"])
        return ok()

    if name == "flash_erase_page":
        bdc.flash_erase_page(cmd["addr"])
        return ok()

    if name == "mass_erase":
        bdc.flash_mass_erase()
        return ok()

    if name == "capture":
        test = cmd.get("test")
        sample_count = cmd.get("sample_count", 256)
        if test == "sync":
            result = bdc.capture_sync(sample_count)
        elif test == "write_bit":
            result = bdc.capture_write_bit(cmd.get("bit", 1), sample_count)
        elif test == "read_bit":
            result = bdc.capture_read_bit(sample_count)
        else:
            return err("unknown capture test: %r" % test)
        # pack samples (list of 0/1 ints) into bytes for compact transfer
        bits = result["samples"]
        packed = bytearray((len(bits) + 7) // 8)
        for i, b in enumerate(bits):
            if b:
                packed[i // 8] |= 1 << (7 - (i % 8))
        resp = ok(
            sample_period_ns=result["sample_period_ns"],
            n_samples=len(bits),
            samples_b64=binascii.b2a_base64(bytes(packed)).decode().strip(),
        )
        if "bit_value" in result:
            resp["bit_value"] = result["bit_value"]
        return resp

    if name == "flash_write":
        data = binascii.a2b_base64(cmd["data_b64"])
        n = bdc.flash_write_region(
            cmd["addr"], data, erase_pages=cmd.get("erase_pages", True)
        )
        return ok(bytes_written=n)

    return err("unknown command: %r" % name)


def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            continue
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
            resp = handle(cmd)
        except BdcError as e:
            resp = err(e)
        except Exception as e:  # noqa: broad - this is a serial command loop
            resp = err("%s: %s" % (type(e).__name__, e))
        print(json.dumps(resp))


main()
