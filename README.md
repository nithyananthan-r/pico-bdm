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

> ### ⚠️ Current bring-up status (2026-09-12): the test rig has a POWER fault
>
> The one target wired up so far has never executed a single BDC command.
> As of the second live session on 2026-09-12 the reason is believed to be
> **hardware, not this code**: a two-pin capture showed BKGD and RESET
> moving as one identical waveform for ~700 µs at a time on nets that are
> not shorted, which happens when the VDD they are both pulled up to is
> collapsing and recovering. The ~8 µs pulse `sync()` measures and reports
> as a 16.3-16.5 MHz "target BDC clock" is one of those brownout dips, so
> **that number is meaningless and must not be fed to `flash_init_clock`**,
> and every byte the read path has ever returned is an artifact of the same
> pulse. Check VDD/VSS/RESET wiring with a meter before debugging anything
> else — see CONTEXT.md, Findings 13-21, for the measurements and the exact
> bench checklist.

Recommended first bring-up steps:
1. Wire everything up per the diagram below — then **measure VDD to VSS on
   the chip's own legs before anything else.** The single costliest mistake
   in this project's history was a month of protocol debugging against a
   target that was not properly powered.
2. **No separate logic analyzer? The built-in Scope panel** (see
   "Self-capture / Scope" below) uses a second, passive PIO state machine
   on the Pico itself to record the actual waveform on BKGD around each
   test operation, so you can sanity-check timing without any extra
   hardware. It is enabled and confirmed working on real hardware (the old
   hang was root-caused and fixed on 2026-09-11). For anything ambiguous,
   go straight to the **two-pin** capture (`{"cmd":"capture2"}`) — watching
   BKGD alone is what made this project misread a power fault as a
   protocol fault for so long.
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

The **Scope** panel in the web UI runs one of a few canned test operations
(SYNC, a transmitted byte, a read phase, or an arbitrary command) while a
*second* PIO state machine passively records the BKGD line and streams the
waveform back for display.

How it avoids needing precise timing coordination from Python: the capture
program arms itself, then executes a PIO `wait` instruction that blocks
until the line **first goes low** — that's the actual trigger. So it
doesn't matter that MicroPython's own call overhead is imprecise; the
capture only starts counting once the real edge happens, at full PIO clock
precision.

**Pick your time scale.** Pass `window_us` to ask for a capture spanning
roughly that much real time; the sample rate is derived from it and the
sample count. This matters more than it sounds: the same signal looks like
completely different things at 500 ns, 1.5 µs and 15 µs per sample, and
every real finding in this project so far came from sweeping that. Without
it the capture is pinned to bit-level zoom and can only ever see a few µs.

```
{"cmd":"capture","test":"sync"}                                  600 µs by default
{"cmd":"capture","test":"command","opcode":224,"addr":65534,
 "window_us":40,"sample_count":128}                              a whole READ_BYTE
```

**Real-time caveat:** the PIO RX FIFO is 4 words = 128 samples deep and the
draining loop is plain Python. Past the first 128 samples the state machine
stalls on its autopush, so later samples are contiguous with each other but
not with the earlier ones — unless the sample period is slow enough
(≳2 µs) for Python to keep up. Trust the first 128 samples of a fast
capture.

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

See [hardware/pico_mcu_wiring_schematic.pdf](hardware/pico_mcu_wiring_schematic.pdf)
for a full schematic (Pico pinout, MCU pinout, R1 pull-up, C1 decoupling
cap, and all the notes below in diagram form).

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
├── firmware/                  <- runs ON THE PICO (MicroPython)
│   ├── main.py                <- serial command dispatcher, auto-runs on boot
│   ├── bdc_pio.py              <- PIO programs: sync, write-bit, read-bit,
│   │                              passive capture (+ the PIO block
│   │                              allocation table — read it before adding
│   │                              or resizing a program)
│   └── bdc.py                  <- higher-level BDC driver + flash routines
├── host/                      <- runs on your PC
│   ├── requirements.txt
│   ├── app.py                  <- Flask app / REST API
│   ├── bdm_client.py            <- pyserial client talking to the Pico
│   ├── srec.py                  <- S-record (.s19) parser + chunk merger
│   ├── templates/index.html
│   └── static/app.js, style.css
├── target-firmware/           <- code that runs on the TARGET MCU, built
│   └── blinky/                   with CodeWarrior and flashed by pico-bdm
│       ├── main.c                (a minimal PTA0 blink bring-up test)
│       └── README.md
└── hardware/                  <- wiring schematic (PDF)
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

