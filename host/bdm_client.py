"""
bdm_client.py — thin Python client for talking to the Pico firmware's
newline-delimited JSON serial protocol.
"""

import json
import threading
import base64
import serial
import serial.tools.list_ports


class BdmClientError(Exception):
    pass


class BdmClient:
    def __init__(self):
        self._ser = None
        self._lock = threading.Lock()

    @staticmethod
    def list_ports():
        return [p.device for p in serial.tools.list_ports.comports()]

    def connect(self, port, baud=115200, timeout=5):
        self._ser = serial.Serial(port, baudrate=baud, timeout=timeout)

    def disconnect(self):
        if self._ser:
            self._ser.close()
            self._ser = None

    @property
    def is_connected(self):
        return self._ser is not None and self._ser.is_open

    def _call(self, cmd_dict, timeout=5):
        if not self.is_connected:
            raise BdmClientError("not connected to a Pico")
        with self._lock:
            self._ser.timeout = timeout
            line = json.dumps(cmd_dict) + "\n"
            self._ser.write(line.encode())
            resp_line = self._ser.readline()
            if not resp_line:
                raise BdmClientError("timeout waiting for Pico response")
            try:
                resp = json.loads(resp_line.decode().strip())
            except ValueError:
                raise BdmClientError("bad response from Pico: %r" % resp_line)
        if not resp.get("ok"):
            raise BdmClientError(resp.get("error", "unknown error"))
        return resp

    # -- convenience wrappers ----------------------------------------------
    def ping(self):
        return self._call({"cmd": "ping"})

    def hw_reset_to_bdm(self):
        return self._call({"cmd": "hw_reset_to_bdm"})

    def sync(self):
        return self._call({"cmd": "sync"}, timeout=10)

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
