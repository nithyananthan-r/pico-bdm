"""
bdm_client.py — thin Python client for talking to the Pico firmware's
newline-delimited JSON serial protocol.
"""

import contextlib
import json
import threading
import base64
import serial
import serial.tools.list_ports


class BdmClientError(Exception):
    pass


class BdmBusyError(BdmClientError):
    """The serial link is held by another operation (a flash write, a wipe).

    Raised only by try_transaction(), i.e. by callers that would rather be
    told "busy" immediately than queue behind a 30-second FLASH operation.
    The live-state poller is exactly that caller: blocking there would pile
    up a request per poll tick for the whole duration of a flash write and
    then deliver a burst of stale snapshots when it finished.
    """
    pass


class BdmClient:
    def __init__(self):
        self._ser = None
        # RLock, not Lock: transaction() takes it for a whole multi-command
        # sequence and every _call() inside that sequence takes it again.
        self._lock = threading.RLock()

    @staticmethod
    def list_ports():
        return [p.device for p in serial.tools.list_ports.comports()]

    def connect(self, port, baud=115200, timeout=5):
        """Open `port`. A failed open leaves any EXISTING connection alone
        and self._ser untouched -- the old code assigned the result of
        serial.Serial() directly, so a failure mid-open could leave stale
        state behind and the caller's error handler would then close a
        connection that was still perfectly good."""
        with self._lock:
            new_ser = serial.Serial(port, baudrate=baud, timeout=timeout)
            # Only swap in once the open actually succeeded.
            old = self._ser
            self._ser = new_ser
            if old is not None and old is not new_ser:
                try:
                    old.close()
                except Exception:
                    pass

    def disconnect(self):
        # Guarded by the lock so it can't race a concurrent _call() and
        # turn self._ser into None mid-transaction (which surfaced as an
        # AttributeError -> Flask 500).
        with self._lock:
            if self._ser:
                try:
                    self._ser.close()
                finally:
                    self._ser = None

    @property
    def is_connected(self):
        return self._ser is not None and self._ser.is_open

    @property
    def port(self):
        """Name of the open port, or None. Lets the app tell 'connect to
        the port I am already on' (idempotent) apart from 'connect to a
        different port' (a real switch)."""
        return self._ser.port if self._ser is not None else None

    @contextlib.contextmanager
    def transaction(self):
        """Hold the serial lock across a whole logical operation.

        _call() locks one JSON round-trip at a time, which is not enough
        for anything the target treats as a sequence -- a FLASH command is
        three consecutive register writes, and another browser tab poking
        /api/read_byte in between is exactly how you get a FACCERR (which,
        per the datasheet, then blocks every later FLASH command).

        Usage:
            with client.transaction():
                client.flash_init_clock(...)
                client.flash_write(...)
        """
        with self._lock:
            yield self

    @contextlib.contextmanager
    def try_transaction(self, timeout=0.05):
        """transaction(), but give up instead of queueing.

        Nothing here is preemptive: the RLock is fair enough that a poller
        cannot starve a flash write, and a flash write cannot deadlock a
        poller -- it just makes it wait. For a poll, waiting is the wrong
        answer, so this raises BdmBusyError and the route reports busy.
        """
        if not self._lock.acquire(timeout=timeout):
            raise BdmBusyError("the BDM link is busy with another operation")
        try:
            yield self
        finally:
            self._lock.release()

    @property
    def busy(self):
        """True if some other thread currently holds the link."""
        if not self._lock.acquire(blocking=False):
            return True
        self._lock.release()
        return False

    def _call(self, cmd_dict, timeout=5):
        if not self.is_connected:
            raise BdmClientError("not connected to a Pico")
        with self._lock:
            self._ser.timeout = timeout
            line = json.dumps(cmd_dict) + "\n"
            self._ser.write(line.encode())
            resp_line = self._ser.readline()
            if not resp_line:
                # Flush before raising. If the Pico's answer is merely LATE
                # rather than absent, leaving it in the input buffer
                # desynchronises every subsequent call -- each one would
                # read the *previous* command's response forever after.
                try:
                    self._ser.reset_input_buffer()
                except Exception:
                    pass
                raise BdmClientError("timeout waiting for Pico response")
            try:
                resp = json.loads(resp_line.decode().strip())
            except ValueError:
                # Same reasoning: a garbled line probably means we're out of
                # step, so drop whatever else is buffered.
                try:
                    self._ser.reset_input_buffer()
                except Exception:
                    pass
                raise BdmClientError("bad response from Pico: %r" % resp_line)
        if not resp.get("ok"):
            raise BdmClientError(resp.get("error", "unknown error"))
        return resp

    # -- convenience wrappers ----------------------------------------------
    def ping(self):
        return self._call({"cmd": "ping"})

    def hw_reset_to_bdm(self):
        """Pulse RESET with BKGD held low.

        *** THIS DOES NOT ENTER BDM ON THE MC9S08SG8. *** Datasheet
        Sec 17.1.1: external-pin and internally-generated resets both
        IGNORE BKGD; only a power-on reset latches active background mode.
        Kept because it is a legitimate diagnostic, but
        power_on_reset_to_bdm() is the entry that works -- see CONTEXT.md
        Finding 63 and the TENTH session.
        """
        return self._call({"cmd": "hw_reset_to_bdm"}, timeout=15)

    def power_on_reset_to_bdm(self):
        """The real BDM entry on this part: cut VDD (which is on a Pico
        GPIO), bring it back with RESET and BKGD both held low, then
        release RESET and BKGD. Takes ~150 ms on the Pico, so the serial
        timeout has to be generous."""
        return self._call({"cmd": "power_on_reset_to_bdm"}, timeout=20)

    def sync(self):
        return self._call({"cmd": "sync"}, timeout=20)

    def set_bit_clock(self, hz):
        return self._call({"cmd": "set_bit_clock", "hz": int(hz)})["hz"]

    def read_byte(self, addr):
        return self._call({"cmd": "read_byte", "addr": addr})["value"]

    def write_byte(self, addr, value):
        self._call({"cmd": "write_byte", "addr": addr, "value": value})

    def read_block(self, addr, length):
        resp = self._call({"cmd": "read_block", "addr": addr, "len": length})
        return base64.b64decode(resp["data_b64"])

    def write_block(self, addr, data: bytes):
        b64 = base64.b64encode(data).decode()
        self._call({"cmd": "write_block", "addr": addr, "data_b64": b64})

    def read_status(self):
        return self._call({"cmd": "read_status"})["value"]

    def background(self):
        """BDC BACKGROUND: halt the CPU into active background mode.

        The firmware has had this verb since the start; the host had no
        wrapper for it, so a UI could resume a target (go/step) but never
        stop one without a power-on re-entry. ENBDM must already be 1 for
        BACKGROUND to take effect (Finding 100); the caller checks BDCSCR.
        """
        self._call({"cmd": "background"})

    def go(self):
        self._call({"cmd": "go"})

    def step(self):
        self._call({"cmd": "step"})

    def tagged_go(self):
        self._call({"cmd": "tagged_go"})

    def write_control(self, value):
        self._call({"cmd": "write_control", "value": value})

    def read_bkpt(self):
        return self._call({"cmd": "read_bkpt"})["value"]

    def write_bkpt(self, addr):
        self._call({"cmd": "write_bkpt", "addr": addr})

    def read_reg(self, reg):
        return self._call({"cmd": "read_reg", "reg": reg})["value"]

    def write_reg(self, reg, value):
        self._call({"cmd": "write_reg", "reg": reg, "value": value})

    def reset_target(self):
        self._call({"cmd": "reset_target"})

    def blank_check(self):
        return self._call({"cmd": "blank_check"}, timeout=5)["blank"]

    def flash_init_clock(self, bus_freq_hz):
        self._call({"cmd": "flash_init_clock", "bus_freq_hz": bus_freq_hz})

    def flash_erase_page(self, addr):
        self._call({"cmd": "flash_erase_page", "addr": addr}, timeout=5)

    def mass_erase(self):
        self._call({"cmd": "mass_erase"}, timeout=10)

    def capture(self, test, sample_count=256, bit=1):
        req = {"cmd": "capture", "test": test, "sample_count": sample_count}
        if test == "write_bit":
            req["bit"] = bit
        resp = self._call(req, timeout=5)
        packed = base64.b64decode(resp["samples_b64"])
        n = resp["n_samples"]
        bits = []
        for i in range(n):
            byte = packed[i // 8]
            bits.append((byte >> (7 - (i % 8))) & 1)
        return {
            "samples": bits,
            "sample_period_ns": resp["sample_period_ns"],
            "bit_value": resp.get("bit_value"),
        }

    # -- ELEVENTH session: safety, security, and state ---------------------
    def chip_info(self, bus_freq_hz=None, blank_check=False):
        req = {"cmd": "chip_info", "blank_check": bool(blank_check)}
        if bus_freq_hz:
            req["bus_freq_hz"] = int(bus_freq_hz)
        resp = self._call(req, timeout=15)
        resp.pop("ok", None)
        return resp

    def identify(self, ram_probe=True, defect_scan=False):
        resp = self._call(
            {"cmd": "identify", "ram_probe": bool(ram_probe),
             "defect_scan": bool(defect_scan)},
            timeout=20,
        )
        resp.pop("ok", None)
        return resp

    def live_state(self, cpu_regs=True, ports=True):
        resp = self._call(
            {"cmd": "live_state", "cpu_regs": bool(cpu_regs),
             "ports": bool(ports)},
            timeout=10,
        )
        resp.pop("ok", None)
        return resp

    def relink(self):
        """Recover the link to a RUNNING target without power-cycling it."""
        resp = self._call({"cmd": "relink"}, timeout=30)
        resp.pop("ok", None)
        return resp

    def power_state(self):
        resp = self._call({"cmd": "power_state"}, timeout=10)
        resp.pop("ok", None)
        return resp

    def security_state(self):
        resp = self._call({"cmd": "security"}, timeout=10)
        resp.pop("ok", None)
        return resp

    def set_security(self, nvopt=0xFC, bus_freq_hz=None):
        req = {"cmd": "set_security", "nvopt": int(nvopt)}
        if bus_freq_hz:
            req["bus_freq_hz"] = int(bus_freq_hz)
        resp = self._call(req, timeout=20)
        resp.pop("ok", None)
        return resp

    def unsecure(self, bus_freq_hz=None, restore_nvopt=0xFE):
        """Mass erase + erase-verify + NVOPT restore, reported step by step.

        Generous timeout: a mass erase plus a blank check plus their FSTAT
        polling is seconds of target time, and the Pico answers only when
        the whole sequence is done.
        """
        req = {"cmd": "unsecure"}
        if bus_freq_hz:
            req["bus_freq_hz"] = int(bus_freq_hz)
        if restore_nvopt is not None:
            req["restore_nvopt"] = int(restore_nvopt)
        resp = self._call(req, timeout=60)
        resp.pop("ok", None)
        return resp

    def flash_image(self, chunks, bus_freq_hz=None, erase=True, nvopt=0xFE,
                    verify=True, timeout=180):
        """chunks: iterable of (addr, bytes). Page-at-a-time on the Pico."""
        req = {
            "cmd": "flash_image",
            "chunks": [
                {"addr": int(a), "data_b64": base64.b64encode(bytes(d)).decode()}
                for a, d in chunks
            ],
            "erase": bool(erase),
            "nvopt": int(nvopt),
            "verify": bool(verify),
        }
        if bus_freq_hz:
            req["bus_freq_hz"] = int(bus_freq_hz)
        resp = self._call(req, timeout=timeout)
        resp.pop("ok", None)
        return resp

    def flash_write(self, addr, data: bytes, erase_pages=True):
        b64 = base64.b64encode(data).decode()
        resp = self._call(
            {
                "cmd": "flash_write",
                "addr": addr,
                "data_b64": b64,
                "erase_pages": erase_pages,
            },
            timeout=30,
        )
        return resp["bytes_written"]


# module-level singleton used by the Flask app
client = BdmClient()