Diagnostics (added 2026-09-12; these are what found the fault recorded in
CONTEXT.md Findings 13-21, so reach for them before adding more protocol
code):

```
--> {"cmd": "raw_xfer", "tx": [228], "nbits": 8}
<-- {"ok": true, "value": 0, "tx_us": 240, "gap_us": 16, "rx_us": 78}
        send an arbitrary byte sequence, then clock in nbits, and report
        how long each phase really took. gap_us is the turnaround between
        the last command bit and the first read bit.

--> {"cmd": "probe_pin", "pin": 14, "ms": 30}
<-- {"ok": true, "low_pct": 16.5, "transitions": 408, "samples": 1231}
        is the target doing anything? works on any GPIO.

--> {"cmd": "drive_pin", "pin": 15, "level": 0}      level: 0 / 1 / null
        hold a line low, drive it high, or release it — and LEAVE it there,
        so you can hold one line and measure the other.

--> {"cmd": "rise_time", "pin": 14, "hold_ms": 10, "trials": 5}
<-- {"ok": true, "rise_us": [1536, 1549, 1571, 1550, 1548]}
        capacitance probe: drive low, release to Hi-Z with no internal
        pull, time the external pull-up. A plain pull-up node recovers in
        ~1 us; milliseconds means a big capacitor is on that net.

--> {"cmd": "set_bit_clock", "hz": 4190000}
        force the bit rate without SYNC, so the bit rate can be swept
        independently of SYNC's (in this project, wrong) assumption.

--> {"cmd": "capture", "test": "xfer", "tx": [228], "nbits": 8,
     "window_us": 64}
        capture a WHOLE transaction — command bytes and read phase.

--> {"cmd": "capture2", "tx": [228], "nbits": 8, "window_us": 200}
--> {"cmd": "capture2", "drive": "sync", "window_us": 1000}
<-- {"ok": true, "bkgd_b64": "...", "reset_b64": "...",
     "sample_period_ns": 781, "n_samples": 256}
        TWO-PIN capture: BKGD and RESET sampled together. This is the one
        that cracked the case — see CONTEXT.md Finding 18.
        Extras: "trigger": "reset" | "bkgd", and "drive": "xfer" | "sync" |
        "none" | "reset" | "bdm" | "areset_sync".
        DEPTH LIMIT: only the first 64 samples are contiguous (16 samples
        per word into a 4-word FIFO). Ask for more and the tail is an
        artifact that looks perfectly repeatable. Single-pin "capture"
        packs 32/word, so its limit is 128.
```

Phase-locked diagnostics (added 2026-09-13). A target with blank FLASH
free-runs in an illegal-opcode reset loop — 40.0 us on the MC9S08SG8 here,
~8.6 us of it in reset — so a stimulus fired at a random moment mostly
lands in the wrong place. These drive the reset themselves and fire at a
controlled delay after releasing it, which makes the target's cycle
phase-locked to the host. **This is what turned SYNC from a ~20% coin flip
into a reliable measurement; see CONTEXT.md Findings 29-31.**

