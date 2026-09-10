# pico-bdm — DIY BDM Programmer for S08/RS08 (e.g. S9S08SG8/SG4) using RP2040

A from-scratch Background Debug Mode (BDM) programmer built on a Raspberry Pi
Pico (RP2040), using its PIO state machines to generate the single-wire BDC
(Background Debug Controller) protocol used by Freescale/NXP S08 and RS08
parts — including the S9S08SG8E2MTJ / MC9S08SG8 family.

Two halves:

- **firmware/** — MicroPython code that runs *on* the Pico. Implements the
  BDC bit protocol in PIO (for the timing precision it needs) plus a simple
  JSON-over-USB-serial command interface.
- **host/** — A Flask web app that runs on your PC, talks to the Pico over
  USB serial, and gives you a browser UI to connect, read/write memory, and
  flash a compiled `.s19`/`.hex` image.

```
PC (browser) <--HTTP--> Flask app <--USB serial (JSON lines)--> Pico <--BKGD/RESET/GND/VDD--> Target MCU
```

## ⚠️ Read this before wiring anything up

This project implements a real, undocumented-in-detail hardware protocol
against **your specific chip**. It is a first-pass implementation based on
the publicly published S08/RS08 BDC protocol summary (see
`firmware/bdc_pio.py` docstring for the exact figures used: 16 target-clock
cycles per bit, 1-cycle-low for a '1', 13-cycle-low for a '0', SYNC = 128
target cycles). Those are the standard, widely-cited values for this
protocol family — but **verify them against a logic analyzer** before you
trust this against a chip you care about. This is completely normal practice
even for commercial BDM pod bring-up, not a sign anything is wrong.

Recommended first bring-up steps:
1. Wire everything up per the diagram below.
2. **No separate logic analyzer? Use the built-in Scope panel** (see
   "Self-capture / Scope" below) — it uses a second, passive PIO state
   machine on the Pico itself to record the actual waveform on BKGD around
   each test operation, so you can sanity-check timing without any extra
   hardware.
3. Run `sync` and confirm the Scope panel's "SYNC pulse" capture shows:
   host driving BKGD low for a long pulse, releasing, then the target
   responding with a pulse. If you have a real logic analyzer too (see
   Option 2/3 below), it's still worth cross-checking against that once —
   the self-capture is a great first-pass sanity check but it's not a fully
   independent observer (see caveat below).
4. Only then try `read_byte` against a known-safe read-only address (e.g.
   `0xFFFE` — the reset vector) before attempting any writes.
5. Do **not** attempt FLASH erase/program commands until steps 1–4 are
   confirmed working — mistakes here can brick the target's flash.

## Self-capture / Scope (no logic analyzer needed)

The **Scope** panel in the web UI runs one of three canned test operations
(SYNC, a single write-bit, a single read-bit) while a *second* PIO state
machine passively records the BKGD line and streams the waveform back for
display.

How it avoids needing precise timing coordination from Python: the capture
program arms itself, then executes a PIO `wait` instruction that blocks
until the line **first goes low** — that's the actual trigger. So it
doesn't matter that MicroPython's own call overhead is imprecise; the
capture only starts counting once the real edge happens, at full PIO clock
precision.

**Honest caveat:** this is not a fully independent observer — the same Pico
that's generating the signal is also capturing it, so it can't catch a bug
in the underlying PIO clock/counter itself, and both state machines share
the same crystal reference. What it *does* reliably catch: wrong pulse
widths, a SYNC that never gets a response (miswiring, dead target, wrong
power), or a read-bit sample landing in the wrong place. That covers the
large majority of first-bring-up failures. If results still look wrong or
inconsistent after this, that's the point to reach for genuinely
independent hardware:

- **A second Pico as a real logic analyzer** (~$4-5): see
  [PicoLogicAnalyzer](https://github.com/gusmanb/logicanalyzer) or
  [sigrok-pico](https://github.com/pico-coder/sigrok-pico), both work with
  the free [PulseView](https://sigrok.org/wiki/PulseView) GUI.
- **A cheap CY7C68013A-based USB logic analyzer clone** (~$5-10, common on
  AliExpress/Amazon as "24MHz 8CH Logic Analyzer") — also works with
  PulseView.

## Wiring

Confirmed against the MC9S08SG8/SG4 datasheet, Table 2-1 / Figure 2-1
(20-Pin TSSOP) — same pinout for both the SG8 and SG4 (they only differ in
FLASH/RAM size, not pin assignment):

| Pico pin        | Target pin (20-TSSOP)                      |
|------------------|--------------------------------------------|
| GPIO (BKGD_PIN)  | pin 2 — BKGD/MS                            |
| GPIO (RESET_PIN) | pin 1 — RESET                              |
| GND              | pin 4 — VSS                                |
| 3V3 (see note)   | pin 3 — VDD                                |
| —                | pins 5–20: Port A/B/C I/O, not needed for bring-up, safe to leave floating |

**Power note:** run the target at **3.3V**, not 5V. The S9S08SG8 datasheet
specifies a 2.7–5.5V operating range, so 3.3V is within spec and lets you
skip level shifting entirely — the Pico's GPIOs are 3.3V and not 5V-tolerant.
If you must run the target at 5V for some other reason, you need a
level-shifter or an open-drain buffer (e.g. 74LVC1G07) between the Pico and
BKGD, plus a way to sense a 5V RESET line safely.

Add a **10kΩ pull-up from BKGD to target VDD** even though the datasheet
says the target has an internal one — it makes the line more robust during
early bring-up and doesn't hurt.

Add a **0.1µF ceramic decoupling capacitor directly across VDD (pin 3) and
VSS (pin 4)**, as close to the chip as physically possible — datasheet
§2.2.1 calls this out explicitly as needed for reliable operation, not
just best practice. A larger bulk capacitor (e.g. 10µF) on the supply rail
is recommended too if the wire run from the Pico's 3V3 pin is long.

Datasheet §2.2.3 also confirms something the firmware already assumes:
**RESET alone cannot force BDM mode** — "RESET pin can only be used to
reset into user mode, you can not enter BDM using RESET pin. BDM can be
entered by holding MS [BKGD] low during POR or writing a 1 to BDFR in
SBDFR with MS low after issuing BDM command." This matches
`hardware_reset_to_bdm()` in `firmware/bdc.py`, which holds BKGD low
across a RESET pulse for exactly this reason.

Both RESET and BKGD are open-drain-style — the Pico code always sets a pin
to "input" (never drives it high) and only ever drives it low, exactly like
the pod hardware does.

## Repo layout

```
pico-bdm/
├── README.md                 <- you are here
├── CONTEXT.md                 <- project history, design decisions, and
│                                  what's verified vs. not — read this
│                                  before continuing work in a fresh chat
│                                  or in Claude Code
├── firmware/
│   ├── main.py                <- runs on the Pico; serial command dispatcher
│   ├── bdc_pio.py              <- PIO programs: sync, write-bit, read-bit
│   └── bdc.py                  <- higher-level BDC driver + flash routines
└── host/
    ├── requirements.txt
    ├── app.py                  <- Flask app / REST API
    ├── bdm_client.py            <- pyserial client talking to the Pico
    ├── templates/index.html
    └── static/app.js
```

## Setup

### 1. Flash MicroPython onto the Pico
Download the official MicroPython UF2 for Pico from micropython.org, hold
BOOTSEL, plug in, drag the UF2 onto the mass-storage drive it presents.

### 2. Copy firmware onto the Pico
Using `mpremote` (`pip install mpremote`) or the Thonny IDE's file manager:
```
mpremote cp firmware/bdc_pio.py :
mpremote cp firmware/bdc.py :
mpremote cp firmware/main.py :main.py
mpremote reset
```
(`main.py` on the Pico auto-runs on boot and starts the serial command loop.)

### 3. Run the host app
```
cd host
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py
```
Then open http://127.0.0.1:5000 in your browser. It'll list available serial
ports — pick the Pico's (usually `/dev/ttyACM0` on Linux, `COMx` on Windows,
`/dev/tty.usbmodem*` on macOS).

## Serial protocol (Pico ⇄ host)

Newline-delimited JSON, host always initiates:

```
--> {"cmd": "ping"}
<-- {"ok": true, "pong": true}

--> {"cmd": "sync"}
<-- {"ok": true, "target_freq_hz": 8000000}

--> {"cmd": "read_byte", "addr": 65534}
<-- {"ok": true, "value": 32}

--> {"cmd": "write_byte", "addr": 128, "value": 255}
<-- {"ok": true}

--> {"cmd": "flash_write", "addr": 57344, "data_b64": "...", "erase_pages": true}
<-- {"ok": true, "bytes_written": 512}

--> {"cmd": "mass_erase"}
<-- {"ok": true}

--> {"cmd": "read_reg", "reg": "PC"}
<-- {"ok": true, "value": 65534}

--> {"cmd": "write_reg", "reg": "A", "value": 255}
<-- {"ok": true}

--> {"cmd": "read_bkpt"}
<-- {"ok": true, "value": 0}

--> {"cmd": "write_bkpt", "addr": 4096}
<-- {"ok": true}

--> {"cmd": "step"}
<-- {"ok": true}

--> {"cmd": "tagged_go"}
<-- {"ok": true}

--> {"cmd": "write_control", "value": 0}
<-- {"ok": true}

--> {"cmd": "reset_target"}
<-- {"ok": true}

--> {"cmd": "blank_check"}
<-- {"ok": true, "blank": true}
```
Errors: `{"ok": false, "error": "message"}`.

`reg` for `read_reg`/`write_reg` is one of `"A"`, `"CCR"`, `"PC"`, `"HX"`
(H:X), `"SP"`. `read_status` reads the same BDCSCR register `write_control`
writes.

## Status / what's implemented vs. what's a stub

- ✅ Host-side Flask app + web UI — complete, hardware-independent, ready to
  use as soon as the Pico responds to `ping`.
- ✅ Serial protocol + command dispatcher — complete.
- ⚠️ PIO-level SYNC / bit tx / bit rx — implemented per the standard spec
  figures, **needs on-hardware timing validation** (see warning above).
- ⚠️ FLASH program/erase sequence — implemented following the
  program/erase flowchart in the SG8/SG4 datasheet (§4.5.3), but only as
  correct *as the register-level memory-write sequence*; it inherits
  whatever correctness the underlying bit-level SYNC/tx/rx has.

## References
- NXP MC9S08SG8/SG4 datasheet (register map, FLASH programming flowchart):
  https://www.nxp.com/docs/en/data-sheet/MC9S08SG8.pdf
- AN3335 "Introduction to HCS08 Background Debug Mode" (BDC overview, command
  table): https://www.nxp.com/docs/en/application-note/AN3335.pdf
- For the authoritative bit-timing diagrams, consult **HCS08RMv1** (HCS08
  Family Reference Manual Volume 1), chapter on the Background Debug
  Controller (S08BDCV1) — not fully available via public web search at time
  of writing; worth requesting from NXP or finding via your usual embedded
  documentation channels.
# pico-bdm
