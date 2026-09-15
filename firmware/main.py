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
POWER_GPIO = 12

bdc = Bdc(bkgd_pin=BKGD_GPIO, reset_pin=RESET_GPIO, power_pin=POWER_GPIO)


def ok(**fields):
    fields["ok"] = True
    return fields


def err(message):
    return {"ok": False, "error": str(message)}


def handle(cmd):
    name = cmd.get("cmd")

    if name == "ping":
        return ok(pong=True)

    if name == "power_on_reset_to_bdm":
        kw = {}
        for k in ("off_ms", "on_reset_hold_ms", "bkgd_hold_us",
                  "release_bkgd", "hold_reset", "settle_ms"):
            if k in cmd:
                kw[k] = cmd[k]
        bdc.power_on_reset_to_bdm(**kw)
        return ok()

    if name == "pad":
        return ok(**bdc.pad(cmd.get("pin", BKGD_GPIO)))

    if name == "pad_policy":
        return ok(**bdc.pad_policy(cmd.get("pue", 1), cmd.get("pde", 0),
                                   cmd.get("drive", 3)))

    if name == "bdm_connect":
        return ok(**bdc.bdm_connect(ms=int(cmd.get("ms", 25))))

    if name == "bdm_check":
        return ok(**bdc.bdm_check(int(cmd.get("ms", 30))))

    if name == "hw_reset_to_bdm":
        kw = {}
        for k in ("settle_ms", "reset_ms", "bkgd_hold_us", "pre_low_ms",
                  "release_bkgd"):
            if k in cmd:
                kw[k] = cmd[k]
        bdc.hardware_reset_to_bdm(**kw)
        return ok()

    if name == "sync":
        freq = bdc.sync()
        return ok(target_freq_hz=freq)

    if name == "sync_locked":
        # Phase-locked SYNC — see Bdc.sync_locked. This is the one that
        # actually works against a free-running (blank-FLASH) S08.
        kw = {}
        for k in ("host_low_us", "d_from", "d_to", "d_step", "trials"):
            if k in cmd:
                kw[k] = cmd[k]
        return ok(**bdc.sync_locked(**kw))

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
        # `arm` also sets BDCSCR.BKPTEN -- without it the breakpoint does
        # nothing at all (see Bdc.write_bkpt). Default on.
        bdc.write_bkpt(
            cmd["addr"],
            arm=cmd.get("arm", True),
            tag_mode=cmd.get("tag_mode", False),
        )
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

    if name == "raw_xfer":
        # {"cmd":"raw_xfer","tx":[0xE4],"nbits":8}
        v, tx_us, gap_us, rx_us = bdc.raw_xfer(
            [int(b) for b in cmd.get("tx", [])], int(cmd.get("nbits", 0))
        )
        return ok(value=v, tx_us=tx_us, gap_us=gap_us, rx_us=rx_us)

    if name == "raw_xfer_full":
        # {"cmd":"raw_xfer_full","tx":[0xE0,0x18,0x07],"nbits":8}
        # Same transfer as raw_xfer, but also returns the bytes sampled
        # while WE were driving ("echo"). Those must equal `tx`; if they
        # don't, the bit grid slipped and `value` means nothing. Telling
        # "the target said nothing" apart from "we lost bit alignment" by
        # hand used to cost whole sessions.
        return ok(**bdc.raw_xfer_full(
            [int(b) for b in cmd.get("tx", [])], int(cmd.get("nbits", 0))
        ))

    if name == "after_reset_sync":
        # {"cmd":"after_reset_sync","delay_us":N,"host_low_us":M}
        return ok(raw=bdc.after_reset_sync(
            int(cmd.get("delay_us", 0)), float(cmd.get("host_low_us", 20))))

    if name == "after_reset_xfer":
        res = bdc.after_reset_xfer(
            int(cmd.get("delay_us", 0)),
            [int(b) for b in cmd.get("tx", [])],
            int(cmd.get("sample_count", 128)),
            float(cmd.get("window_us", 20.0)),
            int(cmd.get("nbits", 0)),
        )
        bits = res["samples"]
        packed = bytearray((len(bits) + 7) // 8)
        for i, b in enumerate(bits):
            if b:
                packed[i // 8] |= 1 << (7 - (i % 8))
        return ok(sample_period_ns=res["sample_period_ns"],
                  n_samples=len(bits),
                  value=res.get("value"),
                  spin_us=res.get("spin_us"), gap_us=res.get("gap_us"),
                  samples_b64=binascii.b2a_base64(bytes(packed)).decode().strip())

    if name == "scan_xfer":
        # {"cmd":"scan_xfer","tx":[0xD5],"d_from":0,"d_to":44,"d_step":1,
        #  "trials":2}  -- phase scan done entirely on the Pico; see
        # Bdc.scan_xfer. Returns max target-driven low per phase.
        return ok(**bdc.scan_xfer(
            [int(b) for b in cmd.get("tx", [])],
            int(cmd.get("d_from", 0)),
            int(cmd.get("d_to", 44)),
            int(cmd.get("d_step", 1)),
            int(cmd.get("trials", 2)),
            int(cmd.get("sample_count", 128)),
            float(cmd.get("window_us", 20.0)),
            cmd.get("skip_us"),
        ))

    if name == "scan_wave":
        # {"cmd":"scan_wave","words":[...],"n_cycles":N,...} — arbitrary
        # cycle-by-cycle BKGD waveform, phase-scanned. See Bdc.scan_wave.
        return ok(**bdc.scan_wave(
            [int(w) for w in cmd.get("words", [])],
            int(cmd.get("n_cycles", 0)),
            int(cmd.get("d_from", 0)),
            int(cmd.get("d_to", 44)),
            int(cmd.get("d_step", 1)),
            int(cmd.get("trials", 4)),
            int(cmd.get("sample_count", 128)),
            float(cmd.get("window_us", 20.0)),
            float(cmd.get("skip_us", 0.0)),
        ))

    if name == "capture_wave":
        r = bdc.capture_wave(
            [int(w) for w in cmd.get("words", [])],
            int(cmd.get("n_cycles", 0)),
            int(cmd.get("sample_count", 128)),
            float(cmd.get("window_us", 8.0)),
        )
        bits = r["samples"]
        packed = bytearray((len(bits) + 7) // 8)
        for i, b in enumerate(bits):
            if b:
                packed[i // 8] |= 1 << (7 - (i % 8))
        return ok(sample_period_ns=r["sample_period_ns"],
                  n_samples=len(bits),
                  samples_b64=binascii.b2a_base64(bytes(packed)).decode().strip())

    if name == "set_bit_clock":
        return ok(hz=bdc.set_bit_clock(cmd["hz"]))

    if name == "capture2":
        r = bdc.capture2_xfer(
            [int(b) for b in cmd.get("tx", [])],
            int(cmd.get("nbits", 0)),
            int(cmd.get("sample_count", 256)),
            float(cmd.get("window_us", 200)),
            cmd.get("drive", "xfer"),
            cmd.get("host_low_us"),
            cmd.get("trigger", "bkgd"),
            cmd.get("bdm_opts"),
        )
        def _pack(bits):
            buf = bytearray((len(bits) + 7) // 8)
            for i, b in enumerate(bits):
                if b:
                    buf[i // 8] |= 1 << (7 - (i % 8))
            return binascii.b2a_base64(bytes(buf)).decode().strip()
        resp = ok(
            sample_period_ns=r["sample_period_ns"],
            n_samples=len(r["bkgd"]),
            bkgd_b64=_pack(r["bkgd"]),
            reset_b64=_pack(r["reset"]),
        )
        if "value" in r:
            resp["value"] = r["value"]
        if "sync_raw" in r:
            resp["sync_raw"] = r["sync_raw"]
        if "xfer_error" in r:
            resp["xfer_error"] = r["xfer_error"]
        return resp

    if name == "probe_pin":
        return ok(**bdc.probe_pin(int(cmd["pin"]), int(cmd.get("ms", 30)),
                                  cmd.get("pull")))

    if name == "rise_time":
        return ok(**bdc.rise_time(
            int(cmd["pin"]), cmd.get("hold_ms", 1), cmd.get("trials", 6)))

    if name == "drive_pin":
        return ok(**bdc.drive_pin(int(cmd["pin"]), cmd.get("level")))

    if name == "capture":
        test = cmd.get("test")
        # bdc_sample packs 32 samples per RX FIFO word, so the count must be
        # a positive multiple of 32 or _capture()'s word accounting is wrong.
        # (Bdc._capture re-checks this; validated here too so a bad request
        # is rejected before any state machine is touched.)
        try:
            sample_count = int(cmd.get("sample_count", 256))
        except (TypeError, ValueError):
            return err("sample_count must be an integer")
        if sample_count <= 0 or sample_count % 32 != 0:
            return err("sample_count must be a positive multiple of 32")
        # Optional: ask for a capture spanning roughly this much real time
        # instead of the default bit-level zoom. Being able to zoom out is
        # what made this feature actually useful -- see CONTEXT.md.
        window_us = cmd.get("window_us")
        if window_us is not None:
            try:
                window_us = float(window_us)
            except (TypeError, ValueError):
                return err("window_us must be a number")
            if window_us <= 0:
                return err("window_us must be positive")
        if test == "sync":
            result = bdc.capture_sync(sample_count, window_us or 600)
        elif test == "write_bit":
            result = bdc.capture_write_bit(
                cmd.get("bit", 1), sample_count, window_us
            )
        elif test == "read_bit":
            result = bdc.capture_read_bit(sample_count, window_us)
        elif test == "command":
            result = bdc.capture_command(
                cmd.get("opcode", 0xE4), cmd.get("addr"),
                sample_count, window_us,
            )
        elif test == "xfer":
            # whole transaction: command bytes AND the read phase
            result = bdc.capture_xfer(
                [int(b) for b in cmd.get("tx", [])],
                int(cmd.get("nbits", 0)),
                sample_count, window_us,
            )
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
        for extra in ("bit_value", "value", "tx_us", "gap_us", "rx_us",
                      "xfer_error"):
            if extra in result:
                resp[extra] = result[extra]
        return resp

    if name == "eval":
        # DIAGNOSTIC ESCAPE HATCH (added in the TENTH session).
        #
        # Every experiment this project has ever run needed a new JSON
        # command in this file plus an mpremote redeploy plus a reset --
        # roughly a minute of turnaround for a one-line question like
        # "what does bdc.read_reg('SP') say". `eval` evaluates a Python
        # expression in this module's namespace (so `bdc` is in scope) and
        # returns its repr as a string, plus the value itself when it is
        # JSON-serialisable.
        #
        # It is only reachable over the local USB serial link, which is
        # already a full control channel for the target -- anything that
        # can send JSON here can already erase the chip.
        expr = cmd["expr"]
        v = eval(expr)                                  # noqa: S307
        try:
            json.dumps(v)
        except Exception:
            v = repr(v)
        return ok(value=v, repr=repr(v))

    if name == "exec":
        exec(cmd["src"], globals())                     # noqa: S102
        return ok()

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
        # NOTE: the serialize-and-print step is INSIDE the try on purpose.
        # It used to sit outside it, so a response that failed to serialize
        # would propagate out of main() and kill the command loop entirely
        # -- from the host's point of view the Pico would just stop
        # answering (it had actually dropped to the REPL).
        try:
            cmd = json.loads(line)
            resp = handle(cmd)
            print(json.dumps(resp))
        except BdcError as e:
            print(json.dumps(err(e)))
        except Exception as e:  # noqa: broad - this is a serial command loop
            try:
                print(json.dumps(err("%s: %s" % (type(e).__name__, e))))
            except Exception:
                # Last resort: emit a hand-built JSON line so the host still
                # sees *something* well-formed and stays in sync.
                print('{"ok": false, "error": "unserializable response"}')


main()