```
--> {"cmd": "sync_locked"}
<-- {"ok": true, "target_freq_hz": 16842106.0, "pulse_us": 7.6,
     "best_delay_us": 68, "hits": 23, "attempts": 102}
        THE RELIABLE SYNC. Scans the reset-loop phase and takes the
        LONGEST response pulse seen — truncation can only shorten a
        response, so the longest one is the true 128-cycle answer.
        Plain {"cmd":"sync"} fires at an arbitrary phase and, when it does
        get an answer, usually gets a truncated one — which reports the
        frequency too HIGH (freq = 128 / width). Prefer this.
        Do NOT call it on a target you have halted in active BDM: it
        asserts RESET.

--> {"cmd": "after_reset_sync", "delay_us": 64, "host_low_us": 20}
<-- {"ok": true, "raw": 124943}
        one phase-locked SYNC. `raw` is bdc_sync's result word.

--> {"cmd": "after_reset_xfer", "delay_us": 64, "tx": [228, 255],
     "nbits": 0, "sample_count": 128, "window_us": 17.1}
<-- {"ok": true, "samples_b64": "...", "sample_period_ns": 140,
     "value": null, "spin_us": 44, "gap_us": 0}
        one phase-locked transaction, captured. With nbits=0 and a 0xFF
        filler byte appended to tx, the read phase costs ZERO turnaround:
        bdc_tx_byte sending 0xFF emits exactly the BDC read-bit marker
        shape, so command and read phase come out of one FIFO load.
        Decode the answer from the capture. With nbits>0 it uses the real
        rx state machine instead, at the cost of a ~10 us gap.

--> {"cmd": "scan_wave", "words": [...], "n_cycles": 144, "d_from": 0,
     "d_to": 44, "d_step": 1, "trials": 4, "sample_count": 128,
     "window_us": 10.0, "skip_us": 7.5}
<-- {"ok": true, "max_low_samples": [...], "best_delay_us": 9,
     "best_low_us": 0.31, "best_at_us": 8.1, "sample_period_ns": 78,
     "skip_sample": 97, "tx_us": 8.55}
        Same phase-locked scan and longest-low-after-skip_us detector as
        scan_xfer, but the stimulus is an ARBITRARY cycle-by-cycle
        waveform instead of bdc_tx_byte's hard-coded 3/13/16 bit shape.
        `words` is up to 8 32-bit words of pindirs bits, MSB first, ONE
        BIT PER TARGET BDC CYCLE: 1 = drive BKGD low, 0 = release to the
        pull-up. 8 words = 256 contiguous BDC cycles = two 16-cycle bytes.
        This is what makes the RATIO between the bit period and the '1' /
        '0' low widths sweepable — a bit-clock sweep scales all three
        together and can never change it. Padding the stream with leading
        0 bits also shifts the burst by 59 ns a time, giving sub-
        microsecond phase resolution that sleep_us() cannot.
        NOTE: no speed-up pulse (a 1-instruction PIO program has no spare
        side-set), so the pull-up RC merges back-to-back '0' bits at
        low0 >= 12. Keep low0 <= 11 here; use bdc_tx_byte for the exact
        3/13/16 shape.

--> {"cmd": "capture_wave", "words": [...], "n_cycles": 128,
     "sample_count": 128, "window_us": 8.0}
<-- {"ok": true, "samples_b64": "...", "sample_period_ns": 62,
     "n_samples": 128}
        one-shot capture of a scan_wave stimulus, no reset priming. Use it
        to check a host-built waveform before trusting a scan built on it.
```

Errors: `{"ok": false, "error": "message"}`.

`reg` for `read_reg`/`write_reg` is one of `"A"`, `"CCR"`, `"PC"`, `"HX"`
(H:X), `"SP"`. `read_status` reads the same BDCSCR register `write_control`
writes.

## Status / what's implemented vs. what's a stub

- ✅ Host-side Flask app + web UI — complete, hardware-independent, ready to
  use as soon as the Pico responds to `ping`.
- ✅ Serial protocol + command dispatcher — complete.
- ✅ BDC command opcode values — verified against HCS08RMv1 Table 7-1.
  (Values only. The 2026-09-11 review pass found several real logic bugs
  in code that was already using correctly-valued opcodes, so this is not
  a statement about the protocol logic.)
- ⚠️ PIO-level SYNC / bit tx / bit rx — bit timings match HCS08RMv1, and
  the SYNC measurement was rewritten on 2026-09-11 to fix an inverted
  counter, a 5-bit counter with nowhere near enough range, an
  indistinguishable timeout-vs-overflow result, and a missing wait-for-high
  before edge detection. **Still needs on-hardware validation** — no target
  has ever successfully synced.
- ⚠️ FLASH program/erase sequence — implemented following the
  program/erase flowchart in the SG8/SG4 datasheet (§4.5.3), with the
  2026-09-11 fixes to FBLANK's bit position, FACCERR clearing, FPROT
  unprotect, the FCDIV divisor rounding, and the multi-region page-erase
  clobber. Correct *as a register-level sequence* against the datasheet;
  it still inherits whatever correctness the underlying bit-level
  SYNC/tx/rx has, and has never been run on a real part.

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
