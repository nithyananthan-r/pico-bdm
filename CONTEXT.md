# CONTEXT.md — project history & design rationale

This file captures the reasoning behind pico-bdm's design, so it survives
outside any one chat session. If you continue this project in **Claude
Code** (or a fresh chat), point it at this file first for full context.

## Origin

Started from identifying an unknown IC marked "SSG4MTJ" on a board. That
resolved to **S9S08SG8E2MTJ** (Freescale/NXP, HCS08 core, TSSOP-20) — the
newer S9S08 numbering scheme for the MC9S08SG8/SG4 family. Datasheet:
https://www.nxp.com/docs/en/data-sheet/MC9S08SG8.pdf

## Why build a custom programmer at all

The chip is programmed via BDM (Background Debug Mode) over a single-wire
BDC (Background Debug Controller) protocol on the BKGD pin — not JTAG/SWD.
Off-the-shelf options exist (NXP USB Multilink, P&E pods, the open-source
**USBDM** project at https://github.com/podonoghue/usbdm-hcs08), but the
decision was made to build one from scratch as a project in itself.

## Why Raspberry Pi Pico (RP2040) over a PIC18F4550/4455

Both were considered. Decision: **Pico**, because its **PIO** peripheral
runs protocol timing autonomously in hardware (no CPU jitter), whereas a
PIC18 would require hand-cycle-counted assembly with interrupts disabled
during each transaction (the approach the *original* Open Source BDM
project used, on a Freescale MC68HC908JB16 — see
https://manualzz.com/doc/6620978/open-source-bdm-interface-user-manual for
that historical precedent). PIC18F4550/4455 remains a valid fallback path
if ever revisited — same protocol, different implementation shape.

## Architecture

```
PC (browser) <--HTTP--> Flask app <--USB serial (JSON lines)--> Pico <--BKGD/RESET/GND/VDD--> Target MCU
```

- `firmware/` runs on the Pico (MicroPython): PIO programs for the BDC bit
  protocol + a second, independent PIO program for self-capture (see
  below) + a serial command dispatcher (`main.py`).
- `host/` is a Flask app + vanilla-JS web UI (dark, bench-instrument
  themed) that talks to the Pico over USB serial and gives you a browser
  UI: connect, sync, read/write memory, flash a `.s19`, mass erase, and a
  waveform "Scope" panel.

## Protocol implementation status — what's verified vs. not

This is the most important section to re-read before doing further work.

- ✅ **FLASH module register addresses** (FCDIV=0x1820, FSTAT=0x1825,
  FCMD=0x1826, etc.) — confirmed directly against the actual SG8/SG4
  datasheet text.
- ✅ **BDC bit timing** (16 target-clock cycles/bit, 1-cycle-low for '1',
  13-cycle-low for '0', SYNC=128-cycle response) — confirmed against the
  actual HCS08 Family Reference Manual (HCS08RMv1) text during this
  session, not just recalled from general familiarity.
- ⚠️ **BDC command opcode bytes** (`CMD_READ_BYTE = 0xE0` etc., in
  `firmware/bdc.py`) — these are from general familiarity with the
  S08BDCV1/V2 command set (reused across the whole product line's
  datasheets), but were **not** re-verified against a fetched copy of
  HCS08RMv1 §7.3.4 in this session (the fetch of that manual got cut off
  before reaching that chapter). **This is the first thing to check before
  trusting reads/writes past what's already been bench-tested.**

## Self-capture / "Scope" feature — why it exists and how it works

The person building this doesn't have a separate logic analyzer. Rather
than block on that, added a self-diagnostic capture mode:

- A **second PIO state machine**, on the *other* PIO hardware block (so it
  never shares instruction memory or GPIO-drive with the state machine
  actually generating the signal), passively watches BKGD.
- It arms itself and blocks on a PIO `wait` instruction until the line
  **first goes low** — that's the capture trigger. This means Python-level
  call timing doesn't need to be precise; the capture only starts counting
  once the real hardware edge happens.
- Surfaced in the web UI as a "Scope" panel: pick a test (SYNC pulse /
  write bit / read bit), hit Capture, see the actual waveform drawn on a
  canvas.
- **Explicitly flagged limitation** (stated to the user, repeated here):
  this is not a fully independent observer — same Pico drives and
  captures, same clock reference — so it can't catch a bug in the
  underlying PIO clock/counter itself. It *does* reliably catch wrong
  pulse widths, a SYNC with no response, or a read-bit sampled at the
  wrong point, which covers most first-bring-up failures. A second Pico
  running sigrok-pico/PicoLogicAnalyzer, or a cheap CY7C68013A USB logic
  analyzer clone, remain the recommended next step if self-capture results
  are inconclusive.

## Open items / natural next steps

1. Verify the BDC command opcodes against HCS08RMv1 §7.3.4 (see caveat
   above) — highest-priority unresolved item.
2. Wire up real hardware, run Scope captures on SYNC / write-bit / read-bit
   before attempting any FLASH erase/program.
3. If Scope results are inconclusive, move to a second-Pico logic analyzer
   (sigrok-pico / PicoLogicAnalyzer) for a fully independent view.
4. Longer-term, optional: a GDB stub for real step-debugging (noted
   earlier as a bigger undertaking than flashing alone — CodeWarrior won't
   talk to this custom hardware, so any interactive debugging needs its
   own protocol on the host side too).
5. Optional: port the bit-tx/bit-rx routines to PIC18F4550/4455 assembly
   as a second firmware target, if that path is revisited.

## Live testing on real Pico hardware (2026-09-10, no target MCU attached)

The Pico that had been previously wired up was found in a broken state:
enumerated as USB\VID_2E8A&PID_000A, streamed garbled repeating binary on
its "serial" port, and `mpremote` could not enter the raw REPL at all.
Root cause never fully identified -- likely stale/unrelated firmware, not
MicroPython. **Fix: re-flash from scratch via BOOTSEL + the official
MicroPython UF2** (v1.29.0 at the time), which produced a clean device at
`VID_2E8A:PID_0005` (the standard MicroPython Pico ID) that behaved
correctly. If the Pico ever seems to be sending nonsense over serial
again, re-flashing MicroPython from BOOTSEL mode is the fastest fix --
don't assume the wiring/opcodes are at fault first.

With firmware copied over and a target-free bring-up (nothing wired to
BKGD/RESET), tested via the raw serial JSON protocol AND through the full
Flask host API:

- ✅ `ping` -- works.
- ✅ `sync` -- correctly fails with a clean "no response from target"
  error (expected, nothing's wired up).
- 🐛 **Found and fixed**: `read_byte`/`write_byte` (and anything else
  calling `_write_bit`/`_read_bit`) before a successful `sync()` crashed
  with a raw `TypeError: can't convert NoneType to int` instead of a clean
  error. Cause: `Bdc._tx_sm()` had the correct `sync()` guard
  (`self.sm_freq is None`) but was dead code -- `_write_bit`/`_read_bit`
  built their own state machine directly without ever calling it. Fixed by
  moving the guard directly into `_write_bit`/`_read_bit` and removing the
  unused `_tx_sm()`/`self._sm`. Verified fixed on hardware (clean
  `BdcError` now returned instead of a crash).
- 🐛 **Found, NOT fixed -- needs real hardware debugging**: the **Scope /
  `capture` command hangs indefinitely** (tested with a 10s timeout, zero
  response) when driving `capture_sync` with nothing wired to BKGD. Doesn't
  just time out slowly -- the whole serial command loop goes silent and
  the Pico needs a physical/`mpremote reset` to recover, which points at a
  genuine hardware-level stall, not a slow-but-bounded software loop
  (traced through the code: every individual wait/timeout in
  `bdc.py`/`bdc_pio.py` involved *should* be bounded to low tens of ms, so
  something outside that accounting is actually blocking).
  Suspected cause: `_capture()` instantiates the passive capture state
  machine (`make_capture_state_machine`, PIO block 1) and then, inside
  `drive_fn()`, a **second, independent** state machine
  (`make_state_machine(bdc_sync, ...)`, PIO block 0) bound to the *same*
  physical GPIO -- an RP2040 GPIO function-select / PIO resource-sharing
  conflict between two state machines on different PIO blocks claiming the
  same pin is the leading hypothesis, but this needs an actual logic
  analyzer or `pdb`-level tracing on hardware to confirm; it was not fixed
  blind. **Until root-caused, don't rely on the Scope/capture panel during
  real bring-up** -- ironically the self-diagnostic tool is the thing
  that's currently broken, not the SYNC/read/write path it was meant to
  validate.
- Flask host app (`/api/connect`, `/api/status`, `/api/sync`,
  `/api/read_byte`, `/api/disconnect`) verified working end-to-end against
  the real Pico over its actual COM port, not just via `app.test_client()`.

## UI/feature expansion (2026-09-10, later session)

Audited which BDC/MCU capabilities the firmware had opcodes defined for
but never actually wired up anywhere (`firmware/bdc.py`'s opcode table had
`CMD_READ_A/CCR/PC/HX/SP`, `CMD_WRITE_*`, `CMD_READ_BKPT`/`CMD_WRITE_BKPT`,
`CMD_TRACE1`, `CMD_TAGGO`, `CMD_WRITE_CONTROL`, and `FCMD_BLANK_CHECK` all
defined as constants with zero callers). Wired all of them through the
full stack (`Bdc` methods → `main.py` dispatch → `bdm_client.py` →
Flask routes → UI):

- **CPU register read/write** (A, CCR, PC, H:X, SP) — new "Registers &
  breakpoint" panel (under "Show advanced").
- **Hardware breakpoint** (read/write BKPT register) + **Step** (TRACE1,
  single-instruction step) + **Tagged GO** (TAGGO, resume until the
  breakpoint address is hit).
- **BDCSCR read/write** (`write_control` — clear sticky WS/WSF/DVF flags,
  change CLKSW) in the same panel.
- **Blank check** (`FCMD_BLANK_CHECK`) — Flash panel, asks the FLASH
  module itself whether the array is erased instead of reading back every
  byte in Python.
- **Reset target** (plain RESET pulse, does NOT force BDM — target just
  reboots and runs its own code) vs. the existing `hw_reset_to_bdm`
  (forces active background mode). Distinct **Go** button added too
  (previously `/api/go` only existed internally for "run after program").

All of the above reuses the SAME opcode table `bdc.py`'s top-of-file
disclaimer already flags as unverified against HCS08RMv1 §7.3.4 — wiring
them up doesn't make their correctness any more or less certain than
before, it just means more of the *already-disclaimed* opcodes are now
reachable from the UI. Verified end-to-end against real hardware (Pico
connected, no target MCU wired up): every new command reaches the Pico
and returns a clean `BdcError` (e.g. "call sync() before any tx/rx
operation") rather than crashing or hanging — confirms the wiring is
correct, NOT that the opcode values themselves are correct against real
silicon.

UI also reorganized around actual workflow (Connect → Flash on top row;
Memory/Scope/Registers demoted behind a "Show advanced" toggle; a
page-wide guide modal; a 5-theme switcher with matching scrollbars). The
default compact view still fits one screen with no scrolling; the
expanded "advanced" view is intentionally allowed to scroll the whole
page rather than hiding content inside tiny per-module scroll boxes.

## Everything already tested (host side, hardware-independent)

- Flask app boots and all routes respond correctly (`/`, `/api/ports`,
  `/api/status`, `/api/capture` error path, etc.) — exercised via
  `app.test_client()`.
- `srec.py`'s S-record parser + contiguous-chunk merger verified against a
  hand-built sample.
- All Python files pass `py_compile`; `app.js` passes `node --check`.
