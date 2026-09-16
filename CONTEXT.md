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
- ✅ **BDC command opcode bytes** (`CMD_READ_BYTE = 0xE0` etc., in
  `firmware/bdc.py`) — resolved 2026-09-11. Verified against HCS08RMv1
  Table 7-1 by a review with primary-source access. The old "unverified"
  disclaimer at the top of `bdc.py` has been replaced accordingly.
  **Keep the distinction honest:** this confirms the opcode *values*. It
  does not say the protocol logic using them was right — the very same
  review found four real bugs (inverted SYNC math, wrong FSTAT FBLANK
  mask, FACCERR never cleared, page erase clobbering an already-programmed
  chunk) in code that was already using correctly-valued opcodes.
- ✅ **SYNC now works against a real target, on demand** — 2026-09-13,
  see "THIRD live session" near the end of this file. `sync_locked()`
  returns 16.84 MHz repeatably (12/12 calls), matching the MC9S08SG8's
  FEI-default DCO. The bit waveform is verified correct to the nanosecond
  at 27 ns/sample.
- ❌ **No BDC *command* has ever been executed by a target.** SYNC works;
  nothing else does. The host side is exhausted as an explanation (all 256
  opcodes, 14 bit clocks, every reset-loop phase at 1 us resolution — see
  Findings 34, 39, 43, 46, 49, 50). Everything above the bit layer (memory
  access, FLASH, breakpoints) is therefore still fixed *against the
  datasheet and the reference manual*, by reading, not by measurement.
- ⚠️ **FLASH security is NO LONGER the leading candidate — it is REFUTED
  as an explanation** (2026-09-14, Finding 44, verified against HCS08RMv1
  §4.6/§4.7.2). The part almost certainly *is* secured (blank NVOPT =
  SEC01:SEC00 = 1:1 = secure, Table 4-8), but the manual states plainly
  that a secured part still accepts non-intrusive BDC commands, BDCSCR
  access, high-page register access, blank check and mass erase. It cannot
  explain a target that ignores `ACK_ENABLE` and `READ_STATUS`. **Read the
  "FIFTH session" section at the end of this file before forming any new
  hypothesis.**

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

1. ~~Verify the BDC command opcodes against HCS08RMv1 §7.3.4~~ — done
   2026-09-11, see above.
2. ~~Wire up real hardware, run Scope captures on SYNC / write-bit /
   read-bit before attempting any FLASH erase/program~~ — done 2026-09-12.
   The Scope works, has run dozens of captures against a real target with
   no hang, and is now **enabled in the UI**. See the 2026-09-12 section.
3. If Scope results are inconclusive, move to a second-Pico logic analyzer
   (sigrok-pico / PicoLogicAnalyzer) for a fully independent view. **This
   is now genuinely worth doing**: the self-capture has taken the
   investigation as far as it can: it has proved the *host* side is
   correct, but it cannot tell you what the target's silicon is refusing
   and why.
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
- 🐛 **Found, NOT fixed at the time -- since ROOT-CAUSED AND FIXED on
  2026-09-11; the hypothesis recorded below turned out to be WRONG, see
  the validation-review section further down**: the **Scope /
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
  single-instruction step) + **Tagged GO** (TAGGO). ⚠️ **Corrected
  2026-09-11:** the description of Tagged GO here was wrong. HCS08 has no
  external tag-input pin, so TAGGO is identical to plain GO; and the
  breakpoint did nothing at all because BDCSCR.BKPTEN was never armed. Both
  fixed — see the validation-review section below.
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
disclaimer flagged as unverified against HCS08RMv1 §7.3.4 at the time —
wiring them up didn't make their correctness any more or less certain,
it just meant more of the *already-disclaimed* opcodes were reachable
from the UI. (Those opcode values have since been verified; see
2026-09-11 below.) Verified end-to-end against real hardware (Pico
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

## Full validation review + fix pass (2026-09-11, no hardware touched)

An independent review with primary-source access to HCS08RMv1 and the
MC9S08SG8 datasheet went through the whole stack. This section records
what it found and what was then changed. **Read the "what is still
unverified" list at the end before trusting any of it.**

### The Scope/capture hang — root-caused and fixed

The leading hypothesis in the section above (an RP2040 GPIO
function-select / PIO resource conflict between two state machines on the
same pin) was **wrong**. RP2040 PIO *inputs* aren't gated by GPIO FUNCSEL
at all, so two SMs reading one pin was never a problem.

The actual cause: `bdc_sample` was declared `fifo_join=PIO.JOIN_RX`, which
surrenders the TX FIFO to double the RX FIFO. But `_capture()` loads the
sample count into that SM with `cap_sm.put(...)` — with no TX FIFO, that
call spins forever inside MicroPython's blocking C code, before
`drive_fn()` is ever reached. That's the hang: not a hardware stall, a
blocking write to a FIFO that had been configured out of existence. The
join is gone (there's a comment in `bdc_pio.py` saying not to re-add it),
and the unused `set_init` on that program went with it.

### PIO instruction memory was over-committed

All three driving programs loaded onto PIO block 0: `bdc_sync` (26) +
`bdc_read_bit` (14) + `bdc_write_bit` (8) = 48 instructions in a block
with **32 slots**. This was invisible during target-free testing purely
because `sync()` always failed first, so the guard in `_write_bit` stopped
the second program ever loading — the moment a real target synced, the
next read/write would have failed to load its program.

`bdc_pio.py` now has an explicit, commented block-allocation table:
block 0 holds `bdc_sync` alone (27/32); block 1 holds `bdc_sample` +
`bdc_write_bit` + `bdc_read_bit` (27/32). `make_state_machine()` now
*requires* an `sm_id` — there are named `SM_*` constants — so a new call
site can't silently inherit block 0. Also corrected a wrong comment there
that claimed concurrent programs need separate *blocks*; they need
separate *state machines*, and instruction memory just has to fit.

### SYNC measurement was wrong three different ways, and is rewritten

1. The PIO program counted DOWN and pushed the *remaining* value; `sync()`
   used that value as if it were the elapsed count. The relationship was
   inverted.
2. The counter was `set(x, 31)` — a 5-bit immediate — with a 3-cycle loop,
   so the maximum measurable pulse was ~93 PIO cycles. An 8 MHz target's
   128-cycle response needs far more than that; the quantisation error
   alone made the result meaningless.
3. Counter wrap and a genuine no-response timeout pushed values the host
   couldn't tell apart, so a working-but-slow target was reported as a
   wiring fault, and `sync(retries=4)` just repeated the identical attempt.

The rewritten `bdc_sync` loads a full 32-bit counter from the FIFO, and
pushes one result word with four distinguishable outcomes: `0xFFFFFFFF`
(no falling edge — target silent/unpowered/unwired), `0xFFFFFFFE` (BKGD
never came back high — shorted, or no pull-up), `0` (overflow: the pulse
is longer than this rung can measure), or the leftover count. `sync()`
does the subtraction properly (`elapsed = 2 * (start - remaining)`, two
PIO cycles per decrement) and walks a deliberate ladder of
(probe clock, measuring window) pairs — 25 MHz/10 ms, 5 MHz/100 ms,
1 MHz/1 s — trading resolution for range, so an overflow retries somewhere
that can actually succeed. Both the new firmware and the host report a
specific reason on failure instead of one generic "check wiring".

Two related things were also corrected while in there:

- **Race at the start of SYNC (HCS08RMv1 §7.3.4.1).** The program went
  straight from releasing BKGD into hunting for a falling edge. Between
  the RP2040's input synchroniser delay and the pull-up's RC rise time,
  the first pin test could still see the tail of *our own* drive-low and
  "detect" a false edge. There is now a bounded wait-for-high first
  (bounded, not a bare `wait` instruction — an unbounded wait on a shorted
  line would be a new way to hang the Pico).
- **Speedup pulse added.** §7.3.4.1 wants the host to briefly drive BKGD
  high before releasing to Hi-Z. It now does (two PIO cycles, so the
  duration scales sensibly with the probe clock), instead of relying
  entirely on the pull-up.

### FLASH safety

- `FSTAT_FBLANK` was `0x02`. Per MC9S08SG8 Rev.8 Figure 4-9 / Table 4-12
  the layout is `FCBEF[7] FCCF[6] FPVIOL[5] FACCERR[4] 0[3] FBLANK[2]
  0[1] 0[0]` — FBLANK is bit 2. Now `0x04`. Blank check was reading the
  wrong bit.
- **FACCERR was detected but never cleared.** Per §4.5.5 it must be
  cleared by writing 1 to it before any command can be processed, and
  FCDIV can't even be rewritten while it's set — so one access error
  wedged the FLASH interface permanently with no recovery. It's now
  cleared at the start of every `_flash_command`, before the `FCDIV`
  write in `flash_init_clock`, and on the way out of the error paths in
  `_flash_wait_command`.
- **Multi-chunk page clobber.** Chunks were erased and programmed one at a
  time. Two S-record runs sharing a 512-byte page (routine in real
  toolchain output) meant programming chunk A then erasing for chunk B
  silently destroyed A — and per-chunk verification never noticed, because
  it only re-read the chunk just written. `/api/flash_srec` now computes
  the union of pages across all chunks, erases them once up front, and
  writes every chunk with `erase_pages=False`.
- `flash_init_clock` targeted exactly 200 kHz — the legal maximum, zero
  margin — using `round()`, which could round the divisor down and push
  fFCLK *over* the limit. It now uses ceiling division (so the divisor can
  only make fFCLK lower, never higher), aims at 175 kHz, and **raises**
  instead of silently clamping DIV when no legal setting exists for the
  given bus clock.
- `REG_FPROT` was defined and never used. There is now a
  `flash_unprotect()` called from `flash_write_region`, `flash_erase_page`
  and `flash_mass_erase`. ⚠️ CONFIDENCE: MEDIUM — the value written
  (`0xFF`, FPDIS=1) is the conventional S08 "unprotect everything" value,
  but the exact FPROT bit names for *this* part were not re-read from the
  datasheet. Flagged in the source too.
- **Breakpoints were a complete no-op.** `write_bkpt()` wrote the address
  but never set BDCSCR.BKPTEN — and BDCSCR resets with BKPTEN=0, so the
  breakpoint did nothing at all on real silicon. It now arms BKPTEN via a
  read-modify-write that preserves ENBDM and CLKSW (a bare overwrite would
  drop out of BDM and/or break the calibrated bit timing), with FTS
  cleared for force mode by default.
- **"Tagged GO" was documented wrong** in `bdc.py`, the UI guide and the
  button tooltip. Per §7.3.4.16, HCS08 has no external tag-input pin, so
  TAGGO is functionally identical to plain GO — it does *not* "resume
  until the breakpoint address is hit". All three now say so. The real
  "run to an address" flow is Set BKPT (which arms it) then Go.

### Host app

- `bdm_client._call` never flushed the serial input buffer on timeout, so
  a merely-late Pico response desynchronised every subsequent call (each
  one reading the previous command's answer, forever). Now flushed on both
  timeout and unparseable-response paths. `/api/read_block` also has a
  server-side length cap, so a huge read can't outrun the client timeout.
- The serial lock was held for one JSON round-trip, not for a whole
  logical operation — a second browser tab could interleave a read into
  the middle of a FLASH command sequence, which is exactly how you provoke
  a FACCERR. There's now a `client.transaction()` context manager, used by
  `/api/flash_srec` and `/api/sync`.
- `app.run(debug=True)` restarted the process (and dropped the live serial
  connection) on any `.py` edit — the most likely reason the Flask server
  kept not surviving between sessions. Now `use_reloader=False`, with the
  tradeoff written down in a comment; the interactive traceback page is
  kept.
- **CSRF**: the mutating routes had no protection, and a plain POST or a
  multipart POST are both CORS-"simple" requests a malicious tab could
  forge — i.e. a drive-by mass erase. All state-changing `/api/` routes now
  require `X-Requested-With: pico-bdm`, which a cross-origin page cannot
  set without a preflight this server won't answer. `app.js` sends it,
  including on the raw `fetch()` used for the file upload.
- `MAX_CONTENT_LENGTH` set (512 KB) so an arbitrarily large upload isn't
  read into memory.
- The S-record parser had no checksum validation at all, no length
  validation, rejected lowercase `s`, and threw a bare `IndexError` on a
  truncated line — which wasn't in the route's caught exception tuple, so
  it escaped as a Flask 500 HTML page and then broke the browser's
  `res.json()` with "Unexpected token '<'". It now validates both, accepts
  lowercase, and raises only `ValueError`. A generic JSON error handler on
  `HTTPException` also catches everything Flask raises before our handlers
  run (415 on bad Content-Type, 413 on oversized upload, 404), so no `/api/`
  route can ever return HTML.
- Smaller ones: `request.json` moved inside the try in `api_connect`;
  `request.get_json(silent=True)` instead of a `request.json or {}` that
  could never reach its `or`; `connect()` no longer leaves stale state (and
  `api_connect` no longer closes a still-good previous connection on
  failure); `disconnect()` is lock-guarded; `KeyError` added to
  `api_sync`'s catch; addresses/values/lengths are range-validated;
  `main.py`'s `print(json.dumps(resp))` moved *inside* the try (a
  serialisation failure used to kill the whole command loop, so the Pico
  just appeared to stop answering); `sample_count` validated as a positive
  multiple of 32.
- The redundant host-side verify after `flash_srec` was **removed**.
  `flash_write_region` already reads back and compares every byte on the
  Pico, and a BDC read is one byte per round-trip — re-reading from the
  host exactly doubled the slowest operation in the stack for zero extra
  coverage. The UI's "Verify after write" checkbox is gone; verification
  is now unconditional and done in firmware. (Documented here because it's
  a deliberate reduction in belt-and-braces, not an oversight.)

### Web UI

- The Memory panel parsed numbers as `parseInt(x,16) || parseInt(x,0)`
  while every other panel used `parseNum` (decimal unless `0x`-prefixed),
  so "10" meant 0x10 in one panel and decimal 10 in another — a real
  hazard on a flash programmer. One `parseNum` is now used everywhere, and
  it throws on non-numbers instead of silently producing `NaN`.
- The page never called `/api/status` on load, so after a refresh the UI
  assumed disconnected even when the server still held the port — the
  "first click fails, second works" experience. It now asks. (Sync state
  isn't recoverable this way; the server doesn't track it. A sync is still
  required after a reload, as before.)
- The Disconnect handler was the only one with no try/catch.
- The Scope waveform's colours were hardcoded Amber Bench hex values, so
  it rendered wrong under every other theme (badly under Paper). It now
  reads `--panel-border` / `--accent` / `--text-dim` from the live theme.
- `.bench { height: calc(100vh - 53px) }` hardcoded the header's rendered
  height — which depends on the Google-hosted Inter/JetBrains Mono
  webfonts, i.e. it is wrong on exactly the offline bench setup this tool
  is for. `body` is now a flex column and `.bench` takes `flex: 1;
  min-height: 0`, with `flex: 1 0 auto` in advanced mode. The documented
  behaviour is preserved: compact view fits one screen with no scrolling,
  advanced view scrolls the whole page.
- The BDCSCR Write button is now disabled until a Read has populated the
  field. Writing the placeholder `0x00` cleared CLKSW and silently broke
  the bit timing sync had just calibrated.

### Target C code

- `SOPT1 = 0x00` in `target-firmware/blinky/main.c` is **correct** — but
  the comment justifying it was wrong (it claimed "SOPT1 default has COPE
  set on this family"; there is no COPE bit on this family). Per §5.7.3
  the COP is controlled by a two-bit `COPT` field at SOPT1[7:6] that
  resets to 1:1, hence SOPT1's 0xC0 reset value, and there is no COPE,
  BKGDPE or RSTPE bit to clear by accident — so writing 0x00 disables the
  watchdog with no risk to BDM access. Comment and the blinky README both
  corrected, caveat removed.
- `delay()`'s loop variable is now `volatile`; without it an optimizing
  build is free to delete the loop entirely and toggle the pin at full bus
  speed, which on first bring-up looks exactly like "the chip never ran my
  code".

### Hardware nicety

`Bdc.__init__` no longer constructs RESET as `Pin(..., Pin.OUT, value=1)`
before immediately releasing it to input. RESET is open-drain with an
internal pull-up and the target can legitimately be driving it low during
its own reset sequence, so that was a momentary drive contention if the
Pico happened to boot mid-target-reset. The code only ever needs to drive
RESET *low*, so it's constructed as `Pin.IN` directly.

### Found during this pass and fixed, but NOT on the review's list

`_write_bit()` / `_read_bit()` called `sm.active(0)` immediately after
`sm.put()`. `put()` only queues counts — the state machine runs
asynchronously — so deactivating right away could cut the bit off
mid-pulse. Both now sleep out the 16-target-cycle bit window first. Also,
`sync()` used to cap the SM clock at 120 MHz while leaving
`PIO_CYCLES_PER_TARGET_CYCLE` alone, which for a fast target would
silently stretch every bit time by the capping factor; it now reduces the
cycles-per-target-cycle ratio instead and stores what it actually used in
`self.k`. Both are flagged here because they are exactly as unverified as
everything else.

### What is still unverified — i.e. almost all of it

Everything above was fixed by reading the reference manual and the
datasheet. **None of it has been run against a real, synced target**, for
the simple reason that no target has ever synced. Specifically, still
needing real-hardware verification before anyone should trust it:

- That SYNC now gets a response at all, that the measured pulse produces a
  sane target frequency, and that the probe ladder picks the right rung.
- That the PIO block allocation actually loads (the instruction counts are
  hand-counted from source, not read back from the hardware).
- That the speedup pulse and the wait-for-high don't upset a real target.
- Every bit-level read/write: the 1/13/16-cycle shapes, the cycle-10
  sample point, and the newly added post-put settle delays.
- Every FLASH operation — erase, program, blank check, FCDIV setup,
  FPROT unprotect (the FPROT value in particular, see its confidence note).
- The breakpoint: that arming BKPTEN makes it fire, and that force mode
  behaves as expected.
- ~~That Scope capture no longer hangs.~~ CONFIRMED 2026-09-12 on real
  hardware over many dozens of captures; the UI button is now enabled.

Host-side things (S-record parsing, CSRF, error handling, the page-union
erase computation, the CSS) *were* exercised locally via
`app.test_client()` and direct calls. Those are genuinely tested. The
firmware is not.

## Real hardware retest after the fix pass (2026-09-11, still no target MCU attached)

Deployed the fixed `firmware/bdc.py`/`bdc_pio.py`/`main.py` to the Pico
and re-ran the tests that are actually possible without a target wired
up. Two of the fix pass's biggest unknowns are now genuinely confirmed on
real silicon, not just "correct by manual analysis":

- ✅ **Firmware boots clean.** The rewritten PIO assembly (new `bdc_sync`,
  the `SM_SYNC`/`SM_WRITE_BIT`/`SM_READ_BIT` block allocation) actually
  assembles and loads on the RP2040 — `py_compile` can't check this on a
  desktop, so this was a real unknown until deployed.
- ✅ **B1 (PIO instruction memory overflow) is fixed, confirmed on
  hardware.** After a `sync()` call (even a bogus one, see below),
  `read_byte`/`write_byte` successfully loaded `bdc_write_bit` and
  `bdc_read_bit` onto PIO block 1 alongside `bdc_sample` with no "PIO
  instruction space not available" error. This exact failure mode was
  *impossible* to hit in every prior test session because `sync()` always
  failed first (no target), so the guard in `_write_bit`/`_read_bit`
  never let the second program's load be attempted. This is the first
  time that code path has ever executed.
- ✅ **A2 (Scope/capture hang) is fixed, confirmed on hardware.** A
  `capture` command that used to hang the Pico indefinitely (requiring a
  physical/`mpremote reset` to recover — this happened repeatedly earlier
  in this project) now returns cleanly in **0.03 seconds**. This was the
  single most disruptive bug found this session; it's the first thing
  future work should feel comfortable trusting.
- ⚠️ **New finding: SYNC can false-positive on a floating BKGD pin.**
  With literally nothing wired to BKGD (bare Pico, no target, no R1
  pull-up), `sync()` still returned `{"ok": true, "target_freq_hz":
  ~11000}` in ~0.15s, repeatably, across 6 runs, all clustered within
  ~1% of each other (11000–11100 Hz). That tight clustering is the tell:
  genuine environmental/RF noise on a floating pin would scatter far more
  randomly run-to-run. The likely explanation is that the host's own
  SYNC drive sequence (release-to-input, wait-for-high, watch-for-fall)
  is measuring the pin's own parasitic self-capacitance discharge rather
  than any external signal — i.e. it's self-triggering, not picking up
  noise. **Practical implication: a `sync()` success by itself is not
  proof a target is actually present and wired correctly** until this is
  either hardened (e.g. a plausibility/sanity check on the measured
  frequency, or requiring the measurement to repeat consistently across
  more than one probe-ladder rung) or at least documented as a known
  limitation in the UI. Not yet fixed — flagging for whoever picks this
  up next. The actual read/write values obtained after this bogus sync
  are meaningless (floating-bus noise), as expected — this doesn't
  invalidate the B1 PIO-loading confirmation above, which only depends on
  the PIO programs loading successfully, not on the data being correct.

Still unverified (needs a real target wired up per
`hardware/pico_mcu_wiring_schematic.pdf`): whether SYNC measures a real
target's pulse correctly, all FLASH operations, the breakpoint, and
whether the speedup pulse/wait-for-high behave correctly against actual
BDC protocol timing rather than a floating pin.

## Live bring-up with a REAL S9S08SG8 target attached (2026-09-12)

Wiring at the time of these tests: Pico GPIO15 = BKGD, GPIO14 = RESET,
**R1 10k pull-up BKGD->VDD installed**, C1 0.1uF VDD-VSS decoupling
installed.

This section is written incrementally as findings land. Everything here is
measured on real hardware unless explicitly marked as inference.

### Finding 1 — the "periodic pulse train" is on RESET (GPIO14), NOT BKGD

A previous (rate-limit-killed) session recorded only the phrase "a
periodic pulse train on BKGD" with no detail. Re-measured directly on the
Pico with plain GPIO reads (no PIO involved, target idle, no BDC traffic):

```
  BKGD  GPIO15 pull=None   transitions=0  low=0.00%     <- dead quiet, solid high
  BKGD  GPIO15 pull=UP     transitions=0  low=0.00%
  BKGD  GPIO15 pull=DOWN   transitions=0  low=0.00%     <- R1 10k pull-up confirmed present
  RESET GPIO14 pull=None   transitions=13686  low=16.49%
  RESET GPIO14 pull=UP     transitions=20511  low=25.24%
  RESET GPIO14 pull=DOWN   transitions=0      low=0.00%
```

`machine.time_pulse_us()` on GPIO14, 40 consecutive pulses:

```
  low  widths us: 8,8,8,8,8,8,4,8,8,8,8,8,9,8,8,8,...  (min 4, max 9, avg 8.0)
  high widths us: 16,23,23,23,22,21,21,23,22,23,41,31,39,25,27,... (min 16, max 52, avg 29.5)
  -> ~8 us low, ~30 us high, period ~37 us, ~27 kHz, ~17-25% low duty
```

Same measurement on GPIO15/BKGD returns `time_pulse_us(...) == -2`
(timeout, no falling edge ever) and `-1` — i.e. **BKGD never moves at
all** while idle. So the pulse train is definitively on the RESET line,
and the earlier "on BKGD" note was a misattribution.

Also confirmed by the same test: **R1 really is installed and working** —
GPIO15 stays high even with the RP2040's internal ~60k pull-down engaged,
which only a much stronger external pull-up can do.

### Finding 2 — reads are not reading the target at all, they're reading the idle-high line

`read_status` after `hw_reset_to_bdm` + `sync`, real hardware, this session:

```
>>> {"cmd":"sync"}         <<< {"target_freq_hz": 16161616.0, "ok": true}
>>> {"cmd":"read_status"}  <<< {"value": 255, "ok": true}    0b11111111
>>> {"cmd":"read_status"}  <<< {"value": 127, "ok": true}    0b01111111
>>> {"cmd":"read_status"}  <<< {"value": 127, "ok": true}
>>> {"cmd":"read_status"}  <<< {"value": 127, "ok": true}
>>> {"cmd":"read_status"}  <<< {"value": 127, "ok": true}
>>> {"cmd":"read_status"}  <<< {"value": 127, "ok": true}
```

Cross-referenced against every previously recorded odd value — 255, 191,
127, 63, 31, and the 0xFF/0xFF at 0xFFFE/0xFFFF that looked like a
correct blank reset vector. **They are all the same shape: all-ones with
a variable number of leading zeros.**

```
  255 = 11111111   0 leading zeros
  127 = 01111111   1
   63 = 00111111   2
   31 = 00011111   3
  191 = 10111111   (a single 0 in bit6 — a glitch one bit in, same family)
```

That is exactly what you get when the target never drives BKGD during the
read bits: every sampled bit returns the pull-up's idle 1, except for the
first few, which occasionally catch a residual low left over from our own
drive. **The 0xFF/0xFF "correct blank reset vector" result was a false
positive** — it was not a blank-flash read, it was the pull-up. Any read
of any address returns 0xFF-ish. This also retires the discarded "reads
drop the first N bits" hypothesis: nothing is being dropped, there is
simply no data on the wire to drop.

### Finding 3 — SYNC "success" is also not proof of a target (the known false-positive, now with a pull-up)

`sync()` reports ~15.2-16.3 MHz repeatably. Working backwards through
`sync()`'s own math, 16161616 Hz corresponds to an elapsed measurement of
198 PIO cycles at the 25 MHz probe clock = a **7.92 us low pulse** on
BKGD. But the idle-line measurement above shows BKGD produces *no*
falling edges at all outside a SYNC attempt, and CONTEXT.md's 2026-09-11
entry already recorded SYNC false-positiving at ~11 kHz on a bare
floating pin. Treat the reported target frequency as unconfirmed until
something independent corroborates it.

### Finding 4 — the pulse train on BKGD, fully characterized (this IS the lost "major finding")

Captured with the on-board Scope (`bdc_sample` PIO SM) at four different
sample rates, driving a deliberately SHORT 10 us SYNC low pulse (not the
firmware's default 10 ms — see Finding 5) so the target's answer lands
inside the capture window. `L`/`H` are low/high run lengths in us:

```
500 ns/sample (64 us window):
  L10.00 H29.50 L8.00 H16.50
  L10.00 H25.00 L8.00 H21.00

1.5 us/sample (192 us window):
  L10.50 H24.00 L7.50 H31.50 L9.00 H31.50 L7.50 H31.50 L7.50 H31.50
  L10.50 H24.00 L7.50 H31.50 L9.00 H30.00 L9.00 H31.50 L7.50 H31.50

5 us/sample (640 us window):
  L10 H30 L10 H30 L5 H35 L5 H35 L5 H30 L10 H30 L10 H30 L10 H30 L10 H30
  L5 H35 L5 H35 L5 H30 L10 H30 L10 H30 L10 H30 L5 H35 L5 ...   (continues)

15 us/sample (1.92 ms window):
  L15 H15 L15 H60 L15 H30 L15 H60 L15 H105 L15 H60 L15 H30 L15 H60
  L15 H105 L15 H1245     <- train STOPS, line stays high for the rest
```

**The pulse train is: ~8 us LOW, ~31 us HIGH, period ~39 us (~26 kHz),
starting right after the host releases BKGD at the end of a SYNC, running
for roughly 500-700 us, then stopping.** (The 5 and 15 us traces are
aliased — their apparent 5/10/15 us runs are quantisation of the same
~8 us low. The 500 ns trace is the accurate one.)

The critical number: **8 us low = 128 target BDC cycles at 16.3 MHz**
(128 / 16.3e6 = 7.85 us). That is exactly the length of a BDC SYNC
response pulse. So the train is *the target emitting a whole burst of
SYNC responses*, roughly 15-20 of them, in answer to one SYNC request.

`sync()` measures only the FIRST pulse of that burst and gets the right
answer — which is why SYNC "works". Everything after it is the problem:
the remaining ~15 response pulses keep arriving for the next ~700 us,
straight through the window where the host has already moved on and
started clocking out the first bits of its next BDC command.

Note the period match with Finding 1: the ~8 us low / ~30 us high train
on RESET (GPIO14) has the *same* signature, so the two observations are
almost certainly the same physical event seen on two lines (either the
target genuinely pulsing both, or coupling between them). Resolving which
is Finding 6 below.

### Finding 5 — the SYNC request drives BKGD low for 10 MILLISECONDS

Not a measurement, a code reading confirmed by the captures: `sync()`
computes `host_low_cycles = int(probe_freq * window_s)` — for the first
ladder rung that is `25e6 * 0.010` = **250,000 PIO cycles at 25 MHz = 10
ms of BKGD held low**. The lower rungs are worse: 100 ms and then a full
**1 second** of BKGD low.

The spec only asks for >= 128 target BDC cycles (7.9 us here). 10 ms is
1,270x too long.

**CORRECTION, measured later the same session — do not repeat the guess
this paragraph originally made.** The first draft of this finding said the
10 ms low was "very likely what provokes the burst" in Finding 4. That was
a hypothesis, and it is **wrong**. Sweeping the host-low duration from
8 us to 10 ms and counting the target's falling edges in a fixed 384 us
window shows no relationship at all:

```
  host_low=     8us  sync=16.49 MHz   BKGD falls=3
  host_low=    10us  sync=16.49 MHz   BKGD falls=8
  host_low=    16us  sync=16.49 MHz   BKGD falls=8
  host_low=    32us  sync=16.49 MHz   BKGD falls=8
  host_low=   100us  sync=16.49 MHz   BKGD falls=8
  host_low=  1000us  sync=16.33 MHz   BKGD falls=8
  host_low= 10000us  sync=16.16 MHz   BKGD falls=10
```

The 10 ms low is still worth shortening — it makes every `sync()` cost at
least 10 ms, and it is what makes `capture_sync` useless as a diagnostic —
but it is a wart, not the bug. (The duration is not arbitrary either: the
rung's window is sized so a 128-cycle pulse from the *slowest* target that
rung can measure still fits, and by the same logic the low must be that
long to be >= 128 cycles of that same slowest target. The two just should
not share one number when the practical range of real S08 BDC clocks is
1-20 MHz.)

This is also why `capture_sync` in the shipped firmware is useless for
diagnosis: its window is 10 ms of solid low, and a 256-sample capture at
25 ns/sample only sees the first 6.4 us of it.

### Finding 6 — the host clocks bits 332x slower than the bit itself

Measured on the Pico (`time.ticks_us()` around `Bdc._write_bit` /
`_read_bit`, target synced at 16.33 MHz, k=7, sm_freq=114.29 MHz):

```
one 16-target-cycle bit window should take 2 us
8 write bits took 2659 us -> 332.4 us/bit
8 read bits  took 2751 us -> 343.9 us/bit
read_status() = 0x7F took 4478 us for 16 bits
```

So each bit is ~1 us of signal followed by **~331 us of idle line**,
because `_write_bit`/`_read_bit` construct a brand-new `StateMachine`
object (and re-do `Pin()` + `pio_gpio_init` + `pio_sm_init`) for every
single bit, then tear it down again. The BDC bits of one command byte are
spread over ~2.7 ms instead of ~8 us — the whole command byte takes
longer than the target's entire ~700 us SYNC-response burst, so the burst
is still in progress while the host is clocking out the command.

Bit *shapes* themselves are fine — Scope capture at 25 ns/sample with the
target synced at 16.33 MHz (61.2 ns/target cycle):

```
write_bit(1): LOW 75 ns  then high   (spec: 1 target cycle = 61 ns)  OK
write_bit(0): LOW 950 ns then high   (spec: 13 cycles   = 796 ns)    ~19% long
read_bit    : LOW 75 ns  then high, sampled bit = 1
```

### Finding 7 — wiring sanity, established by measurement not by inspection

Drive the pin low, release it to Hi-Z with **no** internal pull, and time
how long it takes to come back high. That distinguishes "has an external
pull-up" from "unconnected" without a meter:

```
GPIO15 BKGD   rise-to-high us: 64, 34, 29, 27, 29, 28, 29, 31      <- strong pull-up (R1 10k)
GPIO14 RESET  rise-to-high us: 231, 204, 163, 164, 180, 186, 157, 179  <- weaker pull-up (~5x)
GPIO0  (control, unconnected) : -1 x8   (never rose within 50 ms)
GPIO13 (control, unconnected) : -1 x8
```

So **both BKGD and RESET are genuinely connected** — RESET to something
with a weak pull-up, consistent with the target's internal RESET pull-up.
The unconnected control pins never rise at all, which makes this a clean
discriminator worth reusing.

They are also **not shorted to each other**: driving GPIO14 low leaves
GPIO15 at 0.0% low, in 4/4 trials.

### Finding 8 — the target free-runs and is NOT entering BDM; holding BKGD low is the only thing that stops it

Watching GPIO14 with plain SIO reads (30 ms windows, % of samples low):

```
1) both pins released                      GPIO14 low=13.6% 14.2% 13.9%   GPIO15 low=0.0%
2) while BKGD driven LOW by the Pico       GPIO14 low= 0.0%  (4/4 trials)
   immediately after releasing BKGD        GPIO14 low=11.2% 13.0% 14.2% 13.2%
3) while RESET driven LOW by the Pico      GPIO15 low= 0.0%  (4/4)
4) BKGD held low 200 ms                    GPIO14 low= 0% 0% 0% 0% 0%
   then released                           GPIO14 low=15% 17% 16% 16% 13% 12% 14% 14% 12% 13%
5) BDM-entry attempt: BKGD low, RESET low, release RESET, hold BKGD, release BKGD
   hold= 1 ms -> GPIO14 low over next 5 windows: 13% 13% 12% 11% 14%
   hold= 10 ms ->                                14% 13% 14% 15% 15%
   hold=100 ms ->                                15% 13% 13% 13% 13%
```

Two things fall out of this:

- **Holding BKGD low reliably silences the target's activity, every time,
  and releasing it restarts it immediately.** So the target is powered,
  alive, and reacting to BKGD.
- **The `hardware_reset_to_bdm()` sequence does not latch.** Whether BKGD
  is held for 1 ms, 10 ms or 100 ms past the release of RESET, the moment
  BKGD goes high the target resumes free-running. A target that had
  actually entered active background mode would stay halted. So every
  `read_status` / `read_byte` result recorded in this project so far was
  taken against a target that was *running*, not halted in BDM.

The ~8 us / ~39 us activity itself is consistent with a blank-FLASH
reset loop: an erased part has reset vector 0xFFFF, so the CPU fetches
0xFF at 0xFFFF, takes an illegal-opcode reset, and repeats. The ~8 us low
also matches the S08's documented internal reset drive of 34 bus cycles
at the default FEI bus clock (34 / 4.19 MHz = 8.1 us), which independently
corroborates a stock, untrimmed, never-configured target.

### Finding 9 — bits sent back-to-back, and a full bit-rate sweep: still no real data

To test whether the 332 us inter-bit gap (Finding 6) was the blocker, a
throwaway pair of PIO programs was written that clock the SM at **exactly
one PIO cycle per target BDC cycle**, which makes the 1/13/16-cycle bit
shapes plain `[delay]` counts and lets a whole multi-byte command go out
with no Python between bits *or* bytes (up to 4 bytes pre-loaded into the
TX FIFO). Sketch, for the record — this is the shape the real firmware
should move to:

```python
@asm_pio(set_init=PIO.IN_LOW, out_shiftdir=PIO.SHIFT_LEFT, autopull=False)
def tx_byte():                 # one FIFO word per byte, byte << 24
    pull(block); set(y, 7)
    label("bit")
    set(pindirs, 1)            # cyc 0   drive low
    out(x, 1)                  # cyc 1
    jmp(not_x, "zero")         # cyc 2
    set(pindirs, 0)     [11]   # cyc 3   release  -> '1' = 3 cycles low
    jmp(y_dec, "bit")          # cyc 15  = exactly 16 cycles/bit
    jmp("bit_done")
    label("zero")
    nop()               [9]    # cyc 3..12 still low
    set(pindirs, 0)     [1]    # cyc 13  release  -> '0' = 13 cycles low
    jmp(y_dec, "bit")          # cyc 15
    label("bit_done")
    nop()

@asm_pio(set_init=PIO.IN_LOW, in_shiftdir=PIO.SHIFT_LEFT,
         autopush=True, push_thresh=8)
def rx_byte():
    pull(block); mov(y, osr)   # bit count - 1
    label("bit")
    set(pindirs, 1)            # cyc 0   drive low 1 cycle
    set(pindirs, 0)     [8]    # cyc 1   release
    in_(pins, 1)               # cyc 10  SAMPLE
    nop()               [3]
    jmp(y_dec, "bit")          # cyc 15
```

It works (it assembles, loads, and produces correct waveforms), and it
changed the answers completely — but not into real data. Sweeping the
assumed BDC clock from 250 kHz to 20 MHz, 8 repeats each, after a
BDM-entry attempt at every rate:

```
READ_STATUS (0xE4):              READ_BYTE 0xFFFE (E0 FF FE):
   250000 Hz : 00 x8                250000 Hz : 00 x8
   500000 Hz : FF x8                500000 Hz : FF x8
  1000000 Hz : FF x8               1000000 Hz : FF x8
  2000000 Hz : 5A 5A AD AD AD..    2000000 Hz : 5A x8          <- stable but bogus
  3000000 Hz : BB 76 76 D9 76..    3000000 Hz : 6C 6C 76 66 76 66 66 66
  4000000 Hz : E7 39 67 9C CE..    4000000 Hz : 39 x7, 33
  6000000 Hz : E3 78 1E 1E 71..    6000000 Hz : E3 1E C7 F1 C7 8F 8F 8F
  8000000 Hz : F0 0F F0 F0 E1..    8000000 Hz : 0F E1 3C 7C 7C 1E 0F 0F
 10000000 Hz : 7F 07 F8 07 7C..   10000000 Hz : 07 x7, 3E      <- stable but bogus
 12000000 Hz : 07 E0 0F 0F 03..   12000000 Hz : 03 x8          <- stable but bogus
 16500000 Hz : 00 3F 07 07 00..   16500000 Hz : FF, then 00 x7
 20000000 Hz : 00 FF 7F 00 00..   20000000 Hz : 07 00 C0 FC FF 00 00 F8
```

Every one of these decodes to the *same physical waveform*: a single low
period of about 8 us followed by high, sampled at whatever bit rate we
happened to choose. At 12 MHz (1.33 us/bit) a 6-bit leading low = 8 us
-> 0x03. At 10 MHz (1.6 us/bit) a 5-bit leading low = 8 us -> 0x07. The
seductively stable `0x5A` at 2 MHz is just that same 8 us pulse aliasing
against the target's ~39 us repetition period.

**Conclusion: the inter-bit gap was NOT the (only) blocker, and there is
no bit rate at which this target answers a BDC command. The only thing
ever present on BKGD is one ~8 us low pulse. The target's BDC is not
accepting commands at all.**

This also retires the `0x5A`-looking results as evidence of anything —
note the old session's `write_byte(0x0080, 0x5A)` then reading back
non-0x5A values; 0x5A can appear here purely as an aliasing artifact.

### Finding 10 — the command byte is correct on the wire, and the target's pulse is triggered by our '0' bits

A 256-sample, 120 ns/sample capture (RX FIFO joined for 8 contiguous words)
of `send(0xE4)` decodes, at 8 samples per bit:

```
   _-------_-------_-------_______-_______-__------_______-________ ... (long low) ... -----
      1       1       1       0       0       1       0       0
```

which is **exactly 0xE4 = 1110 0100, MSB first**, with a `1` = short low
(3 cycles) / mostly high, and a `0` = long low (13 cycles) then high.
So the host's encoding, bit order, and both bit shapes are correct. This
is now verified by measurement, not by reading the manual.

Then the trigger question. Sending different patterns and locating any low
run longer than 2.4 us (which can only be the target, since our own
longest low is 13 cycles = 0.8 us):

```
  0xFF  (all '1', longest own low = 3 cycles):  3 of 4 runs -> no long low at all
  0x00  (all '0', longest own low = 13 cycles): 4 of 4 runs -> long low, starting t=0.0-1.8us
  0xE4  READ_STATUS:                            4 of 4 runs -> long low, starting t=2.9us (x3), 5.8us
  0x90  BACKGROUND:                             2 of 4 runs -> long low at t=3.8us
  0x00 x4 (32 '0' bits over 31 us):             3 long lows per window, ~10 us apart
```

**0xE4 = 1,1,1,0,... — its first '0' bit starts at 3 x 0.97 us = 2.91 us,
and the target's long low starts at 2.9 us.** That is not a coincidence
and it is not a free-running reset loop: the target is extending our
'0' bits. `0xFF`, which contains no '0' bit, mostly draws no response at
all.

So the target's BDC *is* listening to the line and reacting to it. It just
never executes anything.

### Finding 11 — BACKGROUND never halts the target, at any bit rate

A functional test that needs no working read path: `BACKGROUND` (0x90)
halts the CPU into active BDM, so if the target received it, its
free-running activity (measured as % of samples GPIO14 reads low) must
drop to zero.

```
  baseline GPIO14 activity: 13.5% 14.3% 14.4% 13.8%

     500000 Hz : before=15.0%  after 0x90= 13.7% 13.7% 13.2%
    1000000 Hz : before=13.9%  after 0x90= 14.2% 13.5% 13.9%
    2000000 Hz : before=15.0%  after 0x90= 13.8% 14.5% 12.7%
    4000000 Hz : before=14.6%  after 0x90= 15.9% 15.6% 14.9%
    6000000 Hz : before=14.9%  after 0x90= 14.7% 14.2% 14.5%
    8000000 Hz : before=14.3%  after 0x90= 14.6% 15.2% 14.7%
   10000000 Hz : before=14.6%  after 0x90= 12.8% 13.9% 14.2%
   12000000 Hz : before=15.6%  after 0x90= 15.4% 13.9% 12.8%
   16600000 Hz : before=14.8%  after 0x90= 13.5% 12.9% 12.9%
   20000000 Hz : before=14.5%  after 0x90= 14.5% 14.2% 14.1%
```

Nothing, at any rate. Contrast with Finding 8: simply holding BKGD low
electrically *does* take the activity to 0.0%. So the target reacts to the
pin, never to the protocol.

### Finding 12 — asserting RESET does not change the target's behaviour at all

Using the target's own extended-low response to `0x00` as the liveness
probe (8 trials per condition):

```
  A) RESET released (normal)        responded 8/8
  B) RESET held LOW by the Pico     responded 8/8     <- 20 ms of asserted RESET
  C) RESET released again           responded 8/8
  D) after hw_reset_to_bdm          responded 8/8
```

The BDC does stay alive through reset on HCS08, so B is not by itself
proof that RESET is dead — but combined with Finding 8 (BDM entry never
latches no matter how long BKGD is held past the release of RESET) it
means **nothing we do to the RESET line has ever been observed to change
the target's state.**

### Where this leaves the investigation

Established beyond reasonable doubt, all by measurement on this hardware:

1. Both BKGD and RESET are physically connected (Finding 7).
2. The host's SYNC pulse, bit encoding, bit order and both bit shapes are
   correct on the wire (Findings 4, 10).
3. The target's BDC is powered and reacting to BKGD — it extends our '0'
   bits, and holding BKGD low silences it (Findings 8, 10).
4. **No BDC command has ever been executed.** Not at any of ten bit rates
   from 250 kHz to 20 MHz; not `READ_STATUS`, not `READ_BYTE`, not
   `BACKGROUND`; and active background mode has never been entered
   (Findings 9, 11).
5. Everything that previously looked like a working read — the "blank
   reset vector" 0xFF/0xFF, the stable 0x5A, the 0x7F/0xBF status values —
   is the idle pull-up plus that single ~8 us pulse, sampled at whatever
   bit rate happened to be in use (Findings 2, 9).

The remaining candidate explanations, in rough order of likelihood, and
none of them yet tested:

- **The part is secured.** An erased HCS08's NVOPT/FOPT reads 0xFF, and on
  this family the SEC bits only read "unsecured" for one specific
  combination. A secured part answers SYNC and reacts to the line but
  refuses memory access and refuses to halt into active BDM, which is
  exactly the symptom set above. The documented way out is a BDC
  mass-erase/unsecure sequence. **This needs checking against the real
  MC9S08SG8 datasheet §4.5 security table before acting on it — the
  encoding is stated from memory here and is NOT verified.**
- The BKGD pin function is disabled (SOPT1.BKGDPE), though that should
  also kill the pin reaction we do see.
- The part is not actually an MC9S08SG8/SG4 and the pinout assumption in
  `hardware/pico_mcu_wiring_schematic.pdf` is wrong for it.

What is now firmly ruled out: inter-bit gaps alone, bit-rate calibration,
bit shape, bit order, the SYNC handshake, and the wiring being open.

### What was CHANGED as a result (2026-09-12), and the hardware evidence

Three real firmware defects were found above and all three are fixed and
deployed. None of them is the reason the target won't talk — that is still
open — but all three were blocking any further progress, because two of
them made the diagnostics lie and the third made the protocol unusable
even against a working target.

**1. Bit-at-a-time PIO replaced with byte-at-a-time (Finding 6).**
`bdc_pio.py`'s `bdc_write_bit` / `bdc_read_bit` are gone, replaced by
`bdc_tx_byte` (12 instructions) and `bdc_rx_bits` (7). The state machine is
now clocked at **exactly one PIO cycle per target BDC cycle**, which is
what lets the 1/13/16-cycle shapes be plain `[delay]` counts inside PIO's
5-bit delay field, so a whole byte comes out of one FIFO word and up to 4
bytes (the longest BDC command — WRITE_BYTE opcode + addr16 + data) leave
the Pico as one uninterrupted bit stream. `bdc.py` caches the two state
machines and rebuilds them only when something else has stolen the BKGD
pin's GPIO function select (a `Pin()`, the SYNC SM on block 0, or the
capture SM), tracked by `Bdc._pio_bound`.

`_configure_bit_timing` no longer has a `k` oversampling factor;
`PIO_CYCLES_PER_TARGET_CYCLE` and `PIO_CYCLES_PER_TARGET_CYCLE_MIN` are
gone. PIO block 1 now holds `bdc_sample`(5) + `bdc_tx_byte`(12) +
`bdc_rx_bits`(7) = 24/32.

**2. `SYNC_ATTEMPTS` gained a separate host-low duration (Finding 5).**
Each rung is now `(probe_clock_hz, window_seconds, host_low_seconds)`
with host lows of 200 us / 2 ms / 20 ms instead of the 10 ms / 100 ms /
**1 second** that fell out of reusing `probe_freq * window_s`.

**3. `_capture()` takes a `window_us` (Finding 4).** The capture rate was
previously pinned to bit-level zoom, so the Scope could only ever see
~6 us and `capture_sync` was guaranteed to show a flat line. It now takes
a requested time span, reachable from the wire protocol as
`{"cmd":"capture", ..., "window_us": N}`. `capture_command` was added
(`{"cmd":"capture","test":"command","opcode":...,"addr":...}`) to capture a
whole real command instead of one isolated bit. The 4-word RX FIFO
real-time limit is now documented in the docstring rather than being an
invisible trap.

Measured on the real Pico + real target, before -> after:

```
sync()                >10 ms   ->  1544 us
per bit (tx)          332 us   ->  47.6 us/byte-amortised, and the 8 bits
                                   of a byte are now CONTIGUOUS (7.8 us
                                   total) instead of spread over 2.7 ms
read_status()         4478 us  ->   924 us
read_byte()                    ->   829 us
capture_sync          flat line (10 ms low vs 6.4 us window) -> see below
```

`capture_sync` now actually shows the handshake and the pulse train, via
the ordinary `{"cmd":"capture","test":"sync"}` command, 2343 ns/sample,
600 us window:

```
L199.2us   <- the host's own SYNC low (now 200 us, was 10 ms)
H 25.8us   <- gap
L  7.0us   <- target response #1  (~128 BDC cycles at 16.5 MHz)
H 32.8us
L  7.0us   <- #2
H 30.5us
L  9.4us   <- #3
H 30.5us   ... continues for the rest of the window
```

Transmit shapes, `capture_write_bit(1)` = a whole 0xFF byte, 78 ns/sample:

```
L0.16 H0.78  L0.23 H0.78  L0.16 H0.78  L0.23 H0.78
L0.16 H0.78  L0.16 H0.78  L0.31 H0.70  L0.31 H2.89     (us)
```

Eight identical bits, each ~0.16-0.31 us low then ~0.78 us high, 0.97 us
total = 16 cycles at 16.5 MHz. Exactly the specified '1' shape, eight
times, back to back. `capture_command(0xE4)` decodes as `1,1,1,0...`
followed by the target's pulse, as in Finding 10.

**What is NOT fixed:** the target still never executes a command. Reads
now return varying values of the `2^n - 1` family (`00 07 7F 1F E0 00 ...`)
rather than a stable 0xFF, which is *expected* and is progress in
honesty — the command bytes now go out fast enough that the read phase
lands at a random point in the target's ~39 us response train instead of
always landing in the same place. Nothing here should be read as the
target working.

**Deliberately left alone:** `Bdc._rx_bits` raises `BdcError` on timeout
instead of returning whatever the pull-up says. That means a silent target
now produces an error rather than a plausible-looking 0xFF — which is the
behaviour that would have caught this whole class of false positive months
ago. Do not "fix" it back into returning a value.

Regression check after the rewrite — all 24 serial commands exercised
against the real Pico and real target, no hang and no crash anywhere:

```
ping / hw_reset_to_bdm / sync / read_status / read_byte / write_byte /
read_block / read_reg PC / read_reg A / write_reg A / read_bkpt /
write_bkpt / write_control / background / go / step / tagged_go /
reset_target / capture{sync,write_bit,read_bit,command}   -> all {"ok": true}
capture test "bogus"  -> {"ok": false, "error": "unknown capture test: 'bogus'"}
"bogus_cmd"           -> {"ok": false, "error": "unknown command: 'bogus_cmd'"}
```

(The *values* those commands return are still meaningless — see above.
This only confirms the rewritten tx/rx path is reachable from every call
site and that the error paths still behave.)

### Suggested next steps for whoever picks this up

> **SUPERSEDED — read the next section ("Second live session") first.** The
> two-pin scope suggested as item 4 below was built, and it showed that the
> ~8 us pulse everything here is reasoned from is a VDD brownout dip, not a
> BDC response. Items 1-3 are downstream of an assumption that no longer
> holds.

1. **Check the MC9S08SG8 datasheet §4.5 security table** and confirm
   whether a blank NVOPT leaves the part secured. If it does, that is
   almost certainly the answer, and the next move is the BDC
   mass-erase/unsecure sequence rather than any more protocol debugging.
2. Independently confirm the target's identity and pinout. Everything in
   this project assumes `hardware/pico_mcu_wiring_schematic.pdf` is right
   about which TSSOP-20 pin is RESET and which is BKGD.
3. Use `{"cmd":"capture","test":"command","opcode":N,"addr":M,
   "window_us":W}` for any further protocol work. It is now the fastest
   way to see ground truth, and every conclusion in Findings 4-12 came
   from it or from the two-pin variant of it.
4. The useful two-pin (BKGD + RESET simultaneously) scope program is not
   in the firmware — it was a throwaway probe. If RESET behaviour matters
   again, it is five lines: `in_base=Pin(14)`, `in_(pins, 2)`, 16 samples
   per word, bit0 = GPIO14 and bit1 = GPIO15.

## Second live session, same day (2026-09-12, later) — instrumentation and the two-pin scope

Everything in this section is measured on the same real hardware.

### Tooling added first (all in `firmware/`, all deployed and working)

- `{"cmd":"raw_xfer","tx":[...],"nbits":N}` — send an arbitrary byte
  sequence then optionally clock in N bits, and **report the real-time
  cost of each phase**. Nothing before this could send a byte sequence the
  firmware didn't already hard-code, which made every protocol hypothesis
  untestable without a redeploy.
- `{"cmd":"probe_pin","pin":N,"ms":M}` — % of samples low + transition
  count on any GPIO, via plain SIO reads.
- `{"cmd":"drive_pin","pin":N,"level":0|null}`.
- `{"cmd":"set_bit_clock","hz":N}` — force the bit clock without running
  SYNC, so the bit rate can be swept independently of SYNC's assumption.
- `{"cmd":"capture","test":"xfer","tx":[...],"nbits":N}` — capture a WHOLE
  transaction. `capture_command` only ever showed outgoing bytes and
  `capture_read_bit` only ever showed a read phase with no command in front
  of it, so neither could show whether the target answers a real command.
- `{"cmd":"capture2",...}` — **two-pin capture**: BKGD and RESET sampled
  together, 2 bits per sample (`bdc_sample2`, PIO block 1, SM 7). This is
  the instrument CONTEXT.md's own "next steps" asked for, and it is what
  broke the deadlock.

### Finding 13 — the tx→rx turnaround is NOT the problem (it is ~16 us)

Measured by `raw_xfer` on real hardware:

```
READ_STATUS (1 tx byte + 8 rx bits): tx_us=235-306  gap_us=15-18  rx_us=73-90
READ_BYTE   (3 tx bytes + 8 rx bits): tx_us=335-342 gap_us=15-17  rx_us=74-76
```

The `gap_us` is the real time between the last transmitted bit and the
first read bit. Pre-arming the RX state machine (it stalls on its first
`pull` without touching pindirs, so an armed-but-idle RX SM does not
disturb the line) brings the turnaround down to ~16 us — comfortably
inside the target's ~39 us activity period. So "the command is thrown away
before the read phase" is ruled out.

Note the `tx_us` of ~235 us for 7.8 us of signal: that is MicroPython's
`StateMachine.active()` overhead either side of the burst. The *bytes*
themselves are contiguous (verified by capture); only the setup is slow.

### Finding 14 — `read_status` now returns a stable 0x00, not the old 0xFF family

After the byte-streaming rewrite, `read_status` is repeatable:

```
hw_reset_to_bdm; sync -> 16494845 Hz
read_status -> 0x00, 0x00, 0x00, 0x00   (was 0xFF/0x7F/0x3F... in the old code)
read_byte(0x1820) -> 0x00, 0x00
```

0x00 is *also* BDCSCR's reset value, so this is tantalising — but writes do
not stick, which is the point of Finding 15.

### Finding 15 — WRITE_CONTROL never changes BDCSCR

```
write_control 0x80 -> read_status 0x00, 0x00
write_control 0xA0 -> read_status 0x00, 0x00
write_control 0x90 -> read_status 0x00, 0x00
write_control 0x00 -> read_status 0x00, 0x00
background         -> read_status 0x00, 0x00
```

BDCSCR is a BDC register, not memory, so FLASH security cannot explain a
write to it being ignored. Either the write path does not reach the target
at all, or the read is not really reading BDCSCR. Either way, **the
`flash_init_clock` "DIVLD not set" failure is not FCDIV-specific and not
security-specific — no BDC register write of any kind lands.**

### Finding 16 — THE BIG ONE: BKGD's ~8 us low pulse is SIMULTANEOUS with RESET going low

This is what the two-pin scope was built for, and it immediately answered
the question every single-pin capture in this project has been ambiguous
about. Both traces below are 781 ns/sample, 200 us window, during a
`READ_STATUS` transaction (`tx=[0xE4]`, then 8 read bits):

```
BKGD  -_--__-_____________----------------------------------------__________-------------
RESET __--________________----------------------------------------___________------------
                                                                  ^^^^^^^^^^
                                                        BOTH LOW, SAME SAMPLES

BKGD  -___-_____________----------------------------------------__________----------------
RESET __________________----------------------------------------__________----------------
                                                                ^^^^^^^^^^
```

And the control, transmitting `0xFF` (a byte with **no '0' bits**, so our
own longest low is only 3 BDC cycles = 0.18 us):

```
BKGD  -_----_-------------------------------------------------------------------------
RESET __--___--_------------------------------___________------------------___________
```

BKGD stays high the whole time while RESET keeps pulsing on its own.

Two things follow, and they overturn the central assumption of Findings
3/4/9:

1. **The ~8 us low on BKGD is not a BDC SYNC response.** A SYNC response
   is the BDC driving BKGD alone; it has no reason to coincide with the
   RESET pin going low. These are the same physical event seen on two
   lines — a RESET of the target.
2. **It is provoked by our '0' bits.** `0xE4` (which contains '0' bits,
   each 13 BDC cycles = 0.8 us low) reliably produces it; `0xFF` (longest
   low 0.18 us) does not, while RESET carries on pulsing by itself.

The immediate consequence for everything upstream: **SYNC's 16.3-16.5 MHz
"target BDC clock" is very probably not a BDC clock measurement at all.**
It is derived from `128 / pulse_width`, and the pulse it measures is this
reset-coincident 8 us low. 8.0 us is also exactly the S08's documented
internal reset drive of 34 bus cycles at the stock 4.19 MHz FEI bus clock
(34 / 4.19e6 = 8.1 us) — the coincidence CONTEXT.md already noted in
Finding 8, now confirmed to be the *same* pulse. So the number `sync()`
reports, and therefore every bit period this driver has ever produced, is
calibrated against a reset pulse rather than a BDC clock.

### Finding 17 — `read_status` = 0x00 is an ARTIFACT, proven by a bit-rate sweep

`set_bit_clock` makes it possible to sweep the bit rate without SYNC. Same
target, same `hw_reset_to_bdm`, only the bit clock changes:

```
rate       | READ_STATUS | RESET activity after ENBDM+BACKGROUND | 3 more reads
250000     | 0x00        | 17.5 16.3 15.1                        | 00 00 00
500000     | 0xFF        | 14.2 16.6 16.8                        | FF FF FF
1000000    | 0xFF        | 15.8 17.4 18.2                        | FF FF FF
2000000    | 0x56        | 14.8 17.4 16.3                        | AD B5 AD
3000000    | 0x6C        | 15.7 16.1 16.5                        | 6C 6E 6C
4190000    | 0x39        | 15.5 16.5 16.6                        | 73 39 73
6000000    | 0x1E        | 16.4 17.3 17.1                        | 71 1E 1E
8390000    | 0x0F        | 17.3 16.3 17.2                        | C3 0F 0F
10000000   | 0x07        | 17.8 15.9 16.3                        | 0F 07 7C
12000000   | 0x03        | 16.0 18.2 16.7                        | 0F 07 03
16494845   | 0x00        | 17.3 14.8 15.8                        | 00 03 00
20000000   | 0x00        | 15.9 17.1 16.1                        | 07 00 00
```

The read value is a pure function of the bit rate: it is the same single
~8 us low pulse sampled at 8 different points. At 16.5 MHz the whole 8-bit
read phase (7.8 us) fits *inside* that pulse, so every bit samples low and
the answer is 0x00. **The stable 0x00 of Finding 14 is the pull-up/pulse
artifact in a new costume, not BDCSCR.** Baseline RESET activity is
16.5-17.4% and nothing — no bit rate, no `ENBDM`+`BACKGROUND` burst sent as
one uninterrupted 3-byte stream — moves it.

(The `ENBDM`+`BACKGROUND` test is worth keeping in mind for a future
target: HCS08's `BACKGROUND` is documented as ignored while ENBDM=0, and
every earlier test in this project sent a bare `BACKGROUND`. Sending
`C4 80 90` as one 24-bit burst takes 23 us, which fits inside the target's
~39 us activity period. It still does nothing here.)

### Finding 18 — BKGD and RESET move IDENTICALLY, sample for sample, and this explains everything

Two-pin capture across a whole SYNC handshake (host drives BKGD low for
200 us, releases, then the target "responds"). 1953 ns/sample, 1000 us
window, 512 samples. `_` = low, `-` = high:

```
t=    0.0us BKGD  _________________________________________________________________----------------------------
            RESET _________________________________________________________________----------------------------

t=  214.8us BKGD  ----------------_---------------_----------------------------____------------____------------
            RESET ----------------_---------------_----------------------------____------------____------------

t=  429.7us BKGD  __---____----------__----------------------------__----------____--------____----------------
            RESET __---____----------__----------------------------__----------____--------_____---------------

t=  644.5us BKGD  -____-------------___---____---------___---------------------------------------------------
            RESET -____-------------___---____---------___-------------_-----____--------____----------__----
                                                       ^^^ from here on they DIVERGE

t=  859.3us BKGD  ------------------------------------------------------------------------
            RESET ---------------------__--------------__-----------____--------____------
```

With `host_low_us=2000` the two traces are **identical for the entire
1000 us window**.

This is the single most informative measurement taken in this project.
Read what it says carefully:

1. For roughly **700 us after the host releases BKGD**, BKGD and RESET are
   the *same waveform* — every edge, every run length, on two nets that are
   provably NOT shorted together (driving GPIO14 low leaves GPIO15 at 0.0%
   low, and at idle RESET pulses continuously while BKGD shows literally
   zero transitions).
2. After ~700 us BKGD settles high and stays high, while RESET carries on
   pulsing at its own ~39 us rate indefinitely.

Two independent nets, each with its own pull-up, cannot track each other
edge-for-edge unless **the thing they are both pulled up to is moving**.
Both pull-ups reference the target's VDD: R1 (10k, BKGD->VDD) and the
target's internal RESET pull-up (measured ~5x weaker, so ~50k). If VDD
collapses, both pins collapse; when VDD recovers, both recover. That is
exactly the observed waveform.

**So the target's VDD is collapsing and recovering, and the ~8 us "BDC
SYNC response" this project has been calibrating against since day one is
a VDD dip, not a BDC response at all.**

### Finding 19 — there is a ~0.1 uF capacitor sitting on the GPIO14 ("RESET") net

`rise_time` drives a pin low for a given time, releases it to Hi-Z with **no
internal pull**, and times how long the external pull-up takes to bring it
back high. It is a capacitance probe: a node that is just a pull-up plus
tens of pF recovers in about a microsecond no matter how long it was held
down.

```
  GPIO15 (BKGD)  hold=  1 ms  rise_us = 64  56  32  56  33
  GPIO15 (BKGD)  hold= 10 ms  rise_us = 57  38  35  34  34
  GPIO15 (BKGD)  hold=100 ms  rise_us = 50  59  34  56  58
  GPIO14 (RESET) hold=  1 ms  rise_us = 1188 1216 1222 1216 1202
  GPIO14 (RESET) hold= 10 ms  rise_us = 1536 1549 1571 1550 1548
  GPIO14 (RESET) hold=100 ms  rise_us = 1559 1535 1549 1567 1568
  GPIO0/13/16 (known unconnected) = -1 (never rose within 50 ms)
```

30-60 us on BKGD is at the floor of what this measurement can resolve (the
MicroPython polling loop itself costs tens of us). **1.2-1.6 ms on GPIO14
is 25-50x that, and it is a real RC**: with any plausible pull-up
(10k external or ~50k internal) that recovery time needs **30-150 nF on the
node**. The only capacitor in this build is C1, 0.1 uF, which the schematic
places across VDD-VSS.

And it is GPIO14's own pull-up doing it, not a path through BKGD — GPIO14
still rises in 1.5-2.0 ms whether BKGD is released, held low, or driven
high:

```
  GPIO14 rise, GPIO15 released : 1536 1574 1549 1533
  GPIO14 rise, GPIO15 LOW      : 2021 2034 2015 2013
  GPIO14 rise, GPIO15 HIGH     : 1817 1901 1901 1899
```

### Finding 20 — GPIO14 behaves like a power net, not a reset net

```
BKGD released (Hi-Z)  -> GPIO14 low=16-17%, ~330-360 transitions per 25 ms
BKGD driven HIGH      -> GPIO14 low= 0.0%,  0 transitions   (3/3 trials)
BKGD driven LOW       -> GPIO14 low= 0.0%,  0 transitions   (3/3 trials)
BKGD released again   -> GPIO14 low=15-18%, ~330 transitions
```

Only a **Hi-Z** BKGD produces the 27 kHz train; pinning BKGD at either rail
stops it dead. A reset input does not behave like that; a supply node being
charged through a resistor from a line that is sometimes Hi-Z does.

And driving GPIO14 itself:

```
GPIO14 driven LOW  -> sync FAILS outright:
                      "no response pulse from target within 1000 ms"
                      (the only condition in this whole project that has
                       ever produced a clean no-response)
                      reads = FF FF FF  (pure pull-up, target silent)
GPIO14 driven HIGH -> sync still "succeeds"; reads = FF at every bit rate
                      >= 8.39 MHz and 00 at every rate <= 4.19 MHz
GPIO14 released    -> everything returns to the old behaviour
```

**Grounding GPIO14 silences the target completely.** That is what shorting
a supply does. Holding a reset input low leaves the BDC alive on HCS08.

### Finding 21 — the physical diagnosis: this is a POWER/WIRING fault

Putting Findings 18-20 together, one model accounts for every observation
ever recorded in this file, and nothing else tried accounts for more than
a few of them:

**The target is not properly powered. Its VDD rail is browning out at
~27 kHz, and the GPIO14 wire — the one this project has always called
RESET — is on, or shares a node with, that rail (C1 is measurably on it).**

The most likely specific fault is that the **3V3(OUT) -> target VDD (pin 3)
connection is open** (or VSS pin 4 is not returned to GND), leaving the
chip fed only through an ESD clamp diode from BKGD via R1 — tens of
microamps into a part that needs milliamps — which is a textbook
relaxation-oscillator brownout. A wire landing on the wrong TSSOP pin would
do it just as well.

Check it against the record:

| Observation | Explained how |
|---|---|
| Idle: RESET pulses ~8 us low / ~31 us high, BKGD dead quiet (Finding 1) | VDD relaxation-oscillates as the chip repeatedly browns out. RESET's weak internal pull-up follows VDD; BKGD is held up stiffly by R1 (10k) direct to the 3.3V rail, so it does not visibly move. |
| Holding BKGD LOW silences ALL target activity, every time (Finding 8, re-confirmed) | Holding BKGD low removes the only charging path and actively sinks it, so VDD goes to 0 and the chip is dead. Nothing to do with entering BDM. |
| Releasing BKGD restarts activity instantly | The charging path is restored. |
| After releasing BKGD, BKGD and RESET track identically for ~700 us (Finding 18) | While C1 recharges through R1, the chip drains everything R1 supplies, so the BKGD node itself sags with VDD. Once C1 is charged, R1 can hold BKGD up again and the two diverge. **R1 x C1 = 10k x 0.1uF = 1.0 ms**, which is the right order for the observed ~700 us. |
| The "SYNC response" is ~8 us and gives a plausible-looking 16.3-16.5 MHz (Findings 3, 4) | It is the first VDD dip after release, not a 128-cycle BDC pulse. The number is meaningless. |
| A '0' bit (0.8 us low) provokes a response; a '1' bit (0.18 us) usually does not (Finding 10) | A longer low drains more charge off the node, so it perturbs VDD enough to trigger a brownout; a short one does not. Nothing to do with the BDC decoding our bits. |
| No BDC command has ever executed, at any bit rate, ever (Findings 9, 11, 17) | The CPU/BDC never actually runs. |
| Nothing done to RESET ever changes anything (Finding 12) | The chip is not being held out of reset by the RESET pin in the first place. |

**This is a HARDWARE fault, not a firmware or protocol fault, and it means
the FLASH-security hypothesis is untested rather than confirmed** — you
cannot conclude anything about security from a part that is not powered.

### What to check on the bench — this is the next step, and it needs a multimeter, not more code

Do these five things **before writing another line of firmware.** Every
protocol conclusion in this file is downstream of them.

1. **Measure VDD (pin 3) to VSS (pin 4) on the chip's own legs**, Pico
   connected and idle. The model predicts well under 3.3 V, and/or visibly
   unstable. A solid 3.30 V falsifies the whole diagnosis.
2. **Continuity, Pico 3V3(OUT) to target pin 3**, and **Pico GND to target
   pin 4.** One of these is predicted open.
3. **Continuity from the GPIO14 wire to target pin 1 (RESET)** — and check
   it is *not* also on pin 3/4. Finding 19 says C1 is on the GPIO14 net; C1
   is meant to be on VDD-VSS, so either the GPIO14 wire has landed on a
   power pin, or C1 has been fitted across the wrong pair.
4. **Which two pins R1 actually bridges.** The schematic says BKGD->VDD. If
   the VDD feed is open, R1 is what is (badly) powering the chip.
5. **Count the TSSOP pins from the dot again.** Pin 1 = RESET, 2 = BKGD/MS,
   3 = VDD, 4 = VSS.

Once VDD is solid, **re-run the whole investigation from SYNC upward** —
do not carry any number forward from before the fix. In particular
`target_freq_hz` must be re-measured, because the current value is
calibrated against a brownout dip (Finding 18).

### Corrections to earlier sections of this file

- **Finding 3/4's "target BDC clock is 16.3-16.5 MHz" is withdrawn.** That
  number is `128 / (8 us pulse)`, and the 8 us pulse is a VDD dip
  (Finding 18). There is no measurement of this target's BDC clock.
- **Finding 4's "the target emits a burst of ~15-20 SYNC responses" is
  withdrawn.** That burst is the VDD rail oscillating while C1 recharges
  after BKGD is released; it lasts ~700 us because R1 x C1 = 1.0 ms.
- **Finding 10's "the target is extending our '0' bits" is withdrawn.** A
  longer low on BKGD drains more charge off the node and provokes a
  brownout dip; a 0.18 us '1' bit does not. The target's BDC is not
  decoding anything.
- **Finding 8's "holding BKGD low reliably silences the target, so the
  target is alive and reacting to BKGD" is re-read**: holding BKGD low
  removes the chip's only supply path. It is alive, but it is not
  "reacting to BKGD" in any protocol sense.
- **The FLASH-security hypothesis is neither confirmed nor refuted — it is
  untested.** You cannot infer anything about NVOPT/SEC01:SEC00 from a
  part that is not powered. The documented unsecure sequence (FPROT, mass
  erase, blank check) is still the right move *once the target runs*, and
  the datasheet research recorded for it stands; it just has not been
  reached yet.
- **The `flash_init_clock` "DIVLD not set" failure is not FCDIV-specific
  and not security-related** (Finding 15): no BDC register write of any
  kind lands, including BDCSCR, which security explicitly does not
  protect. And **no bus-clock number can be derived from SYNC** — if the
  target were healthy and stock, `BUSCLK` would be 4.19 MHz (FEI default:
  ~16.78 MHz DCO / BDIV 2 = 8.39 MHz ICSOUT, / 2 = 4.19 MHz), which is
  what `flash_init_clock` should eventually be given — **not** the
  ~16.3 MHz that SYNC reports, which is not a bus clock under any reading.

## THIRD live session (2026-09-13) — after the wiring correction

**Wiring is now FINAL and confirmed by the user; no further hardware
changes will be made.** R1 = 10k BKGD->VDD. C1 = 0.1 uF VDD->VSS (it was
previously mis-fitted onto the RESET net — that is the bug the second
session's Findings 18-21 were diagnosing). RESET->GPIO14, BKGD->GPIO15,
VDD->Pico 3V3(OUT), VSS->Pico GND. Nothing else. No RC filter on RESET,
no bulk cap, no series resistors. Everything from here is software only.

Electrical confirmation that the wiring fix took effect (measured before
this session started, recorded here for completeness):
`rise_time` hold=1 ms, 6 trials -> BKGD [56,34,34,35,33,34] us,
RESET [55,37,35,34,34,34] us. RESET used to be 1.2-1.6 ms (the misplaced
C1). Both are now at the MicroPython polling loop's own floor (~30 us), so
the extra capacitance on the RESET net is gone.

### Finding 22 — BKGD and RESET are now fully decoupled, and BKGD is dead quiet at idle

`probe_pin`, 50 ms windows, target idle, no BDC traffic:

```
  BKGD  GPIO15  low= 0.00%  transitions=0     (3/3 windows)
  RESET GPIO14  low=24.3-25.0%  transitions ~1000 per 50 ms
```

And unchanged by `hw_reset_to_bdm` (3 attempts) or `reset_target`: RESET
stays at 24.3-25.0% low and BKGD stays at exactly 0.00% low / 0
transitions. So **BKGD is never driven low by anything except us**, which
makes any low we see on BKGD during a transaction unambiguous.

The two-pin capture (781 ns/sample) gives RESET's real shape — `probe_pin`
undersamples it badly:

```
  RESET: ~8.6 us LOW, ~30.5 us HIGH, period ~39 us (~26 kHz), 22% low
```

That is the classic blank-FLASH reset loop: reset vector 0xFFFF fetches
0xFF, illegal-opcode reset, repeat. RESET low ~8.6 us is consistent with
the S08 driving RESET for 34 bus cycles at the stock 4.19 MHz FEI bus
clock (34/4.19e6 = 8.1 us). **This is now believed to be genuine, normal
target behaviour for an erased part, not a fault** — the second session's
"GPIO14 is a power net" reading (Finding 20) was an artifact of C1 being
on that net, and it is withdrawn.

### Finding 23 — the target DOES answer SYNC, but only ~20% of the time, and the answer is usually truncated

This is the first real evidence in the whole project that the target's BDC
is doing something protocol-shaped. Method: `capture2` with
`drive="sync"`, `host_low_us=20`, **`sample_count=64`** (exactly 4 RX FIFO
words, so all 64 samples are contiguous — see the caveat below), 1 us per
sample. Host releases BKGD at t=20 us.

```
     w=.      B ____________________-------------------------------------------
              R ------_________-------------------------------________---------
     w=7.52   B ____________________-________----------------------------------
              R -----------------------------_________------------------------
     w=5.52   B ____________________-______-----------------------------------
              R ---------------------------________---------------------------
     w=2.96   B ____________________-___--------------------------------------
              R ------------------------_________-----------------------------
```

(`w` = the width the `bdc_sync` PIO program itself measured, independently
of the capture. They agree.)

Two hard facts:

1. **When the response is full width it is 7.52-7.68 us, repeatably.**
   128 BDC cycles / 7.52 us = **17.0 MHz**. The MC9S08SG8's FEI-default
   DCO output is ~16.78 MHz, and BDCSCR.CLKSW resets to 0 = "alternate BDC
   clock" = that DCO output. **7.52 us is exactly the SYNC response a
   stock, untrimmed SG8 should give.** This is the first number in this
   project that matches a datasheet prediction it was not fitted to.
2. **The response is cut short exactly when the target resets.** In every
   truncated run, BKGD's low ends on the same sample that RESET goes low.
   The target begins its 128-cycle answer, then its own illegal-opcode
   reset aborts it mid-pulse.

Hit rate and width distribution, `bdc_sync`'s own measurement, 20 trials
per row (`.` = no falling edge at all within the 10 ms budget):

```
host_low_us | hits | widths us
      8     | 0/20 | (too short: 8 us < 128 cycles at 17 MHz = 7.5 us + margin)
     10     | 6/20 | 2.08 6.40 6.08 5.12 1.20 2.24
     16     | 2/20 | 6.00 4.56
     20     | 5/20 | 0.48 5.12 7.68 0.08 1.76
     40     | 4/20 | 0.16 2.48 0.88 3.84
     80     | 4/20 | 7.52 3.04 2.56 6.24
    200     | 3/20 | 7.52 7.52 4.24
    500     | 3/20 | 3.44 2.96 7.60
   1000     | 5/20 | 4.32 3.60 6.96 7.52 0.80
   2000     | 4/20 | 7.60 3.68 5.20 1.52
```

Host-low duration makes no difference above ~10 us, which is what the
spec says (>= 128 target cycles = 7.5 us is all that is required). The
widths are scattered from 0.08 us up to a hard ceiling of 7.68 us — a
truncation distribution, not a noise distribution.

Correlating each trial against RESET's phase (24 trials, 1 us/sample
two-pin capture): **a response appears only when the SYNC release lands
roughly 20-30 us after the target's last RESET rising edge**, i.e. in the
last third of the ~32 us window during which the target is running.
Releases 0-16 us after a RESET rise produce no response at all.

```
  responded:  time since RESET rise = 22 24 24 26 27 28 28 29 us
  silent:     time since RESET rise =  0  1  3  4  6  8 10 10 11 11 12 14 15 15 16 us
```

That phase dependence is not yet explained and is the main open question.
It is NOT "the reset truncates the answer" — that only explains the width,
not why an answer issued early in the run window is missing entirely.

**Methodology note that matters for anyone repeating this:** `capture2`
packs 16 samples per 32-bit word into a 4-word RX FIFO = **64 contiguous
samples maximum**. Ask for 128 and the PIO stalls on autopush at sample 64
and resumes only when the Python drain loop starts — which, on a SYNC
timeout, is 10 ms later. The result is a perfectly repeatable-looking
`L20.31 H41.39 L38.27` tail that is pure artifact. Earlier drafts of this
finding nearly recorded that tail as real. Single-pin `capture` packs 32
samples/word, so its limit is 128.

### Finding 24 — `capture2` silently cancels a `drive_pin(14, ...)` hold

`make_capture2_state_machine(base)` calls `Pin(base, Pin.IN)` and
`Pin(base+1, Pin.IN)`, and `base = min(bkgd, reset) = 14`. So every
`capture2` call re-initialises GPIO14 as a plain input, releasing any hold
`drive_pin` had put on it. A "sync with RESET held low, observed on
capture2" experiment therefore measures nothing of the sort. Plain `sync`
and single-pin `capture` only touch GPIO15 and are safe to combine with a
`drive_pin(14, 0)` hold.

### Finding 25 — the RESET train is a perfectly regular 40.0 us square wave, and NOTHING we do to BKGD changes it

Passive two-pin capture, trigger on RESET falling, 5 us/sample, 512
samples = 2.56 ms continuous (this rate is slow enough that the Python
drain loop keeps up, so unlike a fast capture the whole 2.56 ms is real):

```
  L10 H30 L10 H30 L10 H30 L5 H35 L5 H30 L10 H30 L10 H30 L10 H30 ...
  -> period exactly 40.0 us, low 8-10 us, for the whole 2.56 ms window.
     BKGD: 0.00% low, 0 transitions, across the entire capture.
```

Rock-steady 25 kHz. And a matrix of BDM-entry attempts (each followed by
four 40 ms `probe_pin` windows on RESET) moves it by nothing at all —
every single row is 24.2-24.8% low:

```
  BKGD Hi-Z + reset pulse                     24.67 24.62 24.26 24.81
  BKGD held LOW across reset, KEPT low        24.58 24.44 24.78 24.59
  ... then BKGD released                      24.40 24.46 24.77 24.62
  BKGD driven HIGH across reset (control)     24.41 24.39 24.61 24.54
  BKGD held LOW, no reset at all              24.75 24.52 24.46 24.49
  firmware hw_reset_to_bdm                    24.50 24.37 24.22 24.69
  3 reset pulses, BKGD held LOW throughout    24.41 24.31 24.65 24.54
```

Two things follow:

- **CONTEXT.md Finding 8's "holding BKGD low reliably silences the target"
  is now definitively dead.** With the wiring corrected, holding BKGD low
  does nothing whatsoever to the target. That old observation really was
  the parasitic-power artifact the second session diagnosed.
- **The hardware BDM-entry method does not latch, and the failure is not a
  timing detail of `hardware_reset_to_bdm`.** BKGD held low *continuously*,
  across many self-generated resets, still never halts the target. There is
  no timing window to miss in that configuration — every reset rising edge
  for tens of milliseconds has BKGD low — so the mode latch is either not
  seeing our low, or it is seeing it and refusing.

`hardware_reset_to_bdm` now takes `pre_low_ms` / `reset_ms` /
`bkgd_hold_us` / `release_bkgd` so entry timing can be swept from the host;
sweeping it is not the next thing to try, given the above.

### Finding 26 — anomalous: an internal pull-DOWN on GPIO14 makes the RESET train vanish

```
  GPIO14 pull=None  low=24.83%  transitions=800
  GPIO14 pull=up    low=24.73%  transitions=790
  GPIO14 pull=down  low= 0.00%  transitions=1     <-- reads HIGH, always
  GPIO15 pull=None/up/down   low=0.00%, 0 transitions (all three)
```

This is electrically backwards and is NOT explained. Adding a ~60 k
pull-down to a node that something is actively driving low 25% of the time
can only ever make it read low *more* often. Reading high 100% of the time
with a pull-down engaged, while reading low 25% of the time with no pull,
has no simple open-drain model. (The same result appears in the 2026-09-12
Finding 1 table, where it was not commented on.) Flagged, not resolved.
Whatever is on GPIO14 is not a plain open-drain RESET with a pull-up.

### Finding 27 — REAL FIRMWARE BUG: multi-byte commands were NOT contiguous (6-9 us holes between bytes)

`bdc.py` did `sm.active(1)` **first** and then one `sm.put()` per byte.
A MicroPython `put()` costs several microseconds and a byte is only 7.6 us
of signal at 16.9 MHz, so the state machine drained byte 1 and re-stalled
on `pull` before byte 2 arrived. Measured with the scope SM at
132 ns/sample, `tx=[0xE4, 0x00]`:

```
  before: byte1 at t=0-7.5 us, byte2 not until t=13-16 us  (hole moved run to run)
  after : 16 bits end at t=15.18 us = exactly 2 x 7.59 us, contiguous
          tx=[0x00,0x00,0x00,0x00] -> 32 bits end at 30.74 us = 4 x 7.68 us
```

CONTEXT.md and `bdc_pio.py`'s own docstring both claimed "up to 4 bytes
leave the Pico as one uninterrupted bit stream". **They did not.** Fixed by
filling the TX FIFO *before* `active(1)` (with a `restart()` first so a
previous mid-program deactivation cannot leave stale PC/OSR state). This
matters more here than it would anywhere else: the target's blank-FLASH
reset loop leaves only a ~30 us window, and a 3-byte `READ_BYTE` was
spending ~20 us of that on holes.

### Finding 28 — a zero-turnaround read: send the command and the read-phase markers as ONE tx burst

`bdc_tx_byte` sending `0xFF` emits exactly the drive pattern of a BDC
read-bit marker (low ~3 BDC cycles, release for 13), and the target's
answer for a '0' bit is to hold the line low to ~cycle 10. So
`tx=[opcode..., 0xFF]` **is** a complete read transaction, with the read
phase clocked out of the same FIFO load as the command — no
`StateMachine.active()` call, and therefore no Python, anywhere in the
middle. Sampling it with the scope SM and decoding offline replaces
`raw_xfer`'s 15-18 us turnaround.

This matters because `raw_xfer`'s own numbers made a READ_STATUS
7.6 (tx) + 16 (turnaround) + 7.6 (rx) = ~31 us, i.e. *longer than the
target's entire 30 us run window between resets*. The filler trick makes
the same transaction 15.2 us, which fits with room to spare.

`{"cmd":"capture","test":"xfer","tx":[228,255],"nbits":0,
  "sample_count":128,"window_us":17.1}` — 7.08 samples/bit at 16.9 MHz.
Decode rule: count low samples per bit; our own '1' marker is 3/16 of a
bit (~1.3 samples), our '0' is 13/16 (~5.8), a target '0' answer is ~10/16
(~4.4). Threshold at 4.

Result, `READ_STATUS` as `[0xE4, 0xFF]`, 16 repeats at 16.9 MHz: the
command byte decodes back correctly (0xE4, with occasional single-sample
quantisation slips), and **every bit of the read phase shows a low count of
2-3 samples — identical to the all-`0xFF` control that contains no command
at all.** No bit anywhere in the read phase ever reaches 4. So with a
genuinely gapless 15.2 us transaction at the SYNC-derived bit rate, the
target still does not answer READ_STATUS.

### Finding 29 — ***THE TARGET IS ALIVE AND ANSWERING***: a phase-locked SYNC gives a clean, repeatable, full-width 128-cycle response

This is the finding the whole project has been waiting for, and it is the
first one that is reproducible on demand rather than 20% of the time.

**Method.** Stop firing at a random point in the target's reset cycle. New
firmware primitive `after_reset_sync(delay_us, host_low_us)`
(`{"cmd":"after_reset_sync","delay_us":N,"host_low_us":M}`): pre-build the
SYNC state machine and **pre-load all three of its FIFO words**, then
assert RESET for 60 us, release it, busy-wait `delay_us` on `ticks_us`, and
only then `active(1)` — so the only thing between the delay expiring and
BKGD going low is one `active(1)`. Our reset release is now the time
origin and the target's whole 40 us cycle is locked to us.

Sweep, `delay_us` 0..200 in 2 us steps, 8 trials each, `host_low_us=20`.
`F` = a full-width response (>= 6.5 us), `+` = truncated, `.` = silent:

```
   d=  0        ........
   d=  2..50    +.+.+.++  +.+.++..  ... (60-85% hits, all truncated, median 2-4 us)
   d= 52..74    ........  ........  ........  <- DEAD, 12 rows, 96 trials, zero
   d= 76        ......F.   7.60
   d= 78        ......F.   7.52
   d= 80        ..+FF.F.   7.52
   d= 82        ++F+F+F.
   d= 84..90    ++++++.+  (hits, widths decaying)
   d= 92        +.....+.
   d= 94..114   ........  ........  ... <- DEAD, 11 rows, 88 trials, zero
   d=118        ....F.F.   7.52
   d=120        FF.+F.+F   7.52
   d=122        FFFFFF+.   7.52   <-- 7/8 hits, 6/8 FULL WIDTH
   d=124        .FFFF.+F   7.52
   d=126        F+++FF.F   7.52
   d=128        FF..++.F   7.04
   d=130..134   truncated only
   d=136..150   ........  <- DEAD
   d=152..158   ...F....   7.52  (x4 rows, every hit full width)
   d=160..164   .FFFF F+  7.20-7.52   d=164: 7/8 hits, 5/8 full width
   d=166..172   truncated only
   d=174..192   ........  <- DEAD
   d=194..198   F.F.....  7.60 / 7.52
```

**Read what this says:**

1. **The structure is periodic with period ~40-42 us — the target's reset
   loop period, measured independently as exactly 40.0 us (Finding 25).**
   Live windows open at d = 76, 118, ~154, 194.
2. **At the START of each live window the response is a clean 7.52-7.60 us
   pulse**, and it degrades to truncated stubs later in the window. 128 BDC
   cycles / 7.52 us = **17.0 MHz**; the MC9S08SG8's FEI-default DCO is
   ~16.78 MHz and BDCSCR.CLKSW resets to 0 = "alternate BDC clock" = that
   DCO output. The number was predicted by the datasheet, not fitted to it.
3. `d=122` gives **7/8 responses, 6/8 of them full width**, repeatably.
   `d=164` gives 7/8 and 5/8. This is a *deterministic, on-demand* BDC
   response — not the ~20% coin flip every earlier measurement in this
   project produced.

**So: the target's BDC is powered, clocked at ~17 MHz, listening to BKGD,
and answering the SYNC handshake correctly.** Every "no response" and
every truncated stub recorded before this was the request or the answer
landing on the wrong side of the target's own 40 us reset loop. There was
never anything wrong with the SYNC handshake itself.

The ~50 us of "hits but always truncated" right after our own reset
(d = 2..50) is the target settling; the periodic structure only
establishes itself after that.

**Practical rule for anything that follows: fire the stimulus at
`delay_us` ~= 122 after a RESET release** (or 122 + 42k). Do not fire at a
random time.

### Finding 30 — REAL FIRMWARE BUG: `bdc_tx_byte` had no speed-up pulse, and the pull-up RC was eating the bit boundary

Measured on the real board at **27 ns/sample** (the scope SM's finest
useful rate), bit clock 16.9 MHz so one BDC cycle = 59.2 ns:

```
  BEFORE (release to Hi-Z, let R1 do the rising edge):
     '1' bit LOW          297-324 ns   (spec: 3 cycles = 178 ns)
     '0' bit LOW          854-915 ns   (spec: 13 cycles = 770 ns)
     high between two consecutive '0' bits:  **61 ns**  (spec: 178 ns)
```

Every rising edge carried ~130 ns of RC tail — 2.2 BDC cycles at this
clock, with R1 = 10k and ~30 pF of stray. For back-to-back '0' bits that
leaves the line barely above the RP2040's own Schmitt threshold for one
sample, and at the target's higher VIH (0.7 x VDD) plausibly never high at
all: **the target would see one continuous low instead of a framed bit
stream.** That is precisely the observed symptom set — SYNC works (it only
needs a long low and one rising edge, and `bdc_sync` ALREADY had a
speed-up pulse), and no command has ever executed.

HCS08RMv1's host-to-target bit waveform has the host drive BKGD **high**
for a moment before releasing. `bdc_tx_byte` never did. It now does, using
a side-set bit mapped onto the same GPIO — side-set carries the output
value while `set(pindirs, ...)` carries the direction, so it costs one
extra instruction per path (12 -> 14) instead of three.

```
  AFTER:
     '1' bit LOW          162-189 ns   (spec 178)   EXACT
     '0' bit LOW          732-793 ns   (spec 770)   EXACT
     high between '0's    183 ns       (spec 178)   EXACT
```

`make_state_machine` now also passes `sideset_base`. PIO block 1 is at
**31/32 instructions** — one slot left; see the table in `bdc_pio.py`.

### Finding 31 — ***8/8 FULL-WIDTH SYNC RESPONSES ON DEMAND***

`after_reset_sync`'s delay was a `ticks_us` Python busy-loop that
overshot by ~40 us (measured). Replacing it with `time.sleep_us` (which
busy-waits in C, accurate to ~1 us) turned the fuzzy periodic structure of
Finding 29 into a clean one. Sweep, 8 trials per row, `host_low_us=20`;
`F` = full width (>= 6.5 us), `+` = truncated, `.` = silent:

```
  d=  0..20   ........   (6 rows, 48 trials, silent)
  d= 24       FFFF.F+F   median 7.52 us
  d= 28       ++++++++   median 4.08
  d= 32       ++++++++   median 2.08
  d= 36..60   ........   (7 rows, 56 trials, silent)
  d= 64       FFFFFFFF   median 7.60 us   <-- 8/8, ALL FULL WIDTH
  d= 68       +++++.++   median 4.16
  d= 72       ..+...+.   median 2.80
  d= 76..96   ........   (6 rows, silent)
  d=100       F......F   7.60
  d=104       FF+F+FF+   7.28
  d=108..136  degrading, then silent
```

Period 24 -> 64 -> 104 = **exactly 40 us**, the reset-loop period. At
**delay_us = 64 the target answered 8/8 with a clean 7.60 us pulse** =
128 BDC cycles at **16.84 MHz**.

**Honesty caveat, measured later the same session:** that 8/8 was a good
run, not a guarantee. Re-checked after further firmware edits, the same
`delay_us = 64` gives 4/8 to 8/8 full-width, with the live window still
firmly at d = 62-74 and the dead zone at 52-60 and 76-94. The *structure*
is rock solid and reproducible; the hit rate inside the window is not,
and the absolute best delay shifts by a few microseconds between builds
because it includes MicroPython's own call overhead. **That is exactly why
`sync_locked()` scans and takes the maximum instead of trusting one
delay** — and why it gets 12/12 where a fixed delay gets ~50-100%.

**This is the project's first reliable, repeatable, on-demand BDC
communication.** The target is a live S08 with a working BDC clocked at
~16.84 MHz (the FEI-default DCO, as BDCSCR.CLKSW=0 selects). Everything
before this that looked like "the target is dead" was a combination of
(a) firing at a random point in its 40 us reset loop and (b) the missing
speed-up pulse.

The live window is narrow — full width only for d within ~2 us of 64,
degrading over the following ~8 us, then 24 us of nothing. Both edges are
explained: the response is truncated by the target's next reset, and a
request issued too soon after a reset is ignored entirely.

### Finding 32 — commands still do not execute, with every known defect fixed

With all of the above in place — exact bit shapes, correct speed-up,
contiguous multi-byte tx, phase locking, and the zero-turnaround
`[opcode, 0xFF]` read trick — `READ_STATUS` was swept across a **full
reset period and beyond, delay_us 0..207 in 3 us steps, 4 trials each**
(280 transactions). The per-bit low-sample count of the 8 read bits never
once exceeded 2, which is exactly what the all-`0xFF` control with no
command in it produces. A real `READ_STATUS` answer would be BDCSCR's
reset value 0x00 = eight '0' bits = the target holding the line low for
~10 of 16 cycles in every one of the eight bit times, i.e. ~4-5 low
samples per bit. Nothing remotely like that appears anywhere.

The command byte itself is confirmed correct on the wire in the same
captures: `0xE4` decodes as `L3 H13 L3 H13 L3 H13 L13 H3 L13 H3 L3 H13
L13 H3 L13` = 1110 0100, MSB first, with textbook 3- and 13-cycle lows.

Also fixed while chasing this: the tx->rx turnaround. `raw_xfer` used
`StateMachine.active()` (~10 us) wrapped in a `ticks_us` loop (~40 us),
putting the read phase ~55 us after the command byte — more than a whole
reset period later. `after_reset_xfer` now starts each state machine by
writing SM_ENABLE straight into PIO1's CTRL register through the RP2040's
atomic-set alias (`0x50300000 + 0x2000`), which is a single store.

So the open question is no longer "is the target alive" or "is the host
waveform right" — both are answered. It is: **why does a part whose BDC
answers SYNC perfectly refuse every command?** The leading candidate is
still FLASH security (a blank NVOPT leaves SEC01:SEC00 = 1:1, and on this
family only 1:0 reads as unsecured), which would leave SYNC working while
blocking command execution and active-BDM entry — exactly the symptom set.
That needs checking against the real MC9S08SG8 §4.5 security table before
anyone acts on it; it is still stated from memory here.

### Finding 33 — GPIO14 really is RESET, proved functionally, not by inspection

`_prime_reset()` drives GPIO14 low for 60 us and releases it, and
everything the target does afterwards becomes **phase-locked to that
release**: the SYNC live windows land at fixed delays (24 / 64 / 104 us)
and repeat at exactly the reset-loop period. A pin that was not the
target's RESET could not re-synchronise the target's internal cycle.
Combined with the SYNC response proving GPIO15 is BKGD, **the wiring and
the pinout assumption are now confirmed by function, not by continuity.**

Two-pin capture at the sweet spot, 1 us/sample, t=0 is our SYNC low
starting, 11 of 12 consecutive runs identical:

```
  B ____________________-________-----------------------------------
  R -----------------------------________---------------------------
     |<--- our 20us low --->|      |<- target's own RESET, 8.6us ->|
                            ^ release
                             ^^^^^^^^ target's 128-cycle SYNC answer, 7.52us
```

Note what this settles: RESET stays HIGH for the whole request, the
answer completes cleanly, and the target's next reset follows *after* it.
The request was never being clipped at this phase.

### Finding 34 — host-side explanations are now exhausted

Everything below was swept against the corrected waveform with phase
locking, and none of it produced a single target-driven bit:

| Swept | Range | Result |
|---|---|---|
| Reset-loop phase, `READ_STATUS` | delay 0-207 us, 3 us steps, 4 trials (280 transactions) | nothing |
| Reset-loop phase, `READ_STATUS` | delay 50-93 us, **1 us steps**, 5 trials (220) | nothing |
| **Every opcode 0x00-0xFF** | at delays 58/64/70 us (768 transactions) | **0 hits** |
| Assumed BDC clock | 8, 10, 12, 14, 15, 16, 16.5, 16.78, 16.84, 17, 17.5, 18, 20, 24 MHz, each across delays 44-88 us | nothing |
| `BACKGROUND`, and `WRITE_CONTROL 0x80/0xC0/0x88` + `BACKGROUND` as one 3-byte 22.8 us burst, fired at every phase 40-84 us, 3 passes each | RESET activity before/after | 24.4-24.8% throughout, i.e. the target never halts |

The all-256-opcode sweep is the important one: it simultaneously rules out
a wrong opcode value, a wrong bit order (LSB-first 0xE4 is 0x27, which is
in the set) and a wrong polarity (the complement is in the set too). The
clock sweep rules out a wrong bit period. The phase sweeps rule out timing
against the reset loop. The captures rule out bit shape.

**So the question is now squarely about the target, not the programmer:
why does a part whose BDC answers SYNC perfectly, every time, refuse every
command and refuse to halt?**

Leading candidate, still UNVERIFIED against primary sources and stated
from memory: **the part is secured.** An erased HCS08's NVOPT reads 0xFF,
giving SEC01:SEC00 = 1:1, and on this family only 1:0 reads as unsecured —
so a blank part is secured by default. A secured part answering SYNC while
refusing commands and refusing active-BDM entry is exactly this symptom
set. **Before acting on this, someone with the MC9S08SG8 datasheet needs
to check §4.5/§4.6's security table AND what the BDC is documented to
still accept while secure** — because if a secured part accepts nothing
but SYNC, then the usual "enter BDM, write FCDIV, mass erase" unsecure
recipe cannot work either, and the recovery path has to be something else.

### What was changed in the firmware this session

All deployed to the Pico and regression-tested: **39 serial commands
exercised end to end against the real Pico and real target, no hang, no
crash, every error path still returning a clean JSON error.**

1. **`bdc_tx_byte` gained the speed-up pulse** (Finding 30) — a side-set
   bit on the same GPIO drives BKGD high for one cycle before release.
   12 -> 14 instructions; `make_state_machine` now passes `sideset_base`.
   PIO block 1 is at 31/32.
2. **TX FIFO is filled before `active(1)`** (Finding 27), with a
   `restart()` first. Multi-byte commands are now genuinely contiguous.
3. **`bdc_sample2`'s trigger is runtime-selectable** — the hard-coded
   `wait(0, pin, 1)` became a `jmp_pin` arm loop (same instruction count),
   so a capture can trigger on RESET instead of BKGD.
4. **`make_capture2_state_machine` no longer re-inits the pads**
   (Finding 24) — `Pin(n)` without a mode, since PIO inputs are not gated
   by GPIO function select. A `drive_pin()` hold now survives a capture.
5. **New `after_reset_sync(delay_us, host_low_us)`** and
   **`after_reset_xfer(delay_us, tx, nbits, ...)`**: drive the reset, wait
   an exact `sleep_us` delay, then fire — with the state machine built and
   its FIFO preloaded beforehand, and started by writing SM_ENABLE
   straight into PIO1's CTRL atomic-set alias (`0x50300000 + 0x2000`)
   rather than through `StateMachine.active()` (~10 us) wrapped in a
   `ticks_us` Python loop (~40 us).
6. **New `sync_locked()`** (`{"cmd":"sync_locked"}`) — scans the reset-loop
   phase and **takes the MAXIMUM pulse width seen**, because truncation can
   only shorten a response and nothing can lengthen it past 128 cycles.
   This is the reliable SYNC. `sync()`'s docstring now warns that it
   over-reports frequency against a free-running target.
7. `hardware_reset_to_bdm` gained `pre_low_ms` / `reset_ms` /
   `bkgd_hold_us` / `release_bkgd`; `probe_pin` gained `pull`; `capture2`
   gained `trigger` and the `none` / `reset` / `bdm` / `areset_sync` drive
   modes.

**`sync_locked()` repeatability, 12 consecutive calls, 102 probes each:**

```
  16842106 Hz  pulse 7.60us      16842106 Hz  pulse 7.60us
  16842106 Hz  pulse 7.60us      16494845 Hz  pulse 7.76us
  16666667 Hz  pulse 7.68us      16842106 Hz  pulse 7.60us
  16842106 Hz  pulse 7.60us      16842106 Hz  pulse 7.60us
  16842106 Hz  pulse 7.60us      17021276 Hz  pulse 7.52us
  16842106 Hz  pulse 7.60us      16842106 Hz  pulse 7.60us
```

12/12 succeed; 9/12 land on exactly 16.842 MHz; the spread is one 25 MHz
probe-clock count either way. Mean 16.79 MHz against a datasheet-predicted
16.78 MHz FEI DCO. **This is the first number in this project that is both
repeatable and independently predicted.**

### Suggested next steps

1. **Get the MC9S08SG8 datasheet §4.5/§4.6 and HCS08RMv1 §7 in front of
   someone and settle the security question**, including what the BDC
   accepts while secure. Every remaining hypothesis is downstream of it.
   Do not spend more time sweeping host-side parameters; that space is
   exhausted (Finding 34).
2. If a secured part does accept the unsecure sequence, it will need the
   phase-locked path (`after_reset_xfer`) for every step, because each
   transaction has to fit in the ~30 us between the target's own resets.
   Budget: 7.6 us per byte at 16.84 MHz, so at most 3 bytes plus a read
   phase per window.
3. The 40 us reset loop is a consequence of blank FLASH, so it disappears
   the moment anything is programmed — but it cannot be worked around from
   the Pico side: holding RESET low kills the BDC too (0/48 SYNC responses
   with RESET asserted, using plain `sync` which does not touch GPIO14).
4. Unexplained and worth a second look if it ever matters: an internal
   pull-DOWN on GPIO14 makes the RESET train read as a solid high
   (Finding 26). It is electrically backwards and nobody has explained it.
5. Hardware note, recorded as a recommendation only — **the user has
   declined further hardware changes and that is respected**: R1 at 10k
   against ~30 pF gives a ~130 ns RC tail, which is 2.2 BDC cycles at
   16.8 MHz. The speed-up pulse now hides this completely for host-driven
   edges, but the *target's* rising edges are still RC-limited, and a
   2.2k-4.7k pull-up would give much more margin if this is ever revisited.

## Everything already tested (host side, hardware-independent)

- Flask app boots and all routes respond correctly (`/`, `/api/ports`,
  `/api/status`, `/api/capture` error path, etc.) — exercised via
  `app.test_client()`.
- `srec.py`'s S-record parser + contiguous-chunk merger verified against a
  hand-built sample.
- All Python files pass `py_compile`; `app.js` passes `node --check`.

## FOURTH live session (2026-09-13, later) — measuring the target's RECEPTIVE window

Motivation: everything before this assumed the target's usable window is the
whole ~31 us it spends out of reset, and concluded from "no command ever
works at any phase" that the part must be secured. Nobody had measured
whether a multi-byte command can *physically fit* in the window at all.
It cannot. That is Finding 35.

Hardware and wiring unchanged and FINAL (R1 10k BKGD->VDD, C1 0.1 uF
VDD-VSS, RESET->GPIO14, BKGD->GPIO15, VDD->3V3(OUT), VSS->GND).
Session opened with `sync_locked` -> 16 842 106 Hz, pulse 7.60 us,
best_delay 66 us, 38 hits / 153 probes. Baseline reproduced exactly.

### Finding 35 — the target's BDC is receptive for only ~13 us of its 31.4 us alive window, and receptivity is keyed to the RISING edge

**Method.** `after_reset_sync(delay_us, host_low_us)` sweeps the phase of a
SYNC handshake against the target's own reset loop. Sweeping *only*
`delay_us` (as every earlier session did) cannot separate "when the host's
low STARTS" from "when the host RELEASES it", because with a fixed
`host_low_us` the two move together. So the sweep was run three times with
**different low durations** — 10, 20 and 40 us — delay 0..130 in 2 us steps,
6 trials per point (1188 phase-locked SYNC attempts total).

Live windows, expressed as the **delay** (what earlier sessions reported)
and as the **release time** `delay + host_low`:

```
  host_low=10  live delays  34-44    70-84     110-122
  host_low=20  live delays  24-34    62-74     100-112
  host_low=40  live delays   2-14    42-54      82-92     120-130

  host_low=10  live RELEASE  44-54    80-94     120-132
  host_low=20  live RELEASE  44-54    82-94     120-132
  host_low=40  live RELEASE  42-54    82-94     122-134    160-170
```

**The delay windows move with `host_low`; the release windows do not.**
All three low durations give the identical set of live release times,
period exactly 40.0 us. So:

- **Receptivity is a property of the host's RISING edge (the release), not
  of when the low began.** The BDC's SYNC detector is edge-triggered on the
  release, exactly as HCS08RMv1 describes, and the >=128-cycle low can begin
  anywhere — including *before* the target's reset. With `host_low=40,
  delay=4` the low is asserted straight through the target's entire 8.6 us
  RESET pulse and still gets a clean full-width 7.60 us answer. A BKGD low
  spanning the target's reset is not a problem.
- Full-width (7.52-7.68 us, i.e. untruncated 128 cycles) answers occur only
  in the **first ~4-6 us** of each live window; after that the answers decay
  smoothly to stubs and then vanish, which is the next reset cutting them
  short.

### Finding 36 — where that window sits: the last 13 us before the next reset

Located with `capture2 drive="areset_sync"` (BKGD + RESET sampled together,
1 us/sample, 64 contiguous samples, triggered on our own BKGD falling edge
so t=0 is the start of our SYNC low). Note `capture2`'s phase origin is
offset from `after_reset_sync`'s by a constant ~-20 us of extra setup, so
its own sweet spot was re-found by sweeping (live at delay 10-22 and 50-62
with `host_low=10`) rather than assuming.

Every full-width hit looks like this, 9/9 identical:

```
  B __________-________---------------------------------------------
  R -------------------________-------------------------------______
  |<-our 10us low->|
             ^ release (speed-up pulse, 1 sample high)
              ^^^^^^^^ target's 128-cycle answer, 7.5 us
                     ^^^^^^^^ target's own RESET, 8.6 us low
                                                  period 39-40 us
```

The target's answer ends and its RESET falls within one sample of each
other. Combining that with the truncation edge of the Finding 35 sweeps
(answers collapse to <1 us at release = 54, so the reset falls at ~54.5 in
that frame) gives the full picture on one axis:

```
  target RESET low   |<-- 8.6 us -->|
  target alive       .................|<---------- 31.4 us ---------->|
  BDC receptive      .............................|<-- ~12.9 us -->|
  full-width answer  .............................|<-5us->|
                     ^rise                        ^rise+18.5     ^rise+31.4
```

**The BDC ignores BKGD for the first ~18.5 us after the target comes out of
reset, and is receptive only for the last ~12.9 us.** (18.5 us is ~311 BDC
clocks at 16.84 MHz, ~78 bus cycles at the 4.21 MHz FEI bus clock. The
reason is not established; it is presumably internal reset recovery that
outlasts the RESET pin's own low.) This is measured twice, with two
independent instruments (`bdc_sync`'s own PIO counter and the two-pin
scope), and the two agree.

### Finding 37 — THE STRUCTURAL CONSEQUENCE: only a ONE-BYTE command can fit

At the measured 16.84 MHz BDC clock, one BDC bit is 16 cycles = **0.95 us**,
so one byte is **7.6 us**. Against a **12.9 us** receptive window:

| Transaction | Bytes on the wire | Time | Fits in 12.9 us? |
|---|---|---|---|
| SYNC | (not a command) | 7.6 us answer | yes, with ~5 us to spare |
| any 1-byte command (`BACKGROUND`, `ACK_ENABLE`, `ACK_DISABLE`, `GO`, `TRACE1`) | 1 | 7.6 | **yes** (5.3 us left for an ACK) |
| `READ_STATUS` + read phase (the `[0xE4,0xFF]` filler trick) | 2 | 15.2 | **NO** |
| `WRITE_CONTROL` (set ENBDM) | 2 | 15.2 | **NO** |
| `WRITE_CONTROL`+`BACKGROUND` burst | 3 | 22.8 | **NO** |
| `READ_BYTE` + address + read phase | 4 | 30.4 | **NO** |

So **every single command this project has ever swept was structurally too
long to be received**, and the exhaustive sweeps of Finding 34 were
measuring a window that could never have worked:

- the 256-opcode sweep sent `[opcode, 0xFF]` = 15.2 us;
- the `READ_STATUS` phase sweeps sent 15.2 us;
- the `WRITE_CONTROL`+`BACKGROUND` burst sent 22.8 us.

That reframes Finding 34 completely. Its conclusion ("host-side explanations
are exhausted, so the part must be secured") **does not follow** — the host
side was never given a transaction that could fit. FLASH security remains
untested, not confirmed.

It also predicts a hard deadlock for the obvious workaround: `BACKGROUND` is
documented as ignored while BDCSCR.ENBDM = 0, and the only way to set ENBDM
is `WRITE_CONTROL`, which is 2 bytes and therefore cannot be delivered. If
the BDC's command state does not survive the target's reset, a blank
free-running part cannot be halted from the BKGD pin at all by this route.

### Finding 38 — new firmware tool: `scan_xfer`, a phase scan that runs ON the Pico

`{"cmd":"scan_xfer","tx":[...],"d_from":..,"d_to":..,"d_step":..,"trials":..}`
fires `tx` as one uninterrupted burst at every phase of the target's reset
loop and reports, per phase, the **longest low run on BKGD after our own
transmission has finished and released the line**. Since BKGD is never
driven low by anything but us (Finding 22), a non-zero number is the target
answering — an ACK pulse, a read bit, anything at all.

It exists because the host-side version of the same loop costs ~30 ms per
trial in serial round-trips; on the Pico it is **~3 ms**. That is the
difference between one opcode per minute and all 256 opcodes at every phase
in 97 seconds. Both state machines are built once outside the loop and the
burst is started with a single store to PIO1's atomic-set CTRL alias.

**It is validated in both directions, which matters more than the tool:**

- *Detector control.* With `skip_us=0` (so our own bits count),
  `tx=[0x00]` reports 0.781 us — the spec '0' low is 13 cycles = 0.772 us —
  and `tx=[0xFF]` reports 0.312 us against a spec '1' low of 3 cycles =
  0.178 us (2 samples at 156 ns; quantisation).
- *End-to-end control — it really can catch a target response.*
  `set_bit_clock(1_500_000)` makes a single '0' bit an 8.67 us low, which is
  a legal SYNC request (>=128 target cycles = 7.6 us). `tx=[0xFE]` puts that
  long low last, so the target's 128-cycle answer lands in the look-ahead
  region. `scan_xfer` reports:

```
  d=  0..13   8 0 8 0 8 8 8 6 7 6 5 5 5 1   <- target answers, 8.125 us at t=84.3
  d= 14..40   0 x 27                        <- dead zone
  d= 41..47   8 8 8 8 8 7 6                 <- answers again, period ~41 us
```

  That is the target's own 40 us reset-loop structure, recovered by the
  scanner from a stimulus made of BDC *bits*, not a SYNC. So a zero from
  this tool is a real zero.

### Finding 39 — EVERY one of the 256 possible single-byte commands, at EVERY phase: the target emits nothing

With the bit clock at the phase-locked `sync_locked` value (16 842 106 Hz)
and the one-byte transaction that Finding 37 says is the only thing that
*can* fit in the receptive window:

```
  256 opcodes x delays 0..43 us in 1 us steps x 3 trials
  = 33 792 phase-locked transactions, 96.8 s
  -> global maximum target-driven low on BKGD: 0 samples. Zero hits.
```

This is the test Finding 34's 256-opcode sweep could not be: that one sent
`[opcode, 0xFF]` = 15.2 us, which Finding 37 shows cannot fit. This one
sends 7.6 us, which does fit, and covers every phase at 1 us resolution with
an instrument proven able to see a target response in the same code path.

The payload that matters most in that set is **`ACK_ENABLE` (0xD5)**: one
byte, needs no ENBDM, touches no memory — so FLASH security cannot block it
— and the HCS08 BDC is documented to answer it with an ACK pulse. It draws
nothing, at any phase.

**Caveat, and it is the one thing standing between this and a conclusion:**
the whole ACK_ENABLE observable rests on the claim that the BDC acknowledges
the `ACK_ENABLE` command *itself*. That is recalled, not verified against
HCS08RMv1. If a target does not ACK its own ACK_ENABLE, then a one-byte
command has no observable at all on this hardware and Finding 39 proves
nothing about whether the command was received. Settling that is the single
highest-value piece of documentation research left.

## FIFTH session (2026-09-14) — settling the ACK_ENABLE question against the primary source

The open question closing Finding 39 was: *does an HCS08 target actually
acknowledge the `ACK_ENABLE` command itself, or is `ACK_ENABLE` a silent
internal state change with no observable?* If the latter, Finding 39's
"zero hits across all 256 one-byte opcodes" proves nothing.

Source used: **HCS08 Family Reference Manual Volume I (HCS08RMv1),
Motorola/Freescale, Revision 1** — the same document CONTEXT.md's Sources
list already cites (https://physics.mcmaster.ca/phys4da3/MCU/MC9S08/
HCS08RMV1%20Reference%20Manual.pdf). Read directly as a PDF, §7.3.4 /
§7.3.5 / Table 7-1. All quotes below are verbatim.

### Finding 40 — CONFIRMED BY PRIMARY SOURCE: `ACK_ENABLE` ($D5) DOES issue an ACK pulse, and the manual says so three separate times

**1. §7.3.4.2 ACK_ENABLE** (RM p.227, PDF page 227):

> "Enables the hardware handshake protocol in the serial communication.
> The hardware handshake is implemented by an acknowledge (ACK) pulse
> issued by the target MCU in response to a host command. The ACK_ENABLE
> command is interpreted and executed in the BDC block **without the need
> to interface with the CPU. However, an acknowledge (ACK) pulse will be
> issued by the target device after this command is executed. This feature
> could be used by the host in order to evaluate if the target supports the
> hardware handshake protocol.** If the target supports the hardware
> handshake protocol the subsequent commands are enabled to execute the
> hardware handshake protocol, otherwise this command is ignored by the
> target."

**2. §7.3.5** (RM p.251):

> "ACK_ENABLE — Enables the hardware handshake protocol. The target will
> issue the ACK pulse when a CPU command is executed by the CPU. **The
> ACK_ENABLE command itself also has the ACK pulse as a response.**"
>
> "ACK_DISABLE — Disables the ACK pulse protocol. ... **This command will
> not be followed by an ACK pulse.**"
>
> "The default state of the protocol, after reset, is hardware handshake
> protocol disabled."

**3. Table 7-1, BDC Command Summary** (RM p.224):

```
  ACK_ENABLE    Non-intrusive   D5/d   Enable handshake. Issues an ACK pulse
                                       after the command is executed.
  ACK_DISABLE   Non-intrusive   D6/d   Disable handshake. This command does
                                       not issue an ACK pulse.
```

So the recalled claim the previous session flagged as UNVERIFIED is
**correct, and it is the single best-chosen probe available**:

- `ACK_ENABLE` is **Non-intrusive** — legal while the target is running
  user code, no active background mode required, no ENBDM required.
- It is **executed inside the BDC block, explicitly without CPU
  involvement** — so a running/resetting CPU, a WAIT/STOP state, and FLASH
  security all have no mechanism to suppress it.
- The manual **names this exact use** ("could be used by the host in order
  to evaluate if the target supports the hardware handshake protocol"),
  i.e. it is the documented one-byte target-alive probe.
- Its opcode `$D5` is confirmed against Table 7-1 (matching `bdc.py`).

### Finding 41 — the ACK pulse's exact shape and timing, from §7.3.5 + Figure 7-6

This is what any detector has to be built to catch, and it is **much
smaller and later than the SYNC response `scan_xfer` was validated
against**:

> "This protocol is implemented by **a low pulse (16 BDC clock cycles)
> followed by a brief speedup pulse** on the BKGD pin, generated by the
> target MCU when a command, issued by the host, has been successfully
> executed. ... This pulse is referred to as the ACK pulse. ... **The ACK
> pulse is not issued earlier than 32 BDC clock cycles after the BDC
> command was issued. The end of the BDC command is assumed to be the
> 16th BDC clock cycle of the last bit.** This minimum delay assures enough
> time for the host to recognize the ACK pulse. **Note also that there is
> no upper limit for the delay between the command and the related ACK
> pulse**, since the command execution depends on the CPU bus frequency..."

Converted to this board's measured numbers (BDC clock 16.842 MHz from
`sync_locked`, so 1 BDC cycle = 59.4 ns, 1 bit = 16 cycles = 0.950 us):

```
  one command byte (8 bits)          = 7.60 us
  minimum ACK delay, 32 BDC cycles   = 1.90 us   (after end of byte)
  ACK low pulse itself, 16 cycles    = 0.95 us
  --------------------------------------------------------------
  earliest possible ACK completion   = 10.45 us after the byte starts
  ... and there is NO documented upper bound on the delay.
```

Against Finding 36's measured **12.9 us receptive window**, an
`ACK_ENABLE` + its ACK only fits if the command byte begins within the
**first ~2.4 us** of that window. That is a slice roughly 2.4/40 = 6% of
the reset cycle — the Finding 39 sweep (delays 0..43 us, 1 us steps,
3 trials) does cover it, but only two or three of its phase points, three
trials each.

**Two things follow, and they cut in opposite directions:**

1. The *observable* is real. Finding 39 is not testing a non-existent
   signal. `ACK_ENABLE` should have answered.
2. The *detector's sensitivity to this particular signal is unproven*.
   `scan_xfer` was validated (Finding 38) against an **8.125 us** SYNC
   response. An ACK pulse is **0.95 us** — 8.5x shorter — and arrives at a
   different, later place in the look-ahead region. Until `scan_xfer` is
   shown to catch a ~1 us low at ~2-10 us after the burst, a zero from it
   is not yet a proven zero *for this signal*.

**Verdict on Finding 39: the ACK_ENABLE premise STANDS (Finding 40), but
Finding 39 is NOT yet a clean negative — it needs the detector re-validated
at ACK scale (Finding 41) before its zero can be read as "the command was
not received".** That re-validation is the immediate next step.

### Finding 42 — `scan_xfer`'s detector IS sensitive at ACK scale (0.95 us), measured — but only if `window_us` <= 60

Finding 38 validated `scan_xfer` against an **8.1 us** SYNC response. Finding
41 says an ACK pulse is **0.95 us**. That gap had to be closed before
Finding 39's zero could mean anything, so the detector was calibrated
directly against a known sub-microsecond low **of our own making**, placed
inside the detection region (`skip_us` set so our own last bit counts).

Stimulus: the last bit of `0xFE` is a '0' = 13 BDC cycles = **0.772 us**
low (close to, and slightly smaller than, the 16-cycle 0.950 us ACK). The
last bit of `0xFF` is a '1' = 3 cycles = **0.178 us** — the negative
control. Session baseline `sync_locked` = 16 842 106 Hz, pulse 7.60 us
(identical to the fourth session).

```
  stimulus / skip_us / window_us   ns/sample  max_low_samples   -> us
  1B 0xFE   skip  6.0   win 20        156       5, 5, 5          0.78  <- SEEN
  1B 0xFF   skip  6.0   win 20        156       1, 2, 2          0.16-0.31
  2B FF FE  skip 14.0   win 20        156       5, 5, 5          0.78  <- SEEN at t~14 us
  2B FF FF  skip 14.0   win 20        156       1, 1, 1          0.16
  1B 0xFE   skip  6.0   win 40        312       3, 3, 3          0.94  <- SEEN
  1B 0xFE   skip  6.0   win 60        468       2, 2, 2          0.94  <- SEEN
  1B 0xFE   skip  6.0   win 80        625       1, 1, 1          0.62  <- INDISTINGUISHABLE
```

Three things established:

1. **The detector resolves a 0.77 us low cleanly and repeatably** (5/5/5
   samples), and separates it from a 0.18 us low (1-2 samples), at both an
   early (~6.7 us) and a late (~14.3 us) position in the capture window. So
   an ACK pulse, which is *larger* at 0.95 us, is within its reach.
2. **Sensitivity is a function of `window_us`.** At `window_us=20` (the
   default, and what Finding 39 used) an ACK is ~6 samples; at 40 it is 3;
   at 60 it is 2; **at 80 it collapses to 1 and is no longer distinguishable
   from one of our own '1' bits.** Any ACK-hunting scan must use
   `window_us <= 60`.
3. **A REAL GAP IN FINDING 39 is now identified.** At the default
   `window_us=20` with a 1-byte tx, `skip_us` is 8.4 us, so the detection
   region is only **8.4 -> 20 us = 11.6 us of look-ahead**. HCS08RMv1
   §7.3.5 states the ACK is "not issued earlier than 32 BDC clock cycles
   after the BDC command was issued" and that **"there is no upper limit for
   the delay between the command and the related ACK pulse"**. Finding 39
   therefore only ever looked at ACK delays of 0.8-12.4 us past the end of
   the byte. **An ACK arriving later than that was structurally invisible to
   it.** Finding 39 must be re-run with a look-ahead covering a whole reset
   period.

### Finding 43 — FINDING 39 RE-RUN AND NOW CONCLUSIVE: `ACK_ENABLE` draws nothing, with a 51.6 us look-ahead and a live positive control

Finding 39's weaknesses (unverified premise, unproven ACK-scale
sensitivity, 11.6 us look-ahead) are all closed, and the answer does not
change.

**Scan.** `scan_xfer`, bit clock 16 842 106 Hz, `d` 0..43 us in 1 us steps
(a full 40 us reset period), 4 trials per phase, `skip_us` default (8.4 us,
so none of our own bits count), run at three look-ahead depths:

```
  window_us  ns/sample  look-ahead   D5 ACK_ENABLE   D6 ACK_DISABLE   90 BACKGROUND   FF (ctrl)
     20         156       11.6 us     all 0           all 0            all 0           all 0
     40         312       31.6 us     all 0           all 0            all 0           all 0
     60         468       51.6 us     all 0           all 0            all 0           all 0
```

Not "below threshold" — **zero samples low, anywhere in the look-ahead, at
every one of the 44 phases, in all 12 scans.** The line is never pulled
down at all after our byte ends.

`D5` vs `D6` is the ideal A/B pair the manual itself sets up: adjacent
opcodes, `ACK_ENABLE` documented to ACK, `ACK_DISABLE` documented **not**
to. They are indistinguishable here, which is what you would see if neither
command was received.

**End-to-end positive control, same session, same code path, same
`skip_us=None` call shape.** `set_bit_clock(1_500_000)` makes a single '0'
bit an 8.67 us low — a legal SYNC request — and `tx=[0xFE]` puts it last, so
the target's 128-cycle answer falls in the look-ahead:

```
  win=130 us (1.015 us/sample), tx_us=85.33, skip_sample=85
    runs d=0..47: 666666646533200000000000000000000000000000666664
    best 6.094 us low at t=86.33 us   (immediately after our byte ends)

  win=200 us (1.562 us/sample)
    runs: 333333331122000000000000000000000000000033333333
    best 4.688 us low at t=87.50 us
```

Hits at d=0..13, dead at d=14..40, hits again at d=41..47 — **the target's
own ~41 us reset-loop structure, recovered by the scanner**. The whole
chain (`_prime_reset` -> phase delay -> single-store burst start -> capture
-> skip -> longest-low detection) is provably live at this moment, on this
board.

**So the negative is real:** a one-byte `ACK_ENABLE`, the command the
manual explicitly designates as the host's probe for "is the target
there", fired at every phase of the target's reset loop, with an
instrument calibrated to the ACK's own 0.95 us scale and looking 51.6 us
ahead, produces no response of any kind.

**What that does and does not prove.** It proves the target's BDC does not
*execute* `ACK_ENABLE` at any phase reachable this way. It does not yet
distinguish between:

  (a) the command is never *received* (the 18.5 us post-reset blackout of
      Finding 36 plus the 7.6 us byte plus the ACK's own minimum 1.9 us
      delay plus its 0.95 us width need 10.45 us of the 12.9 us receptive
      window, so the byte must begin in the window's first ~2.4 us — only
      ~2-3 of the 44 phase points, and hit rates degrade near window edges
      even for SYNC);
  (b) the command is received and the BDC refuses to execute it — which on
      this family would point at **security**, and that is the next thing
      to settle from the primary source.

### Finding 44 — SECURITY SETTLED FROM THE PRIMARY SOURCE, AND IT DOES **NOT** EXPLAIN THE SYMPTOM

Both halves of the long-standing security hypothesis were checked in
HCS08RMv1 §4.6 / §4.7.2. **Half of it is confirmed and half of it is
refuted, and the refuted half is the half the project was relying on.**

**Confirmed — a blank part IS secured.** §4.7.2, Table 4-8 "Security
States":

```
   SEC01:SEC00   Description
       0:0        secure
       0:1        secure
       1:0        unsecured
       1:1        secure
```

and §4.6 says it in words:

> "Security is engaged or disengaged based on the state of two nonvolatile
> register bits (SEC01:SEC00) in the FOPT register. ... The 1:0 state
> disengages security while the other three combinations engage security.
> **Notice that the erased state (1:1) makes the MCU secure.**"

An erased NVOPT reads 0xFF, so SEC01:SEC00 = 1:1. **This target is almost
certainly secured.** That part of the recalled hypothesis was right, and it
is now verified rather than remembered.

**REFUTED — security does not block the commands we are sending.** §4.6,
first paragraph:

> "When security is engaged, FLASH and RAM are considered secure
> resources. **Direct-page registers, high-page registers, and the
> background debug controller are considered unsecured resources.**
> ... Attempts to access a secure memory location with a program executing
> from an unsecured memory space or through the background debug interface
> are blocked (writes are ignored and reads return all 0s)."

and, decisively:

> "The on-chip debug module cannot be enabled while the MCU is secure.
> **The separate background debug controller can still be used for
> non-intrusive background memory access commands**, but the MCU cannot
> enter active background mode except by holding BKGD/MS low at the rising
> edge of reset."

and §4.5.4's list of FACCERR causes contains the matching statement from
the FLASH side:

> "Writing the byte program, burst program, or page erase command code
> ($20, $25, or $40) with a background debug command while the MCU is
> secured (**The background debug controller can only do blank check and
> mass erase commands when the MCU is secure.**)"

**So on a secured HCS08:**

| Operation | Blocked by security? |
|---|---|
| `SYNC` | no |
| `ACK_ENABLE` / `ACK_DISABLE` (non-intrusive, BDC-internal) | **no** |
| `READ_STATUS` / `WRITE_CONTROL` (BDCSCR — the BDC is an *unsecured* resource) | **no** |
| `READ_BYTE`/`WRITE_BYTE` of **high-page registers** (FCDIV $1820, FPROT $1824, FSTAT $1825, FCMD $1826 — all explicitly "unsecured resources") | **no** |
| `READ_BYTE`/`WRITE_BYTE` of FLASH or RAM | yes — writes ignored, reads return all 0s |
| FLASH byte-program / burst-program / page-erase via BDM | yes |
| FLASH **blank check** and **mass erase** via BDM | **no — explicitly still allowed** |
| Entering active background mode via `BACKGROUND` | yes |
| Entering active background mode by holding **BKGD/MS low at the rising edge of reset** | **no — this is the documented secure-part entry path** |

**Consequences, and they are large:**

1. **Security cannot explain Finding 43.** `ACK_ENABLE` is non-intrusive
   and executed inside the BDC, which §4.6 names as an *unsecured*
   resource. A secured part is documented to answer it. It does not.
   Something other than security is stopping BDC commands from being
   received or executed. **The "the part is secured, that's why nothing
   works" line that CONTEXT.md has carried since Finding 34 is dead as an
   explanation of the no-response symptom** — though the part being secured
   remains true and still matters for what we can do once commands work.
2. **Security also cannot explain Finding 15** (no BDCSCR write ever
   lands) — BDCSCR is part of the BDC, an unsecured resource. That
   conclusion in Finding 15 was already drawn on logic; it is now backed by
   the manual.
3. **The eventual unsecure recipe is confirmed and is reachable without
   ever entering active background mode**, because every register it needs
   is a high-page (unsecured) register and both FLASH commands it needs are
   the two a secured part still allows. §4.6:

   > "Security can always be disengaged through the background debug
   > interface by following these steps: 1. Disable any block protections by
   > writing FPROT. FPROT can be written only with background debug
   > commands, not from application software. 2. Mass erase FLASH, if
   > necessary. 3. Blank check FLASH. **Provided FLASH is completely erased,
   > security is disengaged until the next reset.** To avoid returning to
   > secure mode after the next reset, program NVFOPT so SEC01:SEC00 = 1:0."

   §4.7.2 restates it: "SEC01:SEC00 changes to 1:0 after successful backdoor
   key entry or **a successful blank check of the FLASH memory**." So on an
   already-blank part a *blank check alone* unsecures it. `bdc.py` already
   has `FCMD_BLANK_CHECK` wired up.
4. **The documented way to halt a secured part is the BKGD/MS pin, not
   `BACKGROUND`** — which is what `hardware_reset_to_bdm` does, and which
   Finding 25 showed does not latch. That failure is now a *bigger* anomaly
   than it was, because the manual says it is the one path security leaves
   open.

**Where that leaves the diagnosis.** Three explanations for "SYNC answers
perfectly, no command ever does" survive, and security is no longer one of
them:

- **(i) The command bytes are not being *received*.** The BDC's SYNC
  detector is a pure pulse-width measurement that needs no bit framing;
  command reception needs the BDC to sample 8 bits at the right instants.
  Everything verified so far verifies the waveform *we emit*, never that the
  BDC framed it. The 18.5 us post-reset blackout (Finding 36) plus the
  10.45 us a byte-plus-ACK needs leaves only ~2.4 us of the 12.9 us window
  where the whole exchange fits — a genuinely tight target.
- **(ii) The BDC is reset (and its command state wiped) every 40 us**, so
  no command ever survives to execute, while SYNC — which answers
  immediately and statelessly — always can.
- **(iii) The model of GPIO14 is still incomplete.** Finding 26 (an
  internal pull-DOWN on GPIO14 makes the 25 kHz train read as a solid high)
  is electrically impossible for a plain open-drain RESET with a pull-up and
  has never been explained.

### Finding 45 — the receptive window located in `scan_xfer`'s OWN phase frame, and the arithmetic that follows

Every earlier phase number in this file is in `after_reset_sync`'s frame,
which is offset from `scan_xfer`'s by an unknown constant. So the window
was re-located **inside `scan_xfer` itself**, using the Finding 38
end-to-end control (`set_bit_clock(1_500_000)`, `tx=[0xFE]`, whose single
trailing '0' bit is an 8.67 us low = a legal SYNC request released at
`d + tx_us`). 1.015 us/sample, 6 trials, d = 0..59 in 1 us steps:

```
  d:      0    5    10   15   20   25   30   35   40   45   50   55
          206666655333100000000000000000000000000606666664533210000000
             ^^^^^ full width           reset period ~39 us  ^^^^^^
```

Live at d = 2..12 (full width d = 2..6, decaying to a stub by d = 12), dead
d = 13..40, live again d = 41..51. **Period 39 us**, matching Finding 25's
40.0 us to within the quantisation.

Reducing to absolute phase (release = `d + 85.33 us`, and 85.33 mod 39 =
7.33):

```
  BDC receptive:            phase 9.3 -> 20.0 us      (width 10.7 us)
  target resets at:         phase ~20.0 us            (period 39 us)
```

**Now the budget for a one-byte command plus its ACK**, using HCS08RMv1
§7.3.5's own numbers at the measured 16.842 MHz BDC clock:

```
  command byte starts at phase   d      (its first falling edge must be
                                         inside the receptive window: d >= 9.3)
  byte ends                      d + 7.60
  earliest ACK start (+32 cyc)   d + 9.50
  ACK ends (+16 cyc)             d + 10.45
  window closes (reset)          20.0
```

- for the whole ACK to appear: `d + 10.45 <= 20.0` -> `d <= 9.55`
- for the BDC to frame the byte at all: `d >= 9.3`

**The viable slice is phase 9.3 -> 9.55 us — a quarter of a microsecond, in
a 39 us cycle.** Even allowing a *truncated* ACK (any part of it before the
reset) only widens it to `d <= 10.5`, i.e. ~1.2 us.

So Finding 37's table needs an amendment: a one-byte command *byte* fits
the window comfortably (7.6 of 10.7 us), but **a one-byte command plus the
ACK that is its only observable does not fit with any margin at all.**

### Finding 46 — 8 400 phase-locked `ACK_ENABLE` transactions at the predicted viable phase: still exactly zero

`scan_xfer`, bit clock 16 842 106 Hz, `window_us=20` (156 ns/sample — the
most sensitive setting, where an ACK would be ~6 samples and even a
reset-truncated sliver of one would be 1-3), `d` = 4..17 us (bracketing the
computed 9.3-10.5 us slice with ~5 us of margin either side),
**300 trials per phase**:

```
  ACK_ENABLE  $D5   d=4..17:  00000000000000    (4 200 transactions, 11 s)
  ACK_DISABLE $D6   d=4..17:  00000000000000    (4 200 transactions, 11 s)
```

Not one low sample, at any phase, in 8 400 transactions.

**Honest reading.** This is a very strong negative, but Finding 45 shows it
is *not yet* logically airtight: the arithmetic says the viable slice is
~0.25-1.2 us wide, and `scan_xfer`'s phase step is limited to 1 us by
`sleep_us`, so a scan cannot guarantee it ever landed inside it. The
remaining ambiguity is now **quantified and small**, but it is real.

**The way to remove it is to break the geometry, not to keep scanning.**
Everything about this problem — the 10.7 us window, the 39 us period, the
truncated answers — comes from the target free-running through a blank-FLASH
reset loop. Stop that loop and every constraint disappears at once. Per
Finding 44 the manual gives exactly one route that a secured part still
allows: **hold BKGD/MS low at the rising edge of reset.** Finding 25 says
that does not latch, and that is now the single most important unexplained
result in this project.

### Finding 47 — the BKGD/MS BDM-entry failure re-confirmed with a COMPLETELY INDEPENDENT observable (CLKSW)

Finding 25 concluded `hardware_reset_to_bdm` never latches, using RESET-pin
activity as the observable. HCS08RMv1 §7.3.2.1 supplies a second,
unrelated one:

> "When the MCU is reset in normal user mode, CLKSW is reset to 0 which
> selects the alternate clock source. ... **When the MCU is reset in active
> background mode, CLKSW is reset to 1 which selects the bus clock as the
> source of the BDC clock.**"

So if the target ever entered active background mode at a reset, its BDC
clock would switch from the ~16.8 MHz FEI DCO to the ~4.19 MHz bus clock,
and a SYNC response would grow from **7.6 us to ~30.5 us** — a 4x change
that needs no command, no ACK and no read phase to see. Additionally, a
halted CPU stops generating illegal-opcode resets, so plain `sync` (which
currently succeeds only ~20% of the time because it fires at a random phase
of the reset loop) would start succeeding every time.

```
                     RESET activity (%low)   plain sync x12 (MHz, '.' = no response)
  BASELINE           25.0 24.7 24.4          28.07 20.0 . . . . . . . . . .
  bkgd_hold_us=100   24.5 24.8 24.6          . . . . . . . . 25.0 . . .
  bkgd_hold_us=1000  25.0 24.8 24.5          . . 18.6 . . . . . 19.51 . 17.02 .
  bkgd_hold_us=10000 24.9 24.6 24.5          . . . . . . . . . . 20.51 . 17.02
```

Every row identical to baseline. The reset loop never stops and the BDC
clock never changes. (The 17-28 MHz readings are *truncated* responses —
truncation shortens the pulse, so plain `sync` over-reports; see
`sync_locked`'s docstring.) **Two independent observables now agree: the
BKGD/MS mode latch does not engage on this target**, which per Finding 44
is the one BDM-entry route a secured part still permits.

### Finding 48 — RESOLVED: Finding 26's "pull-down makes the RESET train vanish" is not anomalous after all

An RP2040 internal pull-down is ~60 k. The S08 RESET pin has an internal
pull-up of roughly the same order. Together they form a divider that parks
the net at **~2 V**. The RP2040's input (Schmitt, VIH ~1.5-2.0 V) reads that
as **HIGH**; the S08's RESET input (VIH = 0.7 x VDD = 2.31 V) reads it as
**LOW**. So the target is held permanently in reset, stops running, stops
taking illegal-opcode resets, and therefore **stops driving RESET low at
all** — leaving the pin sitting at the 2 V divider, which the Pico reports
as "high, 0 transitions".

Finding 26 is therefore fully consistent with GPIO14 being an ordinary
open-drain RESET with a pull-up, and the note in the third session's
"suggested next steps" that it is "electrically backwards and nobody has
explained it" can be struck. It also gives a *free hardware-free way to
hold the target in reset* (`probe_pin`-style pull-down on GPIO14) with no
wiring change, if that is ever useful.

### Finding 49 — THE BETTER TEST: `READ_STATUS`'s FIRST read bit DOES fit the window, and it is still silent

Finding 45's budget kills `ACK_ENABLE` as a probe (the ACK's mandatory
32-cycle delay pushes the exchange to 10.45 us in a 10.7 us window). But
there is a one-byte-command observable that arrives **almost immediately**
and that Finding 37's table got wrong by treating `[0xE4,0xFF]` as an
indivisible 15.2 us block:

`READ_STATUS` is coded `E4/SS` — **no `d` delay**. The host clocks each
status bit with its own falling edge, and per Figure 7-4 the target answers
a '0' bit by holding BKGD low for **13 of that bit's 16 cycles**, starting
within one cycle of our marker. BDCSCR's reset value is 0x00, so the first
status bit is a '0'. Therefore:

```
  bits 1-8  (0 -> 7.60 us)   our command byte 0xE4
  bit 9     (7.60 -> 8.55)   our read-bit marker; TARGET should hold low
                             7.66 -> 8.37 us  = 0.77 us  = 5 samples @156ns
  bits 10,11                 two more status '0' bits, also inside the window
  bits 12-16                 fall past the reset; don't care
```

The command byte plus the **first** read bit is **8.55 us**, which fits the
10.7 us window with room to spare (viable phase `d` = 9.3 -> 11.45 us, a
2.15 us slice, comfortably sampled by a 1 us scan). Our own `0xFF` markers
are '1' bits = 0.18 us = 1-2 samples, so a target answer is unambiguous.

`scan_xfer`, `skip_us=7.5` (detection starts just before bit 9),
`window_us=20` (156 ns/sample), d = 4..19 us in 1 us steps, **200 trials per
phase**:

```
  READ_STATUS [E4 FF]   d=4..19:  2222222222222222   best 0.312 us
  neg control [FF FF]   d=4..19:  2222222222222222   best 0.312 us
  pos control [FF FE]   d=4..19:  5555555555555555   best 0.781 us @ 14.69 us
```

The positive control — our own trailing '0' bit — is picked up as 5 samples
/ 0.781 us at every single phase, so the detector is live and correctly
thresholded. `READ_STATUS` is **bit-for-bit identical to the all-'1'
negative control**, at every phase, over 3 200 transactions.

**This closes the loophole Finding 45 left open.** The observable no longer
needs the marginal 10.45 us; it needs 8.55 us of a 10.7 us window, with a
2.15 us window of viable phases scanned at 1 us. The target still emits
nothing.

**Conclusion: the target's BDC answers SYNC perfectly and does not
receive, frame or execute ANY command — and this is now established with a
transaction that provably fits its receptive window, an observable that
provably registers on the instrument, and security eliminated as a cause.**

### Finding 50 — all 256 opcodes, correct phase frame, first-read-bit observable, validated detector: 26 112 transactions, zero

The definitive version of the sweep Findings 34 and 39 were each trying to
be. `tx = [opcode, 0xFF]`, `skip_us=7.5` (so the detector watches bit 9
onward, where a target answer lands), `window_us=20` (156 ns/sample),
d = 4..20 us in 1 us steps (bracketing Finding 45's viable 9.3-11.45 us
slice), 6 trials per phase, every opcode 0x00-0xFF:

```
  256 opcodes x 17 phases x 6 trials = 26 112 phase-locked transactions, 82 s
  -> global maximum low run after bit 9: 2 samples = 0.312 us
     (that is our OWN '1' marker; the positive control reads 5 samples)
  -> opcodes producing any target-driven bit: NONE
```

This simultaneously covers a wrong opcode value, a wrong bit order (LSB-first
`0xE4` is `0x27`, in the set), a wrong polarity (the complement is in the
set), and every phase of the reset loop at 1 us — with, for the first time,
an observable that provably fits the target's receptive window and an
instrument proven on the same run to register that observable.

## Where the FIFTH session leaves the investigation

**Settled this session, all from the primary source (HCS08RMv1) or from
measurement on this board:**

1. `ACK_ENABLE` ($D5) genuinely does issue an ACK pulse, by design, and the
   manual names it as the host's "is the target there" probe (Finding 40).
   The previous session's flagged-as-unverified recollection was correct.
2. The ACK pulse is 16 BDC cycles (0.95 us) and cannot be issued sooner than
   32 BDC cycles (1.90 us) after the command ends, with no documented upper
   bound (Finding 41).
3. `scan_xfer` resolves a 0.95 us low cleanly, but only at `window_us <= 60`
   (Finding 42) — and Finding 39's default `window_us=20` gave it only
   11.6 us of look-ahead, a real gap that is now closed.
4. A blank part IS secured (SEC01:SEC00 = 1:1), verified against Table 4-8
   — **but security explicitly does NOT block non-intrusive BDC commands,
   BDCSCR access, high-page register access, blank check or mass erase**
   (Finding 44). **Security is therefore dead as an explanation for the
   no-command-response symptom.** It has been the project's leading
   hypothesis since Finding 34 and it is wrong.
5. The BKGD/MS mode latch — the one BDM entry a secured part still allows —
   does not engage, now confirmed by a second, independent observable
   (Finding 47).
6. Finding 26's long-standing "electrically impossible" pull-down anomaly is
   explained and resolved (Finding 48).
7. The target's ~8.6 us RESET low is the documented reset-source-detection
   sequence of §2.2.3 ("driven low for about 4.25 us, released, and sampled
   again about 4.75 us later"), **not** the "34 bus cycles" this file
   previously guessed at in Findings 8 and 22.
8. The receptive window is located in `scan_xfer`'s own phase frame:
   **phase 9.3 -> 20.0 us of a 39 us cycle** (Finding 45).

**The central fact, now on much firmer ground than before:**

> The target's BDC answers SYNC perfectly, repeatably, on demand, with a
> full 128-cycle response at 16.842 MHz — and receives, frames or executes
> **no BDC command whatsoever**, with security eliminated, with a
> transaction that provably fits the receptive window, with an observable
> that provably registers, at every phase, over 40 000+ phase-locked
> transactions this session alone.

**What is NOT yet explained, and what to try next:**

1. **Why does SYNC work and nothing else?** SYNC is a pure pulse-width
   measurement needing no bit framing; a command needs the BDC to frame 8
   bits and sample each at its cycle 10. Every verification in this project
   verifies the waveform *we emit* (and it is textbook-correct at 27 ns
   resolution — Finding 30). Nothing has ever verified that the BDC framed
   it. A mechanism that breaks framing while leaving SYNC intact is the
   shape of the remaining bug.
2. **Is it the right silicon?** A part with a BDM that uses the same
   physical SYNC/bit protocol but a *different command set* would produce
   exactly this symptom set — SYNC perfect, all 256 HCS08 opcodes inert.
   RS08 is the obvious family to rule in or out. The part identity rests on
   a "SSG4MTJ" marking read; it has never been confirmed electrically.
   Worth reading the package marking again carefully.
3. **Is the BDC clock actually stopped during the 18.5 us blackout?** That
   would explain Finding 35's otherwise odd result (a SYNC low spanning the
   target's reset works, provided the release lands in the window) and it
   predicts that the BDC's 512-cycle inter-bit timeout (§7.3.2, = 30.4 us at
   16.84 MHz) also stalls. Testable by varying the gap between two
   transactions.
4. **Do not spend more time on**: security (dead, Finding 44); sweeping
   opcodes, bit clocks or phases (exhausted three separate ways now);
   `ACK_ENABLE` as a probe (Finding 45 shows it is the *worst* one-byte
   probe available — `[0xE4,0xFF]` with `skip_us=7.5` is strictly better).

## SIXTH session (2026-09-14) — chip identity confirmed; hunting the framing failure

Chip identity is now CONFIRMED from the physical package marking, read off
the parts by the user: **`SSG4CTJ5PVS`** and **`SSG8CTJ5PVS`**, Freescale
logo. These are genuine S9S08SG4 / S9S08SG8 parts. The "wrong silicon
family / RS08" hypothesis in the fifth session's next-steps list (item 2)
is therefore **closed: it is the right family, and the HCS08 BDC command
set applies.**

### Finding 51 — the BDC is DEAD while the RESET pin is asserted (so "hold the part in reset and talk to it" is not a route)

Plain `sync` x15 per condition, same session, nothing else changed:

```
  RESET released (normal)   .  .  17.02 21.33  .  17.78  .  . ... ->  3/15 answered
  RESET driven LOW by Pico  .  .  .  .  .  .  .  .  .  .  .  ... ->  0/15 answered
  RESET released again      .  .  .  .  18.82 . . . 17.02 ... ->  2/15 answered
```

With RESET held low the target also shows **zero** activity on BKGD
(`probe_pin(15)`: 0.0% low, 0 transitions), i.e. it is not answering and
not doing anything else either.

**Consequence.** The BDC is held in reset along with the rest of the chip,
so the obvious geometry-breaking move — park the target in reset, where it
has no 39 us loop, and take all the time we want — **does not work**. It
also explains the receptive-window shape from the other side: the ~10.7 us
of each 39 us cycle in which SYNC answers is exactly the part of the cycle
when the chip is *out* of reset. The target is in (mostly internal) reset
for roughly 73% of every cycle, even though the RESET *pin* is only low
~22-25% of it.

### Finding 52 — new tool: `scan_wave`, an arbitrary cycle-by-cycle BKGD waveform player

`bdc_tx_byte` hard-codes the protocol's 16-cycle bit with 3- and 13-cycle
lows as PIO `[delay]` counts. The only knob a host could turn was the
**clock**, which scales the period and both low widths together and can
never change their **ratio**. So a target whose sample point is not where
we think it is would decode *every* bit to the same value, turn *every*
opcode into 0x00 or 0xFF, and be completely invisible to the opcode, clock
and phase sweeps this project has run. That gap is now closed.

`bdc_raw_dirs` (in `bdc_pio.py`) is a **one-instruction** PIO program:
`out(pindirs, 1)`, autopull, `fifo_join=PIO.JOIN_TX`. It shifts a bit
stream from the TX FIFO straight into the pin direction, one bit per PIO
cycle — 1 = drive BKGD low, 0 = release to the pull-up. Clocked at the
target's BDC clock it lets the host describe **any** waveform cycle by
cycle. The joined 8-word TX FIFO holds **256 contiguous BDC cycles** = two
whole 16-cycle bytes. It lives on PIO block 0 next to `bdc_sync`
(28/32 slots); block 1 is untouched.

New firmware commands:
`{"cmd":"scan_wave","words":[...],"n_cycles":N,"d_from":..,"d_to":..,
"d_step":..,"trials":..,"sample_count":..,"window_us":..,"skip_us":..}`
— identical phase-locking, single-store burst start and
longest-low-after-`skip_us` detector as `scan_xfer`, so results are
directly comparable — and `{"cmd":"capture_wave",...}` for checking a
host-built waveform before trusting a scan built on it.

**Known limitation, measured:** the raw player has no speed-up pulse (a
1-instruction program has no spare side-set), so every rising edge carries
the ~130 ns pull-up RC tail of Finding 30. With `low0 = 13` that leaves
0-60 ns of high between back-to-back '0' bits and **consecutive '0' bits
merge into one long low** (confirmed by `capture_wave`: `0x00` at
low0=13 renders as `L0.87 L1.86 L0.87 ...`). Use `low0 <= 11` with this
player; `bdc_tx_byte` remains the tool for the exact 3/13/16 shape.

### Finding 53 — bit SHAPE, bit PERIOD and leading dummy bits: all swept independently, all silent

Stimulus `0xE4` (READ_STATUS) plus one read-bit marker, `scan_wave`,
d = 0..43 us in 1 us steps, 4 trials per phase, 77-115 ns/sample.
Observable: longest low in the marker bit. Controls run in the same loop —
`POS-CTRL` makes the marker our OWN long low, `NEG-CTRL` replaces the
opcode with 0xFF.

```
  A. shape       low0 = 7..12  x  low1 = 1..5   (30 combinations)
       probe  E4 : 2,3,4,5 samples depending on low1 — i.e. EXACTLY our own
                   marker's width, identical to NEG-CTRL at every low1
       POS-CTRL   : 7-11 samples (0.62-0.86 us)  <- detector demonstrably live
  B. framing     0..5 leading dummy '1' bits before the opcode
       probe == NEG-CTRL at every prefix length, every phase
  C. period      12,13,14,15,16,17,18,20,22,24 cycles/bit at a fixed
                 low fraction
       probe == NEG-CTRL at every period, every phase
```

The probe tracks `low1` exactly (low1=1 -> 2 samples, low1=5 -> 5 samples)
and never exceeds it. **The target never extends our marker bit by even
one sample, under any bit shape, any bit period, or any amount of
pre-command idle framing.** Combined with the earlier 12-30 MHz bit-clock
re-sweep (probe bit-for-bit identical to the all-'1' control at all 18
rates x 44 phases), the entire host-waveform parameter space is now
exhausted: value, order, polarity, rate, shape, ratio, framing, phase.

### Finding 54 — the power fault really is gone: BKGD's state no longer affects the target at all

Second session's Finding 20 ("only a Hi-Z BKGD produces the RESET train;
pinning BKGD at either rail stops it dead") was the strongest single
symptom of the parasitic-powering fault. **Re-run now, with the corrected
wiring, it is completely negative**, `probe_pin(14)`, 25 ms windows:

```
  BKGD Hi-Z         24.8 / 24.8 / 24.4 % low   ~490 transitions
  BKGD driven LOW   25.2 / 24.6 / 24.4 %       ~487
  BKGD Hi-Z         24.8 / 24.5 / 24.3 %       ~493
  BKGD driven HIGH  24.8 / 24.7 / 24.7 %       ~493
  BKGD Hi-Z         24.8 / 24.6 / 24.5 %       ~490
```

`rise_time` BKGD [52,34,36,36,35,32] us and RESET [52,34,35,58,55,34] us —
both at the MicroPython polling floor, and BKGD unchanged at hold=20 ms
[55,40,33,33], i.e. no charge-storage behaviour on either net.

So the target is genuinely, independently powered and free-running, and
**holding BKGD low for 25 ms does not halt it** — which is correct HCS08
behaviour (a continuous low is just a never-completed SYNC request; it is
not a CPU halt). Finding 8's original "holding BKGD low silences the
target" is dead in both of its readings: it was a power artifact then, and
the effect does not exist at all now.

### Finding 55 — independent cross-check of the BDC clock from the SYNC *threshold*, and a ~9% discrepancy

The SYNC response width (128 cycles) has always been the only measurement
of the target's BDC clock. `scan_wave` makes two more available: the
minimum host-low that triggers a response (spec: >=128 target cycles) and
the delay from our release to the response (spec: 16 target cycles).

Host low swept 32..192 of OUR cycles at an assumed 16.842 MHz (59.4 ns),
d = 0..59 us, 6 trials/phase. `f` = 15+ low samples, `0-3` = our own edge:

```
   N (our cycles)   our low     target response
     32 .. 136       1.9-8.1 us   nothing anywhere (1-3 samples = quantisation)
    144              8.55 us      7.77 us, 1.24 us after our release   <- ON
    160              9.50 us      7.76 us, 1.25 us
    176             10.45 us      7.72 us, 1.12 us
    192             11.40 us      7.84 us, 1.21 us
```

Sharp threshold **between 136 and 144 of our cycles**, and the per-phase
hit pattern shows the target's own ~40 us reset structure, so these are
real responses.

- **Response width** 7.72-7.84 us (quantisation-inflated from the
  `sync_locked` value of 7.60) / 128 cycles -> **16.8 MHz**.
- **Threshold** ~140 of our cycles, plus ~4 cycles of pull-up RC before the
  target's higher VIH sees the rising edge, so ~144 effective. If that is
  the spec's 128 cycles, the target's clock is **~15.3 MHz**, i.e. our
  assumed clock is ~9% fast.
- **Release-to-response delay** 1.12-1.25 us against a spec 16 cycles;
  0.95 us at 16.84 MHz plus ~0.24 us of RC = 1.19 us. Consistent with
  16.8 MHz; too coarse (170 ns/sample) to separate the two candidates.

Two of the three say 16.8 MHz and one says ~15.3 MHz; the odd one out is
also the one with an implementation-defined margin (a synchroniser needs
more than exactly 128 cycles). **It does not rescue the framing
hypothesis either way** — the 12-30 MHz bit-clock sweep and the
12-24-cycle bit-period sweep both cover 15.3 MHz comfortably and both are
silent. Recorded because it is the only quantitative disagreement about
this target's clock anywhere in the project.

### Finding 56 — the BDC is awake and unharmed while our command bits go past it

If the target's BDC were asleep, or if our bit stream were putting it into
some wedged state, that would explain the silence. Both are now excluded by
a test that needs no command to execute.

**Method.** `scan_wave` stimulus = *k* command-shaped bits, then a
144-cycle low (a SYNC request — Finding 55 puts the threshold just under
that), then release. The target's SYNC answer is the readout.

```
  prefix           live d band (1 us steps, 6 trials)   answer width
  none             d = 1..7      and 36..46             7.90 us
  1 x '1' bit      d = 0..6      and 34..45             7.96
  2 x '1' bits     d = 0..5      and 32..43             7.81
  3 x '1' bits     d = 0..3      and 33..42             7.97
  4 x '1' bits     d = 0..3      and 32..42             7.75
  5 x '1' bits     d = 0..2      and 30..41             7.87
  6 x '1' bits     d = 0..1      and 28..39             7.78
  (identical picture for 1..6 '0' bits)
```

Two things follow:

1. **The answer is full width every time.** Six framed BDC bits — with all
   their falling edges, 3-cycle and 10-cycle lows — leave the BDC perfectly
   able to detect and answer a SYNC immediately afterwards. Our bits do not
   wedge, desynchronise or disable it.
2. **The live band moves EARLIER by exactly one bit time (~0.95 us) per
   added prefix bit**, i.e. the live window is a constraint on the *release
   instant alone* and not on when the burst started. Combined with Finding
   35 (a SYNC low may span the target's entire reset and still be
   answered), the BDC's low-duration counter is effectively free-running:
   **what the ~13 us window gates is the target's ability to RESPOND, not
   its ability to watch the pin.**

That is the sharpest statement of the anomaly this project has:
*the BDC watches BKGD continuously, responds correctly to the one stimulus
that needs no framing, and responds to nothing that does.*

### Finding 57 — BKGD/MS BDM entry swept over its ENTIRE timing space (63 combinations): never latches

Finding 47 varied only `bkgd_hold_us`. This varies all three knobs of
`hardware_reset_to_bdm` together — `pre_low_ms` (how long BKGD is already
low before RESET is asserted) x `reset_ms` (how long RESET is held) x
`bkgd_hold_us` (how long BKGD stays low after RESET is released) — and
reads out on **both** observables at once: RESET-pin activity (0% if the
CPU halted) and plain-`sync` hit rate (~100% if there is no reset loop).

```
  pre_low_ms   0, 1, 10
  reset_ms     1, 5, 50
  bkgd_hold_us 20, 50, 100, 300, 1000, 5000, 30000
  = 63 entry attempts, 504 syncs

  baseline (no attempt)            sync 0/10   RESET 24.7%
  ALL 63 combinations              sync 0-3/8  RESET 24.2-25.1%
```

Every row is baseline. The RESET activity never moves outside a 0.9-point
band across the whole matrix, and the sync hit rate stays at the
free-running ~10-25%. **There is no combination of pre-low, reset width and
post-release hold that makes this target latch active background mode.**

Per Finding 44 this is the one BDM-entry route the manual says a secured
part still permits, so its failure is not explainable by security either.

### Finding 58 — the LAST geometric loophole closed: the command fired at every 59 ns of the reset cycle

Every phase scan in this project — Findings 29, 31, 32, 34, 39, 43, 46, 49,
50 and this session's — steps the delay with `sleep_us()`, i.e. in **1 us =
17 BDC-cycle** jumps. Finding 45's arithmetic put the viable slice for an
`ACK_ENABLE` exchange at only 0.25-1.2 us wide, so "the scan never landed
inside it" has been a live objection the whole time.

`scan_wave` removes it: padding the waveform with leading idle cycles
shifts the whole burst by **59.4 ns** per cycle, and pad x `delay_us` is a
two-dimensional grid at BDC-cycle resolution.

```
  stimulus  READ_STATUS 0xE4 + one read-bit marker (8.55 us)
  pad       0..16 cycles  (0 -> 950 ns, i.e. a full bit time of offset)
  d         2..21 us in 1 us steps
  trials    8 per point
  = 2 720 phase-locked transactions covering EVERY 59 ns of the cycle

  probe     4 or 5 samples at every one of the 340 grid points
  NEG ctrl  4 samples (our own marker, identical)
  POS ctrl  9 samples (0.70 us — detector live in the same run)
```

**Global maximum across the entire sub-microsecond grid: 5 samples =
0.39 us, which is our own '1' marker plus one sample of quantisation.** The
positive control is 9. There is no phase, at BDC-cycle resolution, at which
this target answers a command.

### Finding 59 — looking at the wire DURING the command: complete silence, live phase and dead phase are bit-identical

`after_reset_xfer` at 156 ns/sample, 12 trials per point, averaged into a
per-sample "fraction high" profile so a single 156 ns glitch would show as
an intermediate digit. Same `tx` fired at live reset-loop phases
(d = 10,12,14,16,64,66,68 — inside and around the SYNC sweet spot) and at
dead ones (d = 30,32):

```
  E4-FF  d=10  _-----_-----_-----_____-_____-17----2____83____7--_-----_ ...
  E4-FF  d=30  _-----_-----_-----_____-_____-12----5____87____3--_-----_ ...
  FF-FF  d=16  _-----_-----_-----_7----_5----34----52----6_------_-----_ ...
  FF-FF  d=32  _-----_-----_-----_9----_8----14----10----7_------_-----_ ...
```

Every trace is exactly the host's own byte (`0xE4` = 1110 0100, `0xD5` =
1101 0101, `0xFF`), and the live-phase and dead-phase traces are
structurally identical — the intermediate digits are the capture SM's
one-sample alignment jitter at our own edges, and they appear equally at
both. **There is no glitch, no hesitation, no partial drive, nothing at all
from the target at any point during a command**, not even a wrong or
truncated reaction.

Per the standard reading, "complete silence throughout" says the target's
BDC command-parsing logic never engages at all, as opposed to engaging and
degrading partway through.

### Finding 60 — bit-clock coverage extended to 2-11 MHz: also silent

Completing the re-sweep (12-30 MHz was done earlier this session with the
same tooling). `scan_xfer`, `[0xE4,0xFF]` vs `[0xFF,0xFF]` vs the
`[0xFF,0xFE]` positive control, d = 0..43 us, 5 trials:

```
  2,3,4,5,6,7,8,9,10,11 MHz   E4 == FF at every rate, every phase
                              positive control 1.14-7.33 us everywhere
```

**Bit-clock coverage is now continuous from 2 MHz to 30 MHz with the
modern phase-locked tooling and a validated observable.**

## Where the SIXTH session leaves the investigation

Rig health at the end of the session: `sync_locked` 6/6, pulse 7.60-7.68 us,
40-47 hits out of 153 attempts, 16.67-16.84 MHz. Everything below was
measured with that rig demonstrably working.

**Closed this session:**

| Question | Answer |
|---|---|
| Is it the right silicon? | **Yes.** Package marking read directly: `SSG4CTJ5PVS` / `SSG8CTJ5PVS`, Freescale. Genuine S9S08SG4/SG8. RS08 hypothesis dead. |
| Bit order / opcode value / polarity | Covered by Finding 50's 256-opcode sweep and re-covered here. |
| Bit **rate** | 2-30 MHz, continuous, phase-locked, validated observable. Silent. |
| Bit **shape** (low1 x low0, the ratio the clock sweep can never change) | 1-5 x 7-12 cycles. Silent. (Finding 53) |
| Bit **period** | 12-24 cycles at fixed low fraction. Silent. (Finding 53) |
| Pre-command framing | 0-5 leading dummy bits. Silent. (Finding 53) |
| Command-to-data-phase gap | 0, 8, 16, 32, 48, 64 cycles, 1 and 4 read markers. Silent. |
| **Phase, at BDC-cycle resolution** | 2 720 transactions covering every 59 ns of the 40 us cycle. Silent. (Finding 58) |
| Does our bit stream wedge the BDC? | **No** — it answers a SYNC immediately after 1-6 framed bits, full width. (Finding 56) |
| Is the BDC deaf outside its window? | **No** — the window gates the target's ability to RESPOND, not to watch the pin. (Finding 56) |
| Can we park the target in reset and take our time? | **No** — the BDC is dead while RESET is asserted, 0/15 syncs. (Finding 51) |
| Is the old power fault back? | **No** — BKGD's state no longer affects the target at all. (Finding 54) |
| Can BKGD/MS force active BDM? | **No**, over all 63 combinations of its three timing knobs. (Finding 57) |
| Any target reaction mid-command? | **None**, at 156 ns resolution, live phase vs dead phase. (Finding 59) |

**The host side is now exhausted in every dimension it has**: opcode value,
bit order, polarity, bit rate, bit shape, bit period, framing, inter-byte
gap, phase (at 59 ns), drive strength/speed-up (tested both with and
without), and reset-loop geometry.

**Best remaining theory, and it is new this session.** Three symptoms now
hang together:

1. the BDC's **SYNC engine** works perfectly, continuously, and to spec;
2. the BDC's **command decoder** never engages, for any byte, ever;
3. the **BKGD/MS mode latch** never engages either.

(2) and (3) are different pieces of silicon from (1) — the mode latch is a
plain hardware latch sampled at the rising edge of reset, independent of
the serial engine, of security, and of the CPU, and it is documented to
work even on a secured part (Finding 44). A part where the small standalone
SYNC block works and the rest of the BDM block does not is the shape of
**a partially non-functional BDM module on this particular die** — damage,
or a re-marked/wrong die behind a correct-looking package marking.

**The single highest-value next test is therefore a CHIP SWAP, not a
rewire.** The user has both an `SSG4CTJ` and an `SSG8CTJ`. Putting the
other one in the same socket, changing no wiring, and re-running
`sync_locked` followed by the `[0xE4,0xFF]` `scan_xfer` probe takes two
minutes and either reproduces the symptom (pointing at something still
systematic) or fixes it outright.

**Second-highest:** a genuinely independent observer. Every measurement in
this project is the Pico looking at its OWN pin. A second Pico running
sigrok-pico/PicoLogicAnalyzer, probing at the *target's* legs, is the only
way left to see what the target's end of the wire actually receives.

**Do not spend more time on:** security (dead, Finding 44); the host
waveform in any of its dimensions (exhausted, this session); phase
(exhausted at 59 ns, Finding 58); parking the target in reset (Finding 51);
BKGD/MS entry timing (Finding 57).

### New tooling added this session (all deployed to the Pico)

- `bdc_pio.bdc_raw_dirs` + `make_raw_state_machine` — one-instruction
  arbitrary waveform player on PIO block 0 SM 1, 256 contiguous BDC cycles.
- `Bdc.scan_wave(words, n_cycles, ...)` — phase-locked scan of an arbitrary
  waveform, same detector as `scan_xfer`.
- `Bdc.capture_wave(words, n_cycles, ...)` — one-shot capture for checking a
  host-built waveform.
- `{"cmd":"scan_wave",...}` and `{"cmd":"capture_wave",...}` in `main.py`.
- Host-side helper `wave.py` (build_bits / pack / show) in the session
  scratchpad; trivially re-creatable — `1` = drive BKGD low for one BDC
  cycle, `0` = release, packed MSB-first into 32-bit words.

### Finding 61 — R1 (10k BKGD pull-up) removed as a quick negative control: no change

Same chip, same everything else, R1 physically removed (BKGD now relies
solely on the chip's internal pull-up). `probe_pin(15)` idle: still 0%
low, 0 transitions — internal pull-up alone is adequate. `sync_locked`
still works, 3 calls gave 15.69/16.67/16.84 MHz (a bit more scatter than
the tightly-clustered ~16.84 MHz seen with R1 in place — plausibly a
slightly less crisp rise time feeding into the pulse-width measurement —
but same ballpark, not a qualitative change). `scan_xfer([0xE4,0xFF])`
across the full phase range: **zero response, identical to every prior
result.** R1 is ruled out as a contributing factor to the command-decoder
silence. **Chip swap (SG4<->SG8) is still the pending highest-value next
test — not done yet, deliberately deferred by the user to a later
session.**

### Finding 62 — THE ACTUAL DATASHEET ANSWER: external/internal-error resets ignore BKGD; only power-on reset latches active BDM entry

Read the MC9S08SG8 datasheet's own **Chapter 17 "Development Support"**
in full for the first time this project (pages 261-281 — everything
before this relied on the generic HCS08RMv1 reference manual, not this
part's own chapter). Section 17.1.1 "Forcing Active Background", quoted
exactly:

> "For the MC9S08SG8, you can force active background after a **power-on
> reset** by holding the BKGD pin low as the device exits the reset
> condition. You can also force active background by driving BKGD low
> immediately after a serial background command that writes a one to the
> BDFR bit in the SBDFR register. **Other causes of reset including an
> external pin reset or an internally generated error reset ignore the
> state of the BKGD pin and reset into normal user mode.**"

**This is the real explanation for Finding 25 (BDM entry never latches),
confirmed from the primary source, not inferred.** `hardware_reset_to_bdm()`
in `firmware/bdc.py` asserts the **RESET pin** — an external pin reset.
Per this exact sentence, that specific reset source is *documented* to
ignore BKGD entirely on this part. Worse: this target's free-running
40us loop resets itself via an **internally generated error reset**
(illegal opcode) — the *other* reset source this same sentence says
ignores BKGD. So the BKGD-hold trick was NEVER going to work against
this target via either mechanism actually available (RESET-pin pulse or
letting the illegal-opcode loop reset itself) — **only an actual
power-on reset (cycling VDD) can ever latch active BDM on this chip.**
The whole "sweep BKGD-hold timing exhaustively" line of investigation
(Findings 21-25, 43) was sound engineering against the wrong reset
source for entering *active* BDM specifically — it could never have
succeeded, no matter how the timing was tuned.

**This does NOT yet explain why *non-intrusive* commands (READ_STATUS,
READ_BYTE, ACK_ENABLE, etc.) fail** — Table 17-1 and the chapter
introduction both state non-intrusive commands work "at any time even
while the user's program is running," with no dependency on ENBDM/active
background mode at all (ENBDM's own field description: "0 = BDM cannot
be made active (**non-intrusive commands still allowed**)"). That part
of Finding 39/50's negative result is still open and this section does
not resolve it — see below for what was still being read when this was
checkpointed.

**New concrete thing to try**: power-cycle VDD itself (not the RESET pin)
while holding BKGD low, to catch a genuine power-on reset. Currently VDD
is wired straight to the Pico's 3V3(OUT) pin, which isn't a
software-switchable GPIO — would need rewiring VDD through a spare GPIO
configured as a push-pull output (or open-drain sinking through a
transistor/FET if current is a concern) to toggle power under firmware
control and catch the POR window. Not yet attempted; needs a small wiring
change (the user has been declining further hardware changes so far —
worth explicitly re-raising given this is no longer a "maybe it'll help"
guess but a documented requirement).

Full BDC command table (Table 17-1) cross-checked against
`firmware/bdc.py`'s opcodes: all match, plus this chapter reveals **four
commands not yet implemented** — `READ_NEXT` (0x70), `READ_NEXT_WS`
(0x71), `WRITE_NEXT` (0x50), `WRITE_NEXT_WS` (0x51) (increment H:X then
read/write) — all Active BDM only, so not relevant until active BDM
entry itself is solved, but worth adding later for completeness.

Also confirmed from this chapter (Table 17-2, WS field description): "the
**BACKGROUND command can be used to force the target CPU out of wait or
stop and into active background mode**" — a possible alternate entry path
not yet tried, though the target here isn't believed to be in a
wait/stop state (it's actively free-running/resetting), so likely
inapplicable, but noted in case that assumption is wrong.

## SEVENTH session (2026-09-15) — external research: a WORKING MC9S08SG8 BDM programmer found

### Finding 63 — EXTERNAL SOURCE: `mztulip/hcs08_bdm_programmer` is a working BDM programmer written **specifically for and tested only on the MC9S08SG8** — the exact part

https://github.com/mztulip/hcs08_bdm_programmer (AVR / Arduino Mega 2560,
MIT-ish, ~600 lines of C). Cloned and read in full. README, verbatim:

> "It had been written for MC9S08SG8, it is tested only with this
> microcontroller at the moment. ... It uses Arduino Mega 2560 board to
> realise BDM debug interface. **Simple circuit is attached using universal
> board, to allow automatically reset power supply to enter background
> mode.**"

So a *third party who got this exact chip working* considered a
**switchable target power supply mandatory hardware**, not an optional
extra. Their pin map (`bdm.h`): BKGD = PF0, RESET = PF1,
**TARGET_SUPPLY = PF2**.

**The bring-up sequence, verbatim from `hcs08_prog.c` `main()` and
`bdm.c` `enter_background()`:**

```c
DDRF = (1<<BKGD_PIN) | (1<<RESET_PIN) | (1<<TARGET_SUPPLY_PIN);
PORTF = 0;
set_RESET_low();                 // RESET driven LOW (push-pull)
set_BKGD_low();                  // BKGD  driven LOW (push-pull)
disable_target_supply(true);     // TARGET POWER OFF
_delay_ms(100);
disable_target_supply(false);    // TARGET POWER ON  <- POR happens HERE,
                                 //    with BOTH RESET and BKGD held low
serial_init();                   // (several ms pass)
enter_background();              // { RESET low; delay 10 ms;
                                 //   RESET HIGH;          <- exits reset HERE
                                 //   delay 10 us;
                                 //   BKGD HIGH; BKGD -> input }
sync_command();
read_BDCSR();
```

**The difference from every BDM-entry attempt in this project:** this holds
**RESET asserted LOW across the whole power-up** and only releases it
~10 ms later. The reset *source* is therefore a genuine power-on reset
(which MC9S08SG8 §17.1.1 says is the one source that honours BKGD/MS), but
the *exit* from reset is deferred to a moment the host chooses, with BKGD
comfortably low. Every POR attempt in this project (firmware GPIO12
version and the user's manual pushbutton version) let the chip exit POR on
its own as VDD ramped, with RESET free — so the chip was out of reset and
into its 40 us illegal-opcode loop within microseconds of VDD rising, and
every subsequent reset is an "internally generated error reset" which
§17.1.1 says **ignores BKGD**.

**Other facts recovered from this source, all directly useful:**

- `read_target_identifier()` reads **SDIDH = $1806, SDIDL = $1807**, and
  the check is `if ((sdid & 0x0fff) == 0x14) -> "MC9S08SG8"`. Top nibble
  is the mask revision. So a working READ_BYTE at $1806/$1807 gives a
  **non-0xFF, non-0x00, known-answer** target — exactly the "unambiguous
  readback" the current session was asked to find, and it also settles
  SG4-vs-SG8 identity once reads work.
- Their bit shape, converted to target BDC cycles at ~16.8 MHz (AVR 16 MHz,
  62.5 ns/instruction): bit period ~42-44 cycles, '1' low ~6 cycles,
  '0' low ~26 cycles. **They do NOT use the 3/13/16 shape** — they use a
  much longer bit period with roughly the same low/high sense. That is
  legal (the target re-syncs on each falling edge and samples at cycle 10;
  only the sample-point level matters, and the period only has to stay
  under the BDC's 512-cycle timeout). pico-bdm's 8.4 MHz point in its
  2-30 MHz bit-clock sweep is very close to this same shape, so this is
  **not** a new explanation for the silence — recorded for completeness.
- **BKGD is driven PUSH-PULL for the entire command byte** in their
  `write_byte()` (`DDRF |= 0x01` once at the top, never released until the
  read phase). pico-bdm releases BKGD to Hi-Z for 13 of every 16 cycles
  and relies on the pull-up. Untested difference; cheap to test.
- They drive **RESET push-pull HIGH** too, not Hi-Z-with-pull-up.
- After each command they insert `DLY` (~42 AVR nops, ~2.6 us) before the
  read phase — a deliberate command-to-data gap.

**Consequence / next action:** the highest-value test is now to reproduce
their entry sequence exactly, which needs target VDD back on a
switchable GPIO (GPIO12 code already exists in `bdc.py`/`main.py`).

### Finding 64 — the chip currently in the socket is PROVABLY a different die from the one used in sessions 3-6 (the swap really happened)

The user was unsure whether the pre-swap and post-swap tests used the same
physical part. Two command-free measurements settle it — both taken this
session with nothing changed but the chip:

```
                      sessions 3-6 chip      chip in socket NOW
  sync_locked pulse   7.52 - 7.68 us         8.08 - 8.16 us
  derived BDC clock   16.67 - 17.02 MHz      15.69 - 15.84 MHz
  probe_pin(14) low   24.2 - 25.1 %          13.5 - 13.7 %
```

The SYNC pulse is a 128-cycle count of the target's own FEI DCO, so a 6%
shift is a different (untrimmed, part-to-part varying) DCO — and the
reset-loop duty cycle has nearly halved. These are different dies.
**The chip swap is confirmed; "we may have tested the same chip twice" is
ruled out.**

**BUT — and this matters — the two parts share the SAME lot code.** The
markings are `SSG4CTJ5PVS` and `SSG8CTJ5PVS`: different part number, same
`5PVS` suffix. So they are NOT two independently-sourced parts; they are
the same lot, plausibly the same reel from the same seller. "Two different
chips with the identical fault would be a big coincidence" is therefore a
much weaker argument than it looked: a bad/remarked/counterfeit lot is a
single common cause. Recorded so nobody over-weights the swap result.

### Finding 65 — symptom fully reproduced on the second chip, with live controls

```
  sync_locked                        15.84 MHz, pulse 8.08 us, 33/153 hits
  scan_xfer [E4 FF] skip 7.5 win 20  2 samples at ALL 44 phases (20 trials each)
  scan_xfer [FF FF] (neg control)    2 samples at all 44 phases
  scan_xfer [FF FE] (pos control)    6 samples (0.938 us) at all 44 phases
```

Probe is bit-identical to the negative control; the detector is
demonstrably live in the same run. Identical to every result on the first
chip.

### Finding 66 — GPIO12 is currently NOT connected to anything

`rise_time(12)` returns `-1` x4 (the pin never rises after being driven
low and released — the project's established open-circuit signature), and
`drive_pin(12,0)` leaves the target's reset train completely unchanged
(13.49% vs 13.73% low). So target VDD is still on the Pico's fixed
3V3(OUT) pin and **no power-cycle experiment can run until one wire is
moved.**

### Finding 67 — datasheet Chapter 17 read directly (pages 261-272): the protocol as implemented is correct, and two details worth recording

Read from the real PDF, not recalled:

- **Sec 17.2.2**: "Data is transferred MSB first at 16 BDC clock cycles per
  bit (nominal speed). **The interface times out if 512 BDC clock cycles
  occur between falling edges from the host.** Any BDC command that was in
  progress when this timeout occurs is aborted without affecting the
  memory or operating mode of the target MCU system." 512 cycles = 32.6 us
  at this chip's 15.7 MHz — **shorter than the target's own ~40 us reset
  period**, so nothing can ever be carried across a reset boundary anyway.
- **Fig 17-2 (host-to-target)**: "there is a 0-to-1 cycle delay from the
  host-generated falling edge to where the target perceives the beginning
  of the bit time. **Ten target BDC clock cycles later, the target senses
  the bit level.** Typically, the host actively drives the pseudo-open-drain
  BKGD pin during host-to-target transmissions to speed up rising edges.
  **Because the target does not drive the BKGD pin during the host-to-target
  transmission period, there is no need to treat the line as an open-drain
  signal during this period.**" -> driving the command byte fully push-pull
  is explicitly sanctioned; the 3/13/16 shape samples correctly at cycle 10
  either way.
- **Fig 17-3 (target-to-host logic 1)**: "The host holds the BKGD pin low
  long enough for the target to recognize it (**at least two target BDC
  cycles**). The host must release the low drive **before the target MCU
  drives a brief active-high speedup pulse seven cycles after** the
  perceived start of the bit time." pico-bdm's read marker is 3 cycles low
  + a 1-cycle speed-up, released by cycle 4 < 7. Legal.
- **Fig 17-4 (target-to-host logic 0)**: target "drives the BKGD pin low
  for **13 BDC clock cycles**, then briefly drives it high". At 15.7 MHz
  that is 0.83 us = ~5 samples at 156 ns — the detector reads 6 for the
  positive control and 2 for the probe, so a real answer could not be
  missed.
- **Sec 17.2.1**: "BKGD is a pseudo-open-drain pin and **there is an
  on-chip pullup so no external pullup resistor is required.**" (Consistent
  with Finding 61: removing R1 changed nothing.)

**Nothing in Chapter 17 imposes a prerequisite this project has not met.**
There is no minimum SYNC count, no oscillator requirement, no crystal
requirement, no handshake negotiation. The host implementation is
correct against the primary source.

### Finding 68 — `power_on_reset_to_bdm` rewritten to match the known-working implementation; `bdm_check` added

`firmware/bdc.py`:

- `power_on_reset_to_bdm(off_ms=100, on_reset_hold_ms=10, bkgd_hold_us=100,
  hold_reset=True, release_bkgd=True)`. New behaviour, and the change that
  matters: **RESET is asserted LOW before power is cut and stays low for
  `on_reset_hold_ms` AFTER power returns**, so the chip cannot leave the
  power-on reset until we release RESET, with BKGD low at that instant.
  Every earlier POR attempt (firmware and the user's manual pushbutton
  version) let the part exit POR on its own during the VDD ramp, after
  which it is free-running within microseconds and every subsequent reset
  is an "internally generated error reset" that Sec 17.1.1 says ignores
  BKGD.
- `_power_drive_12ma()` raises the power GPIO's pad drive from the RP2040
  default 4 mA to 12 mA (PADS_BANK0 DRIVE=3). An SG8 at the FEI default
  draws more than 4 mA; a sagging supply would brown the part out and look
  identical to "the chip is dead". This was NOT done in the earlier
  GPIO12 experiment.
- Power-off now actively drives the rail to 0 V (`power.value(0)`) for
  100 ms so C1 is genuinely discharged. **A suspected reason the user's
  manual unplug-the-Pico test failed: the Pico's own 3V3 bulk capacitance
  may never have decayed below the POR release threshold, giving an LVD /
  brownout reset rather than a true POR — and LVD is another reset source
  Sec 17.1.1 says ignores BKGD.**
- New `bdm_check()` / `{"cmd":"bdm_check"}` — a command-free pass/fail for
  "did we enter active background mode", using all three known
  observables at once: RESET-pin duty (0% = CPU halted), plain-`sync` hit
  rate (~100% if the reset loop has stopped), and SYNC pulse width
  (active BDM resets CLKSW to 1 -> the answer grows ~4x, 8 us -> ~30 us).

Deployed and regression-checked on the real Pico. Baseline
(GPIO12 disconnected, so no actual power cycle happens):
`{"reset_low_pct": 13.68, "sync_hits": 1/12, "sync_pulse_us": [5.92]}`
before and after — i.e. unchanged, exactly as expected with the power
pin unwired. The command path itself works.

**BLOCKED ON ONE WIRE:** move target VDD from the Pico's 3V3(OUT) pin
(physical pin 36) to **GPIO12 (physical pin 16)**. Nothing else changes.

### Finding 69 — ***REAL DEFECT FOUND: the RP2040's internal PULL-DOWN is enabled on GPIO15 (BKGD)***

Read straight out of the RP2040's PADS_BANK0 registers on the live Pico
(`mem32[0x4001C000 + 4 + 4*n]`), with the rig in its normal idle state:

```
  GPIO12 (power) PADS=0x52  IE=1 DRIVE=1 PUE=0 PDE=0 SCHMITT=1
  GPIO14 (RESET) PADS=0x52  IE=1 DRIVE=1 PUE=0 PDE=0 SCHMITT=1
  GPIO15 (BKGD)  PADS=0x56  IE=1 DRIVE=1 PUE=0 PDE=1 SCHMITT=1   <-- PDE=1
                                                      ^^^^^^
```

**BKGD — the one pin that carries the entire protocol — has the RP2040's
~60 kohm internal pull-DOWN engaged.** GPIO14 and GPIO12 do not.

**Why only that pin:** PUE=0/PDE=1 is the RP2040's hardware *reset default*
for every pad. `Bdc.__init__` constructs `Pin(reset_pin, Pin.IN)` and
`Pin(power_pin, Pin.OUT)`, and MicroPython's `Pin()` constructor clears the
pulls — so 14 and 12 get cleaned up. **BKGD is never passed through a
`Pin()` constructor in `__init__`; it goes straight to `pio_gpio_init()`,
which does not touch the pad's pull bits.** So BKGD kept the power-on
default and nobody ever looked.

**Why this is exactly the Finding 48 trap, on the other pin.** Finding 48
resolved the long-standing GPIO14 anomaly like this: an RP2040 ~60 kohm
pull-down against the S08's internal pull-up parks the net at ~2 V, which
**the RP2040's Schmitt input reads as HIGH while the S08's VIH
(0.7 x VDD = 2.31 V) reads it as LOW.** The identical divider is present on
BKGD:

- Datasheet Sec 17.2.1: "BKGD is a pseudo-open-drain pin and **there is an
  on-chip pullup**". The HCS08 spec range for that pull-up is roughly
  17.5-52 kohm.
- **With R1 (10 k) fitted:** 3.3 x 60/70 = 2.83 V -> above the target's
  2.31 V VIH. Marginal but passing.
- **With R1 removed (which Finding 61 did, deliberately, as a "negative
  control" that "changed nothing"):** 3.3 x 60/(60+35) ~ **2.08 V — BELOW
  the target's 2.31 V VIH.** Every single measurement in this project would
  still have reported "BKGD is high, 0.00% low", because 2.08 V is a
  comfortable HIGH to the RP2040's own input. **The Pico and the target
  disagree about what "high" means on this wire, and only the Pico's view
  was ever recorded.**

**Why it would break commands while leaving SYNC intact — the exact
symptom this project has been chasing for weeks:**

- **SYNC needs no valid static high at all.** It needs a long driven LOW
  and then *one rising edge*; `bdc_sync` drives an explicit push-pull
  speed-up pulse to 3.3 V, so the edge is real. Everything after that is
  the **target** driving the line (its own 128-cycle low and its own
  speed-up pulse), so the host's pad pull is irrelevant to the answer.
- **A command bit needs the line to be a valid logic HIGH at the target's
  sample instant** (datasheet Fig 17-2: "Ten target BDC clock cycles later,
  the target senses the bit level on the BKGD pin"). `bdc_tx_byte` drives
  the speed-up high for **one PIO cycle (~60 ns) and then releases to
  Hi-Z for the remaining ~12 cycles of the bit** — straight onto the
  pull-down divider, decaying toward ~2.08 V with tau ~1 us. Both real
  implementations found this session do the opposite: **USBDM's
  `bdm_tx1..5` and mztulip's `write_byte()` both hold BKGD driven
  PUSH-PULL HIGH for the whole high portion of every bit and only
  3-state at the end of the byte** (USBDM `MOV #BKGD_HIGH_MASK,DATA_PORT`
  then `BDM_3STATE_ASM` after the 8th bit; mztulip `DDRF |= 0x01` once at
  the top of the byte). The datasheet explicitly blesses this: "Because
  the target does not drive the BKGD pin during the host-to-target
  transmission period, **there is no need to treat the line as an
  open-drain signal during this period**."

This is the first mechanism ever identified in this project that is
**structural** — not opcode-, clock-, shape-, framing- or phase-dependent
— and therefore invisible to every sweep that has been run, and identical
on both chips.

FIX APPLIED: force PUE=1/PDE=0 and DRIVE=12 mA on the BKGD pad, re-applied
after every `Pin()`/PIO rebind. See Finding 70 for the measured result.

### Finding 70 — the BKGD pad fix is deployed and verified, and it did NOT fix the symptom (honest negative)

`firmware/bdc_pio.py` gained `PAD_POLICY` / `set_pad_policy()` / `read_pad()`
/ `apply_pad()`, and `apply_pad()` is now called inside
`make_state_machine`, `make_raw_state_machine`, `make_capture_state_machine`
and `make_capture2_state_machine`, plus in `Bdc.__init__` and after every
`Pin(bkgd, ...)` in `bdc.py`. Default policy: **PUE=1, PDE=0, DRIVE=3
(12 mA)**. New serial commands `{"cmd":"pad","pin":N}` and
`{"cmd":"pad_policy","pue":..,"pde":..,"drive":..}` make it readable and
A/B-switchable at runtime.

Verified on the real Pico: GPIO15 and GPIO14 both now read
`PADS=0x7A  IE=1 DRIVE=3 PUE=1 PDE=0 SCHMITT=1` (was 0x56 / 0x52), and the
setting survives a `sync_locked` (i.e. the re-bind path re-applies it).

**Result: no change.**

```
  sync_locked                         15.84 MHz, pulse 8.08 us, 30/153 hits
  scan_xfer [E4 FF] skip 7.5 win 20   2 samples at all 44 phases (20 trials)
  scan_xfer [FF FF] neg control       2 samples at all 44 phases
  scan_xfer [FF FE] pos control       6 samples (0.938 us) at all 44 phases
```

So the pull-down was a genuine, real defect that had been on the BKGD pin
since day one and nobody had looked — but with R1 (10 k) fitted the divider
sat at ~2.83 V, above the target's 2.31 V VIH, so it was not *the* fault.
**It should stay fixed regardless** (it would have been fatal with R1
removed, which is a configuration this project has deliberately run, and
it makes every future measurement mean what it says). Keep it.

### Finding 71 — this chip's reset loop measured directly: period 42.0 us, low 8.58 us

Passive two-pin capture, `{"cmd":"capture2","drive":"none","trigger":
"reset","sample_count":512,"window_us":2560}` = 2.56 ms continuous at
5 us/sample, 60 complete cycles:

```
  RESET   mean low 8.58 us | mean high 33.42 us | period 42.00 us
  BKGD    0.00% low, 0 transitions across the whole 2.56 ms
```

Three things this settles:

- **`probe_pin`'s "13.6% low" on this chip was an aliasing artifact**, not a
  real duty cycle. The true duty is 8.58/42.0 = 20.4%, close to the first
  chip's. `probe_pin` samples with a MicroPython SIO loop at ~24 us/sample
  against a 42 us period; do not read duty cycles off it. (It is still a
  perfectly good *halted vs running* detector, which is all `bdm_check`
  uses it for.)
- **The `d_from=0, d_to=44` phase scans really do cover a whole period on
  this chip** (42.0 us). There is no uncovered phase. Had the 13.6% figure
  been real the period would have been ~63 us and the scans would have had
  a blind third — that worry is closed by measurement.
- The 8.58 us low reproduces the first chip's exactly, consistent with the
  Sec 2.2.3 reset-source-detection sequence (drive low ~4.25 us, release,
  re-sample ~4.75 us later).

### Finding 72 — MC9S08SG8 has NO BKGDPE and NO RSTPE bit: two hypotheses killed outright

Read from the real datasheet, Sec 5.7.3 Figure 5-4 / Table 5-5. **SOPT1 on
this part is `COPT[7:6] | STOPE[5] | reserved[4] | 0[3] | IICPS[2] | 0 | 0`,
reset value 0xC0.** There is no BKGDPE bit and no RSTPE bit anywhere in the
register map.

Consequences:

- **The BKGD pin's BDM function cannot be turned off by software on this
  part.** The plausible-sounding theory that the wild CPU (running garbage
  out of a blank FLASH) writes SOPT1 and disconnects the BDC partway
  through each 42 us cycle is **impossible**. Dead.
- **The RESET pin is unconditionally a reset pin** — it is not a
  software-selectable PTA5/RESET share on this device, so no configuration
  drift can turn it into a GPIO.

### Finding 73 — the unambiguous readback targets are now known exactly (datasheet-sourced)

The project has never had a read target whose correct answer is neither
0x00 nor 0xFF, so every "successful" read has been ambiguous against a
floating line. Four now exist, all in the high-page (unsecured) register
space that a secured part still allows:

| Addr | Reg | Expected | Source |
|---|---|---|---|
| $1807 | SDIDL | **0x14** exactly | Fig 5-7, "Reset: 0001 0100"; ID hard-coded 0x014 for MC9S08SG8 |
| $1806 | SDIDH | bit7 = **1**, bits 3:0 = **0** (bits 6:4 indeterminate) | Fig 5-6; "Bit 7 is a mask option tie off that is used internally to determine that the device is a MC9S08SG8" |
| $1800 | SRS | **0x82** after a power-on reset (POR=1, LVD=1) | Table 5-3: POR "also set[s] the low-voltage reset (LVR) status bit" |
| $1809 | SPMSC1 | **0x1C** | Fig 5-8, "Reset: 0001 1100" |

This also answers the open question "did the chip swap really happen /
which chip is in the socket": **SDIDL distinguishes an SG8 (0x14) from an
SG4 by part ID**, and the independent confirmation is that mztulip's
`read_target_identifier()` reads $1806/$1807 and tests
`(sdid & 0x0fff) == 0x14` for "MC9S08SG8". (Finding 64 already settles the
swap by DCO and reset period, without needing a working read.)

### Finding 74 — new `{"cmd":"bdm_connect"}`: the whole documented entry-and-identify sequence in one command

`Bdc.bdm_connect()` runs, for each of five
(off_ms, on_reset_hold_ms, bkgd_hold_us) variants —
`(100,10,2000) (250,50,2000) (100,1,200) (500,10,20000) (100,10,100)`;
2000 us matches USBDM's own `BKGD_WAITus`:

1. `power_on_reset_to_bdm` (RESET *and* BKGD held low across the power
   cycle, 12 mA rail drive, rail actively pulled to 0 while off);
2. `bdm_check` — **command-free** pass/fail: has the 42 us reset loop
   stopped (`reset_low_pct < 2%`) or does plain `sync` now hit >= 10/12;
3. only if halted: `sync`, `read_status` (BDCSCR: expect ENBDM=1,
   BDMACT=1, CLKSW=1), then `read_byte` of $1806 / $1807 / $1800, and it
   declares success only when **SDIDL is neither 0x00 nor 0xFF**.

Deployed and exercised end-to-end on the real Pico. With GPIO12
disconnected it correctly reports `{"connected": false}` with all five
variants showing the target still free-running (reset_low_pct 13.6-14.2,
sync_hits 0-4/12) — i.e. the command works, the power pin simply is not
attached to anything.

## Where the SEVENTH session leaves the investigation

**Blocked on exactly one physical change.** Move the target's VDD wire from
the Pico's **3V3(OUT) (physical pin 36)** to **GPIO12 (physical pin 16)**.
Nothing else changes — BKGD stays on GPIO15, RESET on GPIO14, VSS on GND,
R1 and C1 as they are. Then:

```
  {"cmd":"bdm_connect"}      (allow ~3 minutes; it tries 5 timing variants)
```

`{"connected": true}` with `sdidl_1807 = 20` (0x14) is the whole problem
solved. `{"connected": false}` with every variant still showing
`reset_low_pct ~14` means BKGD/MS entry fails even from a genuine,
host-controlled power-on reset — which, given everything else is now
eliminated, would point hard at the parts themselves (same lot, Finding 64).

**Why this specific test and not more protocol work.** Three independent
lines converged on it this session:

1. **The only known BDM programmer written for this exact part**
   (mztulip/hcs08_bdm_programmer, "tested only with this microcontroller")
   treats a **switchable target supply as required hardware** and always
   enters active background mode via a power-on reset with RESET held low
   across the power-up (Finding 63).
2. **USBDM does the same thing** — `bdm_connect()` falls back to
   `bdm_cycleTargetVdd(RESET_SPECIAL)`, which drives BKGD low, powers the
   target on, waits for RESET to rise, holds BKGD low a further
   `BKGD_WAITus = 2000 us`, then releases. Its `bdm_cycleTargetVddOff()`
   explicitly *measures* Vdd and waits until it is below 10% before
   powering back on. That is why commercial pods pop a "please cycle power
   to the target" dialog.
3. **The vendor/industry statement of the problem is this project's exact
   symptom**: a blank MCU generates a cyclic (illegal-opcode /
   illegal-address) reset which prevents the MCU from entering BDM mode,
   and the only way to force a blank device into background debug mode is
   through a power-on reset; once the part has been programmed once, the
   problem disappears.

**Why the earlier POR attempts do not count against this.** The firmware
version cycled a GPIO whose pad drive was the RP2040 default 4 mA and
never held RESET low; the user's manual version (unplug the Pico, press a
BKGD-to-GND button, replug) relied on the **Pico's own 3V3 bulk
capacitance decaying below the target's POR release threshold** — which it
very plausibly never did, giving a brownout/LVD reset instead, and
Sec 17.1.1 lists LVD alongside pin and error resets as sources that
**ignore BKGD**. Both are addressed in Finding 68.

**Still untested, the one remaining host-side axis** (deliberately not
attempted, to avoid destabilising a working rig before handover): both
reference implementations hold BKGD **driven push-pull HIGH for the entire
high portion of every command bit** and 3-state only at the end of the
byte, where `bdc_tx_byte` drives a 1-cycle speed-up and then releases to
Hi-Z for ~12 of 16 cycles. With the Finding 70 pad fix in place (PUE=1)
plus R1 plus the target's own on-chip pull-up there is no longer an
electrical mechanism for this to matter, so it is low-probability — but it
is the last structural difference from known-working code.

**Do not spend more time on** (in addition to everything the sixth session
listed): the host waveform in any dimension; phase (the period is 42.0 us
on this chip and the 0-44 us scans cover it, Finding 71); SOPT1 / BKGDPE /
RSTPE theories (those bits do not exist, Finding 72); the "one defective
chip" framing (both parts share the `5PVS` lot code, Finding 64).

### Sources consulted this session

- MC9S08SG8 MCU Series Data Sheet Rev. 8 (NXP) — read directly as PDF:
  Ch. 5 (SRS/SBDFR/SOPT1/SOPT2/SDIDH/SDIDL/SPMSC1) and Ch. 17
  (Development Support, Figs 17-2/17-3/17-4, Table 17-1, Sec 17.2.2-17.2.3).
- https://github.com/mztulip/hcs08_bdm_programmer — cloned and read in
  full. Written for and tested only on the MC9S08SG8.
- https://github.com/podonoghue/usbdm-firmware — cloned; read
  `USBDM_JMxx_V4_12/Sources/BDM.c` (bdm_connect, bdm_physicalConnect,
  bdm_hardwareReset, bdm_softwareReset, bdm_syncMeasure, bdm_tx1..3) and
  `BDMCommon.c` (bdm_cycleTargetVddOn/Off) and `BDMCommon.h` (timing
  constants).
- NXP AN3335 "Introduction to HCS08 Background Debug Mode".
- NXP Community threads on MC9S08SH8/SE8 BDM connection failure and the
  USBDM "Please cycle power to the target" dialog.
- Chinese-language sources searched (21ic BBS, amobbs HCS08RMv1 translation,
  baidu baike BDM, e-eway): nothing beyond the same standard advice —
  hold BKGD low at the rising edge of reset / power-cycle the target, and
  "do not connect any large capacitance to the BKGD/MS pin".

## EIGHTH session (2026-09-15) — VDD is on GPIO12 at last; a genuinely NEW target state

Wiring now: VDD = GPIO12 (switchable), BKGD = GPIO15, RESET = GPIO14,
VSS = GND, R1 10k BKGD->VDD, C1 0.1uF VDD-VSS. User measured VDD live with
a multimeter during a `bdm_connect` run: steady **3.19-3.20 V** throughout,
including the power-on transient — so supply sag is ruled out by direct
measurement, not inference.

### Finding 75 — the new "stopped" state is REAL and target-driven, not a host artifact

Reproduced and then attacked with two hard controls. All measured this
session:

```
state S (after power-on via GPIO12):
  probe_pin(14)  low 0.00%   transitions 0      <- reset loop GONE
  probe_pin(15)  low 0.00%   transitions 0
  sync x8        13.92 13.84 13.84 13.84 13.84 13.84 13.92 13.84 us  (8/8)
  bdm_check      sync_hits 12/12, reset_low_pct 0.0

CONTROL A — RESET held LOW by the Pico, then sync x8:
  sync x8        None x8        <- BDC dead while RESET asserted (Finding 51)
  after release  probe14 low 1.5%, 38 transitions
  sync x8        8.08 8.08 6.88 8.08 8.08 6.88 8.08 8.08 us
                 ^^^^ back to the FREE-RUNNING 15.84 MHz clock

CONTROL B — target power OFF (drive_pin 12 low), then sync x8:
  probe14 / probe15  100.0% low  (both rails dead; R1 drags BKGD to 0 V)
  rise_time(15)      -1 x4       (open-circuit signature)
  sync x8            None x8     <- NOTHING answers when unpowered
  power back ON      sync x8 = 13.84-13.92 us again, probe14 0.0%
```

Three things this nails down:

- **The 13.84 us SYNC answer is genuinely coming from the target.** It
  vanishes when the target is unpowered and it vanishes when RESET is
  asserted. It is not the pull-up, not self-triggering, not an artifact.
- **The reset loop really has stopped** and the state is stable and
  reproducible across power cycles.
- **A RESET-pin pulse puts the chip BACK into the free-running 15.84 MHz
  state**, and a power cycle puts it back into the 9.25 MHz stopped state.
  The two states are cleanly switchable, on demand, by choice of reset
  source. This is the first time in the whole project that the target has
  had two distinguishable operating states.

### Finding 76 — the stopped state does NOT require BKGD to be held low, so it is probably NOT BKGD/MS active-BDM entry

Control B's power cycle was a bare `drive_pin(12,0)` / `drive_pin(12,1)`
with **BKGD left Hi-Z the whole time** — pad policy PUE=1, so the RP2040's
own ~50 k internal pull-up held BKGD at 3.3 V while VDD ramped, i.e. BKGD
was unambiguously **HIGH** at the moment the chip exited power-on reset.
That is the opposite of the documented BDM-entry condition (Sec 17.1.1
wants BKGD LOW as the device exits POR) — and the stopped state appeared
anyway, identically.

So "the reset loop stopped" cannot be attributed to active background mode
being latched. Something else about a power-on reset (as opposed to a
pin reset) stops this chip. Do not assume state S == active BDM.

### Finding 77 — ***THE TARGET EXECUTED A BDC COMMAND.*** ACK_ENABLE draws a real, target-driven ACK pulse in state S

First time in this project's history. `{"cmd":"capture","test":"xfer",...}`
in state S, after `sync()` calibrated the bit timing to the state-S clock
(9 248 555 Hz, 1.73 us/bit), 256 samples at 234 ns. Run-length decode of
BKGD (`L`/`H` = low/high run in us):

```
READ_STATUS 0xE4  L0.23 H1.40 L0.47 H1.40 L0.23 H1.40 L1.40 H0.23 L1.40
                  H0.47 L0.23 H1.40 L1.40 H0.47 L1.40 H46.33
                  = 1 1 1 0 0 1 0 0 = 0xE4, then NOTHING

BACKGROUND  0x90  L0.47 H1.40 L1.40 H0.23 L1.40 H0.23 L0.47 H1.40 L1.40
                  H0.23 L1.40 H0.23 L1.40 H0.47 L1.40 H46.33
                  = 1 0 0 1 0 0 0 0 = 0x90, then NOTHING

ACK_ENABLE  0xD5  L0.23 H1.40 L0.47 H1.40 L1.40 H0.23 L0.47 H1.40 L1.40
                  H0.23 L0.23 H1.40 L1.40 H0.47 L0.23
                  = 1 1 0 1 0 1 0 1 = 0xD5, then
                  H5.38 **L1.87** H40.25
                        ^^^^^^^^
```

**That `L1.87` is not ours.** The host's longest possible low is 13 BDC
cycles = 1.40 us at this clock, and every one of our eight command bits is
visible immediately before it at exactly 0.23 us ('1') or 1.40 us ('0').
A 1.87 us low, 5.38 us after the last command bit, with no host drive in
that interval, can only be the target.

**And it is exactly the right pulse in exactly the right place.** RM
Sec 7.3.5 / Fig 7-6: the ACK is a **16-BDC-cycle** low pulse issued after
the command, and `ACK_ENABLE` is the one command that is acknowledged even
while acknowledgement is otherwise off. 16 cycles at 9.2486 MHz =
**1.73 us**; measured 1.87 us (within one 234 ns sample). The two commands
captured in the same run that are NOT self-acknowledging drew nothing, in
the same session, at the same bit rate, with the same detector — a live
negative control on the same run.

This retires the central mystery of the last five sessions. The BDC
command decoder **does** work, the host's waveform **is** decodable by the
target, and the reason nothing ever worked before was the target's
state, not the protocol: every prior attempt was made against a chip
free-running in a 42 us illegal-opcode reset loop.

## NINTH session (2026-09-15) — the read phase: the SM handover was the bug

Picks up exactly where the EIGHTH session was cut off mid-sentence
("One SM can't hand the pin to the other. Let me restructure to a single
contiguous transaction.").

State at session start: firmware on the Pico byte-identical to the repo
(bdc.py / bdc_pio.py / main.py sha256 match), which means the EIGHTH
session's last edit — `bdc_rx_bits`' read marker widened from a 1-cycle
low to a 3-cycle low — **was** deployed before the agent died.

### Finding 78 — the 3-cycle read marker did NOT fix reads. Every read is still 0xFF, at every address (honest negative)

`bdc_pio.py`'s `bdc_rx_bits` docstring, written by the EIGHTH session just
before it was killed, claims the 1-cycle read marker was "the reason
**every** read in this project's history returned 0xFF/0x00". Re-measured
today with that fix live on the hardware, in state S, synced at
9 195 402 Hz:

```
read_status x8        -> 0xFF 0xFF 0xFF 0xFF 0xFF 0xFF 0xFF 0xFF
read_byte $1807 x4    -> 0xFF   (SDIDL, must be 0x14)
read_byte $1806 x4    -> 0xFF   (SDIDH)
read_byte $1800 x4    -> 0xFF   (SRS, must be 0x82 after POR)
read_byte $1802/$1808/$1809 -> 0xFF
$1800..$180F          -> FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF
$0060..$006F  (RAM)   -> FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF
$FFFE/$FFFF           -> 0xFF
```

Address-invariant across register space, RAM and the reset vector. By this
project's own long-standing bar (CONTEXT.md, Finding 12 onward:
address-invariant = pull-up artifact, address-varying = real), **the
separate-RX-state-machine read path reads nothing at all.** The marker
width was not the cause. That docstring claim is retracted — it was
written from a plausible datasheet quote, never measured.

### Finding 79 — ***A CONTIGUOUS TRANSACTION DRAWS REAL DATA; A TRANSACTION WITH AN SM HANDOVER DRAWS NOTHING.*** Same command, same bit rate, same session.

The decisive experiment. A `0xFF` byte pushed through `bdc_tx_byte` is
bit-for-bit **identical to a BDC read marker** (3 cycles low, release, a
13-cycle window where the target may answer). So `[0xE4, 0xFF]` sent as
two words into the same TX FIFO is a complete READ_STATUS transaction —
command *and* read phase — from a **single state machine**, with no
`active(0)` / `active(1)` handover and no Python in between. Compare with
`raw_xfer(tx=[0xE4], nbits=8)`, which is the identical waveform on paper
but hands the pin from the TX SM to the RX SM in between.

Captured with the read-only scope SM, decoded by hand on the bit grid
(trigger = first falling edge; bit k = k x 16 target cycles; sample point
= cycle 10 of each bit), state S, 9 195 402 Hz, 132 ns/sample:

```
                        <---- our 8 command bits ---->  <-- our 8 markers -->
   bit                   0    1    2    3    4    5    6    7    8    9   10   11
[E4 FF] #0  low cycles  2.4  3.6  4.9 12.1 12.1  2.4 13.4 13.4  2.4  2.4 13.4  6.1
            sampled      1    1    1    0    0    1    0    0    1    1  *0*   1
[E4 FF] #1  low cycles  2.4  3.6  4.9 12.1 12.1  2.4 13.4 13.4  2.4  2.4 13.4 12.1
            sampled      1    1    1    0    0    1    0    0    1    1  *0*  *0*
[E4 FF] #2/#3 identical to #0
            byte0 = 0xE4  (our command, echoed back by our own drive)

NEGATIVE CONTROLS, same run, same bit rate, same decoder:
[FF FF] #0  low cycles  2.4  2.4  4.9  2.4  2.4  2.4  2.4  3.6  2.4  2.4  2.4  3.6
[90 FF] #0  low cycles  2.4 13.4 13.4  2.4 12.1 13.4 13.4 13.4  2.4  3.6  3.6  3.6
[D5 FF] #0  low cycles  3.6  3.6 12.1  2.4 13.4  3.6 13.4  3.6  2.4  2.4  3.6  3.6
            -> in EVERY control, marker bits 8..11 are a clean 2.4-3.6
               cycles low.  Nothing but our own 3-cycle marker.

SEPARATE-SM control, same session, [0xE4] + rx phase (gap_us 15-45):
  L0.23 H1.40 L0.47 H1.40 L0.23 H1.40 L1.40 H0.47 L1.40 H0.23
  L0.47 H1.40 L1.40 H0.23 L1.40 H46.33      <- 46 us of dead silence
  value = 0xFF
```

**Marker bit 10 of `[E4 FF]` is held low for 13.4 target cycles.** Our own
marker is 3 cycles and is over by cycle 3; nothing on the host drives the
line for the remaining 13 cycles of that window. 13.4 cycles low, sampled
at cycle 10 = 0, in the exact bit slot the protocol reserves for the
target's answer, reproducible across 4 consecutive runs, and **absent in
three different negative controls in the same run** — one of which
(`[FF FF]`) is the same 16-bit waveform with no command in front of it.

So: the target answers a READ_STATUS *only* when the read markers arrive
with no state-machine handover in front of them. That is the entire
explanation for Finding 78 and, very probably, for every 0xFF this project
has ever read.

**Not yet established** (do not over-read this): bits 12..15 of that
capture fall outside the scope SM's stall-free depth (the RX FIFO is 4
words = 128 samples; at 132 ns/sample only the first ~17 us = ~9.7 bits
are guaranteed contiguous in time), so the *value* of BDCSCR is NOT
measured yet — only that a real target-driven data bit exists. The full
byte, and address-varying READ_BYTE, are the next step.

### Finding 80 — ***THE FIRST REAL READ IN THE PROJECT'S HISTORY.*** READ_STATUS returns 0xD6, reproducibly, against 0xFF on every control

The single-state-machine rewrite, deployed and measured. `bdc_tx_byte`
now **samples BKGD at cycle 10 of every bit it transmits** and autopushes
one sampled byte per transmitted byte, and `bdc_rx_bits` is deleted. A
0xFF byte's bit shape is exactly the BDC read-bit marker, so
`[command..., 0xFF]` in one FIFO burst is the command *and* its read
phase on one state machine, with no handover and no Python between them
(`bdc.py::_xfer`).

The sampled bytes taken while WE are driving come back too, and they must
equal what we sent ('1' released by cycle 4 -> samples high, '0' still
driven at cycle 10 -> samples low). That is a free per-transaction
alignment check; every result below passed it.

```
state S, sync = 9 248 555 Hz

STEP 1 -- alignment/echo, no read phase.  ALL PASS:
  sent E4 -> echo E4      sent 00 -> echo 00     sent AA -> echo AA
  sent 90 -> echo 90      sent FF -> echo FF     sent 55 -> echo 55
  sent D5 -> echo D5      sent E0 18 07 -> echo E0 18 07

STEP 2 -- read phases, 3 trials each, echo OK on all:
  (no command), 8 markers   -> 0xFF 0xFF 0xFF     <- negative control
  0xFF then 8 markers       -> 0xFF 0xFF 0xFF     <- negative control
  ACK_ENABLE (D5) + markers -> 0xFF 0xFF 0xFF     <- negative control
  BACKGROUND (90) + markers -> 0xFF 0xFF 0xFF     <- negative control
  READ_STATUS (E4)+ markers -> **0xD6 0xD6 0xD6**
```

**0xD6, three times out of three, where four different controls in the
same run all return the pull-up's 0xFF.** And it cross-checks against a
completely independent measurement path: the hand-decoded scope capture
of Finding 79 read marker bits 0,1,2 as `1 1 0`, and 0xD6 = `1101 0110`
begins `1 1 0`. Two different instruments, one answer.

Decoded as BDCSCR (Sec 17.4.1: ENBDM BDMACT BKPTEN FTS CLKSW WS WSF DVF):

```
0xD6 = 1   1      0      1   0     1  1   0
       ENBDM=1  BDMACT=1  BKPTEN=0  FTS=1
       CLKSW=0  WS=1      WSF=1     DVF=0
```

`WS=1` — the target reports itself **in wait or stop mode**. That is a
self-consistent explanation for "state S": the chip is not running, which
is exactly why the 42 us illegal-opcode reset loop of every prior session
is absent. `WSF=1` says the last command **failed because of that**.

### Finding 81 — READ_BYTE still returns 0xFF at every address. The likely cause is the missing 16-cycle command delay, NOT a fake read

Same run, same session, echo check passing on every one:

```
  $1806 $1807 $1800 $1802 $1809 $0060 $FFFE $FFFF  -> all 0xFF (x3 each)
  $1800..$180F  -> FF x16
  $0060..$006F  -> FF x16
  $FFB0..$FFBF  -> FF x16
```

So READ_STATUS answers and READ_BYTE does not. Two candidate causes, both
testable and neither yet settled:

1. **Missing delay.** The BDC command table writes READ_BYTE as
   `E0 / AAAA / d / RD` — a delay `d` between the last address bit and the
   first data bit, while the target performs the memory cycle. READ_STATUS
   is `E4 / RD` with no `d`, which is precisely the command that works.
   Our marker byte currently starts one bit time after the last address
   bit, i.e. d = 0. This is the leading hypothesis.
2. **WSF=1 / WS=1.** BDCSCR says the target is in wait or stop mode and
   that a command already failed for that reason. Memory-access commands
   are exactly the ones that fail in that state, while status reads do
   not.

Both would produce the observed pattern. Next: insert the delay, and
separately try to leave wait/stop.

### Finding 82 — ***WRITES WORK.*** BDCSCR readback tracks four different values we chose, bit for bit

The test no artifact can pass: write a value *we* pick into BDCSCR with
WRITE_CONTROL (0xC4), then READ_STATUS it back. BDCSCR's writable bits are
ENBDM(7), BKPTEN(5), FTS(4), CLKSW(3); BDMACT/WS/WSF/DVF are read-only
status. CLKSW was deliberately never set — it reselects the BDC clock
source and would invalidate the bit timing underneath us.

```
state S, sync 9 248 555 Hz, echo check passed on every transfer

baseline                 -> 0xCC  ENBDM BDMACT CLKSW WS
write 0x80  (ENBDM)      -> 0xC0  ENBDM BDMACT
write 0xA0  (+BKPTEN)    -> 0xE0  ENBDM BDMACT BKPTEN
write 0x90  (+FTS)       -> 0xD0  ENBDM BDMACT FTS
write 0xB0  (+BKPTEN+FTS)-> 0xF0  ENBDM BDMACT BKPTEN FTS
write 0x80               -> 0xC0  ENBDM BDMACT
write 0x00               -> 0xC0  (ENBDM stays set — it is documented as
                                   not clearable from inside active BDM)
write 0xA0               -> 0xE0
  then READ_STATUS x5    -> 0xE0 0xE0 0xE0 0xE0 0xE0   (stable, no drift)
  then 3 plain reads     -> 0xE0 0xE0 0xE0             (reads don't disturb)
```

Every writable bit went exactly where it was put, four distinct values,
each one predicted before the run. **The host can now read AND write this
target's BDC controller.** Note the baseline also moved (0xD6 last run,
0xCC this run — CLKSW and WS/WSF differ), which is itself evidence the
byte is live target state and not a constant.

### Finding 83 — ***READ_BYTE WORKS, and the missing piece was the command delay `d`.*** Datasheet-exact, address-varying values

Finding 81's hypothesis 1 was right. The BDC table's `E0 / AAAA / d / RD`
delay is real and we had been sending d = 0. With one extra marker byte
between the address and the data phase (`[E0 AH AL FF FF]`, so d = 8 bit
times, far more than the 16 cycles the table asks for):

```
addr    what it is                d=0 read     d=8 read    datasheet says
$1807   SDIDL                     14           **14**      0x14  MATCH
$1806   SDIDH                     14 (stale)   **A0**      REV|ID hi nibble
$1800   SRS   after POR           A0 (stale)   **82**      0x82  MATCH
$0060   RAM                       82 (stale)   **00**      -
$FFFE   reset vector hi           00 (stale)   **FF**      blank FLASH
```

Two things at once here.

- **The d = 8 column is real data.** SDIDL = 0x14 and SRS = 0x82 are the
  exact values Finding 73 nominated as the unambiguous readback targets,
  arrived at independently. SDIDH = 0xA0 completes the part ID as
  0x_14 = MC9S08SG8/SG4, matching the chip identity confirmed in the SIXTH
  session. $FFFE = 0xFF is a blank FLASH reset vector, which is exactly
  what a chip stuck in an illegal-opcode reset loop should have. And the
  values **vary by address** — this project's own standing bar for real
  vs. pull-up artifact, cleared at last.
- **The d = 0 column is the *previous* transaction's data, lagged by
  exactly one.** $1806 returns $1807's value, $1800 returns $1806's,
  $0060 returns $1800's, $FFFE returns $0060's. Perfect lag-by-one down
  the whole column. That is the classic BDC symptom of reading the shift
  register before the target's memory cycle has refilled it — and it is a
  much better explanation of Finding 81 than anything about the target's
  state.

CAVEAT, and it is a real one: a 5-byte burst does not fit the 4-word TX
FIFO, so `_xfer_long` tops the FIFO up from Python mid-burst, and its
underrun flag fired on every one of these transfers. A later repeat of the
same reads (after a BACKGROUND) came back lagged again rather than
correct, so the long path is NOT yet reliable. The values above are real —
they are too specific and too datasheet-exact to be anything else — but
the transport that produced them still has a hole in it. Fixing that is
the next step, not a formality.

### Finding 84 — the last bug was OUR OWN SPEED-UP PULSE colliding with the target's data

Between Finding 83 and here, reads went bad: READ_STATUS drifted
0xD5/0xD6/0xCD/0xCC run to run instead of holding one value, and every
READ_BYTE came back as alternating-bit mush (0x55 0xAA 0x52 0x25 0x4A ...).

Two false leads were chased and both are now ruled out **by measurement**,
so nobody repeats them:

- *Not* the 4-bytes-per-FIFO-word PIO rewrite. Reverting it byte for byte
  to the program that produced Finding 80 did not fix the mush. (The
  rewrite was a real regression for a different reason and is documented
  in `bdc_pio.py`; it is reverted.)
- *Not* the target falling out of state S. Measured directly:
  probe_pin(14) and probe_pin(15) both 0.00% low / 0 transitions, SYNC
  13.84-13.92 us, exactly Finding 75's signature. The chip was fine.

The scope capture of a live READ_STATUS showed it. Run-length of BKGD
through the read markers:

```
   ... our 8 command bits (0xE4) ... then the markers:
   L0.26 H1.45   marker 0, nothing in the window        -> data 1
   L0.26 H1.45   marker 1, nothing in the window        -> data 1
   L0.26 H0.13 L1.06 H0.26     <-- marker 2
         ^^^^^ 132 ns of HIGH inside the target's low
```

`bdc_tx_byte`'s '1' bit ended with one cycle of actively driven HIGH — the
speed-up pulse added in the SEVENTH session — before releasing to Hi-Z. A
'1' bit and a read marker are the *same waveform*, so during every read
phase that pulse drove the RP2040's 12 mA output HIGH into the target's
data-'0' low. The capture shows the target's low being interrupted by our
spike and then resuming.

The dead `bdc_rx_bits` had a comment saying exactly this would happen
("driving high into it would be real contention") and it was right; the
program that replaced it inherited the pulse from the transmit path and
nobody noticed the two waveforms were now the same one.

Fix: the '0' path keeps its speed-up (it is the one that needs it — back
to back '0' bits get only 3 cycles of high between them), the '1'/marker
path does not. A '1' has 13 cycles of high and the 10k pull-up's ~300 ns
rise is done by cycle 3 of them, long before the target samples.

### Finding 85 — ***REAL, VERIFIED, ADDRESS-VARYING, ORDER-INDEPENDENT READS.*** The thing this project has been trying to do since 2026-09-12

One-line change from Finding 84, deployed, first run:

```
state S, sync 9 248 555 Hz, every transfer's echo check passing

READ_STATUS x4        -> C8 C8 C8 C8      (was drifting; now rock steady)

addr    register   4 trials        datasheet
$1807   SDIDL      14 14 14 14     0x14            MATCH
$1806   SDIDH      A0 A0 A0 A0     REV|ID hi       MATCH (part ID 0x_14)
$1800   SRS        82 82 82 82     0x82 after POR  MATCH
$1802   SOPT1      C0 C0 C0 C0     0xC0            MATCH
$1809   SPMSC1     1C 1C 1C 1C     0x1C            MATCH  <- Finding 73
$0060   RAM        00 00 00 00

$1800..$180F   82 00 C0 00 12 00 A0 14 00 1C 00 00 00 00 00 00
same range read in REVERSED address order, printed in address order:
               82 00 C0 00 12 00 A0 14 00 1C 00 00 00 00 00 00
               ^^ byte-for-byte identical. Order-independent.

$FFB0..$FFBF   FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FE
$FFFE/$FFFF    FF FF        (blank FLASH reset vector)
```

Every bar this project set for itself, cleared in one run:

- **Address-varying**, not the pull-up's constant 0xFF.
- **Datasheet-exact** on five independent registers, including the two
  Finding 73 nominated in advance as unambiguous.
- **Repeatable** — four identical trials each.
- **Order-independent** — reading $1800..$180F backwards gives the
  identical picture, so no value is an echo of the previous transaction
  (the exact failure mode of Finding 83).

And a bonus that falls straight out: **$FFBF = 0xFE is NVOPT**, whose
SEC[1:0] = 10 means **the chip is UNSECURED**. That retires the security
question the FIFTH and SIXTH sessions could not settle.

Note the reset vector at $FFFE/$FFFF reads 0xFFFF — blank FLASH — which is
precisely why this chip free-runs in an illegal-opcode reset loop whenever
it is not stopped. The whole story is now consistent end to end.

### Finding 86 — ***WRITES VERIFIED TOO.*** WRITE_BKPT / READ_BKPT round-trips six 16-bit values, 6/6

`WRITE_BKPT` (C2/WBKP) and `READ_BKPT` (E2/RBKP) are a clean write/read
pair: a 16-bit register with no side effects, no memory cycle, and no
delay `d` in Table 17-1, so neither command can be blamed on timing.

```
state S, health-checked before each write (READ_STATUS != 0xFF and
$1807 == 0x14), sync 9 248 555 Hz

wrote 0x8000 -> read 0x8000   OK
wrote 0x0080 -> read 0x0080   OK
wrote 0x1234 -> read 0x1234   OK
wrote 0xABCD -> read 0xABCD   OK
wrote 0x7F7F -> read 0x7F7F   OK
wrote 0xFFFF -> read 0xFFFF   OK
```

Six arbitrary 16-bit values, chosen by the host, each recovered exactly.
Together with Finding 85 this is a **working bidirectional BDC link**.

### Finding 87 — writing BDCSCR with CLKSW=0 kills the link, every time. Preserve CLKSW.

Every `WRITE_CONTROL` in the run above was followed by READ_STATUS =
0xFF and a target that had to be re-entered:

```
   wrote 0x80 -> status 0xFF      [re-entered: 0xC8]
   wrote 0xA0 -> status 0xFF      [re-entered: 0xC8]
   wrote 0x90 -> status 0xFF      [re-entered: 0xC8]
   wrote 0x00 -> status 0xFF      [re-entered: 0xC8]
   wrote 0xB0 -> status 0xFF
```

The resting BDCSCR in state S is **0xC8 = ENBDM | BDMACT | CLKSW**, and
every value written above has CLKSW = 0. CLKSW selects the BDC clock
source, so clearing it changes the target's BDC clock out from under a
bit rate that was calibrated by SYNC against the old one — after which
nothing decodes and everything reads as the pull-up's 0xFF. It is not
damage and it is not a lost state: a power-on re-entry brings it back
every time.

This also revises Finding 82. Those writes were real — the readbacks
tracked them bit for bit — but they were also silently switching the BDC
clock, which is very likely why the session's results got flaky
immediately afterwards. **Any BDCSCR write must OR the current CLKSW back
in**, or re-run SYNC straight afterwards.

### Finding 88 — WRITE_BYTE loses the data byte's MSB, sometimes. Open, and narrowed.

Reads are exact; WRITE_BKPT is exact; WRITE_BYTE to RAM is not:

```
$0060 <- 0x00 -> 0x00 OK     $0060 <- 0x80 -> 0x00   xor 0x80
$0060 <- 0x01 -> 0x01 OK     $0060 <- 0x81 -> 0x01   xor 0x80
$0060 <- 0x7F -> 0x7F OK     $0060 <- 0xFF -> 0x7F   xor 0x80
$0060 <- 0x55 -> 0x55 OK     $0060 <- 0xAA -> 0x2A   xor 0x80
full sweep of all 256 values at $0060: 128/256 correct — exactly the
128 values with bit 7 clear. The XOR is 0x80 every single time.
```

What is known:

- It is **not our waveform**. The per-transaction echo check (the samples
  taken at cycle 10 of every bit we drive) passes on every one of these:
  we transmitted bit 24 as a '1' and read the line back high ourselves.
- It is **not the byte position in general**. WRITE_BKPT puts an MSB in
  byte 1 and byte 2 and both survive (0x8000 and 0x0080 above).
- It is **not universal**: `$00E0 <- 0x80` read back 0x80 correctly in the
  same run where `$0060 <- 0x80` read back 0x00.
- `WRITE_BYTE_WS` (C1) behaves identically, so it is not specific to C0.

Leading hypothesis, consistent with all of it: **rise time at the
target's VIH, on bit 24 only.** Finding 84 removed the host speed-up pulse
from '1' bits because it collided with target data during read markers —
but a '1' bit and a read marker are the same waveform, so transmit bits
lost their fast edge too. A '1' now relies on the 10k pull-up: ~300 ns
per time constant, and the target samples about 650 ns after release, at
VIH = 0.7 x VDD = 2.31 V. That is enough margin most of the time and not
always, and it would be worst on a bit that follows a long run of '0's
(six of them, in `C0 00 60`) where the line has had only 3-cycle high
excursions to recover in.

If that is right, the fix is to restore the speed-up pulse for
*transmitted* bits while keeping it off *read markers* — which needs the
PIO program to be told which is which, since today they are the same code
path. That is the next piece of work, and it is a known problem with a
known shape rather than a mystery.

### Finding 89 — correction to Finding 88: it is not an MSB transport bug, it is RAM

More data, and it changes the diagnosis. Writing 0xFF to $0060..$006F and
reading straight back:

```
   7F 00 00 FF FF 7C FF FF 7C FF FF 00 7F 00 FF 00
```

Seven of the sixteen cells took 0xFF exactly. The rest lost bit 7 (0x7F),
lost bits 7/1/0 (0x7C), or lost everything (0x00). That is not "the MSB
of the last transmitted byte", which was the Finding 88 story, and it is
not a single-bit transport fault at all.

It is also **per-address deterministic**: `$0060 <- 0x80` read back 0x00
on 20 consecutive attempts, under four different surrounding sequences
(plain, cleared first, written twice, with a READ_STATUS in between) —
0/20 every time — while `$0063` and `$0064` take 0xFF perfectly in the
same run.

And the transport is provably fine underneath it: WRITE_BKPT round-trips
0x8000, 0x0080, 0xABCD, 0x5AA5 and 0xFFFF exactly (Finding 86, and 7/7 in
Finding 90 below), which exercises the same command shape and the same
MSB-set bytes.

So the open question is no longer "does the host transmit correctly" but
"why does this chip's RAM not hold some bits at some addresses in this
state". Candidates for a future session, in order of cheapness:

1. The RAM is not fully available in state S. The chip reached this state
   through a power-on reset that stopped it dead rather than through a
   normal BDM entry, and Finding 76 already warns that state S is
   **not confirmed to be textbook active BDM**. BDCSCR reporting
   BDMACT=1 does not settle it.
2. The CPU is still executing something that clobbers low RAM.
3. A genuine RAM fault in this particular chip — cheap to test, there is
   a second chip (see the memory note from 2026-09-14).

None of this blocks the read path, which is exact everywhere it was
tested, including FLASH and the register file.

### Finding 90 — ***END TO END, FROM COLD, TWICE, THROUGH THE SHIPPING API***

Not raw transfers — `power_on_reset_to_bdm` / `sync` / `read_status` /
`read_byte`, the actual driver entry points, run twice from a cold start
with a full power cycle in between. Both runs byte-identical:

```
power_on_reset_to_bdm  ok
sync                   9 248 555 Hz
read_status            0xC8

read_byte through the shipping path (READ_BYTE then READ_LAST):
   $1807 SDIDL   = 0x14   expect 0x14   OK
   $1806 SDIDH   = 0xA0   expect 0xA0   OK
   $1800 SRS     = 0x82   expect 0x82   OK
   $1802 SOPT1   = 0xC0   expect 0xC0   OK
   $1809 SPMSC1  = 0x1C   expect 0x1C   OK
   $FFBF NVOPT   = 0xFE   expect 0xFE   OK
read_status x6         C8 C8 C8 C8 C8 C8

write_control, CLKSW preserved:
   wrote 0xA8 -> status 0xE8   alive
   wrote 0x88 -> status 0xC8   alive
   wrote 0x98 -> status 0xD8   alive

WRITE_BKPT / READ_BKPT:
   8000 0080 1234 ABCD 5AA5 FFFF 0000  ->  7/7 exact

read_byte $1807 after all of the above  ->  0x14
```

**The link is real, bidirectional, repeatable and reproducible from a
cold start.** After six sessions in which no BDC command ever executed,
this one ends with a target that identifies itself, reports its reset
source, hands back its security byte, and accepts and returns 16-bit
values the host chose.

### Finding 91 — `sm.exec("...")` costs 9 ms a call; pre-encode it

`_xfer` ends by explicitly releasing BKGD (Finding: a transaction that
stops at cycle 10 of its last bit can otherwise freeze `pindirs` mid-bit
and leave the line driven low for milliseconds, which is longer than the
128 cycles that define a SYNC request). That release was written as
`sm.exec("set(pindirs, 0)")`.

MicroPython **assembles that string on every call**. Measured on this
board, `raw_xfer` reported:

```
   tx_us  9206     <- with sm.exec("set(pindirs, 0)")
   tx_us  826 822 805 822 792   <- with rp2.asm_pio_encode() done at import
```

9.2 ms of assembler around 30 us of signal, on every single transfer.
Invisible while poking at a handful of registers; it would have made
FLASH programming (tens of thousands of transfers) take tens of minutes
and looked like a hardware problem. `bdc_pio.SET_PINDIRS_IN` is now
encoded once at import.

With that in, `read_block($1800, 16)` over the JSON protocol returns

```
   82 00 C0 00 12 00 A0 14 00 1C 00 00 00 00 00 00
```

in 0.05 s — byte for byte the Finding 85 picture, through the shipping
block-read path.

## Where the NINTH session leaves the investigation

WORKING, VERIFIED ON HARDWARE:

- Entry: `power_on_reset_to_bdm` on GPIO12 into state S, then `sync()`.
  Reproducible on demand; Finding 75's probe signature confirms it.
- `read_status` — stable 0xC8.
- `read_byte(addr)` — exact at every address tested, in register space,
  RAM and FLASH; address-varying and order-independent.
- `write_control` — writable bits track exactly, with CLKSW preserved.
- `WRITE_BKPT`/`READ_BKPT` — 7/7 exact 16-bit round trips.

THE THREE THINGS THAT MADE IT WORK, all in `bdc_pio.py`/`bdc.py`:

1. **One state machine for the whole transaction.** The read phase is
   0xFF marker bytes in the same FIFO burst as the command, and
   `bdc_tx_byte` samples BKGD at cycle 10 of every bit it sends. Handing
   the pin to a second SM (Finding 79) or letting Python top up the FIFO
   mid-burst (Finding 83) both read nothing but 0xFF.
2. **No host speed-up pulse on '1' bits.** A '1' bit and a read marker
   are the same waveform, so that pulse was driving 12 mA of RP2040 into
   the target's data-'0' low. The '0' path keeps its pulse (Finding 84).
3. **READ_LAST for memory reads.** READ_BYTE is `E0/AAAA/d/RD` and the
   delay `d` is not optional — skipping it returns the previous read's
   data with no error at all. `E8/SS/RD` has no `d` because its status
   byte is the delay (Finding 83).

OPEN, IN PRIORITY ORDER:

1. **RAM writes are unreliable at some addresses** (Finding 89). Start by
   deciding whether state S is really active BDM — Finding 76 says it is
   not established — and try the second chip.
2. **CPU register reads** (READ_A/READ_PC/READ_HX/READ_SP) were only
   tested in a run where the link had already dropped, so they are
   UNTESTED, not failing. Re-run them; they are Active-BDM commands and
   take a delay `d`, so they need `_xfer_read(..., delay=1)`.
3. **FLASH programming** is now unblocked in principle but untouched.
4. The host Flask app has not been run against any of this; everything
   here was done over the raw serial JSON protocol.

## TENTH session (2026-09-15) — writes, registers, and FLASH

Picks up from the NINTH session with the link intact. First act of the
session was to reproduce Finding 90 cold, and it reproduced exactly:

```
sync 9 248 555 Hz, status 0xC8
$1807 SDIDL 0x14 | $1806 SDIDH 0xA0 | $1800 SRS 0x82
$1802 SOPT1 0xC0 | $1809 SPMSC1 0x1C | $FFBF NVOPT 0xFE
raw_xfer tx_us = 836        (Finding 91's pre-encoded exec is live)
read_block($1800,16) in 0.050 s
20 x read_byte in 0.345 s -> 17 ms each, nearly all of it USB round trip
```

17 ms per host-driven byte is the number that matters for FLASH: a
192-byte image driven byte-by-byte from the PC would be ~30 s of serial
latency, so bulk work has to run inside the Pico (`flash_write_region`),
not as a Python loop over the JSON protocol.

A general `eval` / `exec` escape hatch was added to `firmware/main.py`
this session. Every experiment before it needed a new JSON verb, a
redeploy and a reset — about a minute of turnaround for a one-line
question. It only widens a USB link that can already erase the chip.

### Finding 92 — ***the RAM write fault is NOT a transport bug. It is a fixed per-address AND mask, confined to exactly $0060-$006F.***

Finding 89 left "why does this chip's RAM not hold some bits" open, with
the NINTH session's transport bugs as live suspects. Settled now, by the
test that separates the two: write **five different patterns** to the same
cell and see whether the surviving bits depend on the data.

```
addr   wFF->  wAA->  w55->  w0F->  wF0->   implied mask
$0060   7F     2A     55     0F     70     0x7F
$0061   00     00     00     00     00     0x00
$0062   00     00     00     00     00     0x00
$0063   FF     AA     55     0F     F0     0xFF   (perfect)
$0064   FF     AA     55     0F     F0     0xFF   (perfect)
$0065   7C     28     54     0C     70     0x7C
$0066   FF     AA     55     0F     F0     0xFF   (perfect)
$0067   FF     AA     55     0F     F0     0xFF   (perfect)
$0068   7C     28     54     0C     70     0x7C
$0069   FF     AA     55     0F     F0     0xFF   (perfect)
$006A   FF     AA     55     0F     F0     0xFF   (perfect)
$006B   00     00     00     00     00     0x00
$006C   7F     2A     55     0F     70     0x7F
$006D   00     00     00     00     00     0x00
$006E   FF     AA     55     0F     F0     0xFF   (perfect)
$006F   00     00     00     00     00     0x00
```

Every readback is exactly `written & mask` with **one mask per address,
identical across all five patterns**. A transport fault corrupts
*patterns* (Finding 88's "the MSB of the last byte"); this corrupts
*cells*. Finding 88's rise-time-at-VIH hypothesis is therefore retired —
it cannot explain why 0x55 survives at $0060 while 0xF0 loses exactly its
top bit, nor why $0063 takes every one of the five perfectly in the same
run through the same code path.

And the fault has a hard boundary. Writing 0xFF and reading straight back
across the whole RAM array:

```
$0060..$0067   7F 00 00 FF FF 7C FF FF     <- the only bad window
$0080..$0087   FF FF FF FF FF FF FF FF     OK
$00C0..$00C7   FF FF FF FF FF FF FF FF     OK
$0100..$0107   FF FF FF FF FF FF FF FF     OK
$0140..$0147   FF FF FF FF FF FF FF FF     OK
$0180..$0187   FF FF FF FF FF FF FF FF     OK
$0200..$0207   FF FF FF FF FF FF FF FF     OK
$0240..$0247   FF FF FF FF FF FF FF FF     OK
```

Health-checked (status 0xC8, $1807 = 0x14) after each block, all good.

Two consequences:

- **RAM writes work.** 496 of the part's 512 RAM bytes take an arbitrary
  byte and give it back. The driver's `write_byte` is correct. What is
  broken is sixteen bytes of silicon at the very bottom of the array — a
  localised defect in this particular die, which is exactly candidate 3 of
  Finding 89 and needs no protocol change at all.
- **$0240 reads and writes**, so the part in the socket has the full
  512-byte RAM of an **MC9S08SG8**, not the SG4's 256 bytes. That is the
  first positive identification of which of the two chips is seated.

This does not block FLASH programming, which touches FLASH addresses and
the FCDIV/FCNFG/FPROT/FSTAT registers at $1820-$1825, never low RAM.

### Finding 93 — every CPU register read returns the pull-up. The non-intrusive/active-BDM split is exact.

First real test of `read_reg` (the NINTH session only ever reached it with
a dead link), with the documented `delay=1`:

```
A = 0xFF   CCR = 0xFF   HX = 0xFFFF   PC = 0xFFFF   SP = 0xFFFF
```

Address-invariant 0xFF: this project's own signature for "the target said
nothing and we read the pull-up". Two of those are not even plausible as
real values — after reset the HCS08 SP is $00FF, and CCR has defined
reset bits — so this is a genuine no-answer, not a suspicious-looking
truth. (PC = 0xFFFF *is* the right answer for a blank reset vector, which
is exactly the kind of coincidence this project has been fooled by before;
it is not evidence of anything on its own.)

Line this up against everything that does work and the split is clean:

```
WORKS (non-intrusive, Table 17-1):   READ_STATUS, WRITE_CONTROL,
                                     READ_BYTE, READ_LAST, WRITE_BYTE,
                                     READ_BKPT, WRITE_BKPT
SILENT (active-BDM only):            READ_A, READ_CCR, READ_PC,
                                     READ_HX, READ_SP
```

Every command that works is one the BDC can service without the CPU being
halted in background mode; every command that answers nothing is one that
requires active BDM. That is the sharpest evidence yet on Finding 76's
open question — **"state S" is not textbook active background mode** —
and it is consistent with BDCSCR = 0xC8 all the same, because BDMACT is
the BDC's own claim and not an independent witness.

It also does not block FLASH programming: the whole erase/program flow is
memory and register writes plus FSTAT polling, none of which is an
active-BDM command.

### Finding 94 — ***FLASH ERASE AND PROGRAM BOTH WORK.*** First bytes ever written to this chip's FLASH

`sync()` measures the target's BDC clock, and BDCSCR = 0xC8 has
**CLKSW = 1**, which per HCS08RMv1 selects the **MCU bus clock** as the BDC
clock. So the 9 248 555 Hz this driver has been calibrating against all
along is a direct measurement of fBus — and 9.25 MHz is exactly what an
untrimmed FEI ICS gives on this part (nominal 8-10 MHz). That is the
number FCDIV needs, and it did not have to be guessed.

```
at entry            FSTAT=0xC0 FCDIV=0x00 FPROT=0xFF FCNFG=0x00
                    (FCBEF=1 FCCF=1, no FPVIOL, no FACCERR: idle and clean)

A  flash_init_clock(9248555)
     FCDIV = 0xB4  DIVLD=1 PRDIV8=0 DIV=52  ->  fFCLK = 174.5 kHz
                                               (window is 150-200 kHz)
B  flash_blank_check()          -> False
C  flash_program_byte($FF00, 0x5A)  in 0.016 s
     $FF00 = 5A     $FF01 = FF   $FF02 = FF        <- neighbours untouched
C2 flash_program_byte($FF10, 0xA3)
     $FF00..$FF1F  5A FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF
                   A3 FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF
     blank_check   -> False
D  flash_erase_page($FF00)      in 0.039 s
     $FF00..$FF1F  FF x32
     blank_check   -> True      FSTAT=0xC4, FBLANK set by the hardware
```

**DIVLD is read-only and is set by the hardware when FCDIV is written.**
Reading it back as 1 is therefore independent proof that a write reached a
peripheral register, not merely that no error was raised. Two different
values at two different addresses, each landing exactly where it was put
with its neighbours left blank, is the same address-varying bar the read
path had to clear. And the FLASH module's own blank-check flag tracks the
array state in both directions.

`write_byte` is correct, `flash_program_byte` is correct,
`flash_erase_page` is correct, `flash_init_clock` is correct.

### Finding 95 — ***HAZARD: erasing page $FE00 blanks NVOPT and SECURES THE PART at the next reset.*** Read this before erasing anything.

$FFB0-$FFBF are the nonvolatile registers, and they live in the **last
FLASH page, $FE00-$FFFF** — the same page as the reset vector at $FFFE.
NVOPT is $FFBF and it was 0xFE (SEC01:SEC00 = 1:0 = unsecured). Erased, it
reads 0xFF, i.e. **SEC = 1:1 = SECURED**.

This also explains, retroactively, why the step-B blank check above
returned False on an array that reads as all-0xFF: NVOPT = 0xFE was the
one programmed byte in the whole part, and the array only reported blank
*after* the page holding it was erased.

HCS08 latches the security state from NVOPT **at reset**, so nothing
happened until the next `power_on_reset_to_bdm` — and then:

```
$1807 SDIDL 0x14 | $1806 SDIDH 0xA0 | $1800 SRS 0x82
$1802 SOPT1 0xC0 | $1809 SPMSC1 0x1C          <- registers still fine
$1820 FCDIV 0x00 | $1824 FPROT 0xFF | $1825 FSTAT 0xC0
$1821 FOPT  0xC3  ->  KEYEN=1 FNORED=1 SEC=1:1   ***SECURED***

$0100 <- 0x5A  reads back 0x00          <- RAM blocked
$E000 $F000 $FF00 $FFB0 $FFBF $FFFE $FFFF  all 0x00
$FFB0..$FFBF   00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
flash_program_byte($FFBF, 0xFE) -> FLASH access error, FSTAT=0xD0 (FACCERR)
```

Textbook HCS08 secure behaviour: the BDC still answers, the peripheral
register file still reads, and **every FLASH and RAM access returns 0x00**.

RULES, for this session and every later one:

1. **Any erase that touches page $FE00 must be followed by reprogramming
   $FFBF = 0xFE before the target is reset**, or the part locks itself.
   Security is evaluated at reset only, so the window is the whole of the
   current power cycle — but it closes the moment you power-cycle.
2. The CodeWarrior blinky .s19 files do **not** contain $FFBF, and they do
   contain $FFFE, so flashing one of them erases page $FE00 and leaves
   NVOPT blank. Programming a blinky image without adding NVOPT = 0xFE by
   hand secures the chip.
3. A mass erase has the same consequence, for the same reason.

Recovery is possible and is attempted next: FOPT reads KEYEN = 1, so
backdoor key access is enabled, and the key at $FFB0-$FFB7 is now erased
and therefore known to be FF FF FF FF FF FF FF FF. The other documented
route is a BDM mass erase.

### Finding 96 — ***THE ERASE-VERIFY IS WHAT UNSECURES AN HCS08.*** Full recovery from Finding 95, and the exact recipe

Mass erase alone does **not** unsecure the part. Run step by step, with
FSTAT read after every single write:

```
   start                 FSTAT=0xC0   $E000=00 $FFBF=00 $FFFE=00  RAM$0100=00
   FPROT  <- FF          FSTAT=0xC0
   FSTAT  <- 30          FSTAT=0xC0     (clear FACCERR/FPVIOL)
   $FFFF  <- FF          FSTAT=0xC0     (the latch write)
   FCMD   <- 41          FSTAT=0xC0     (mass erase)
   FSTAT  <- 80          FSTAT=0x00     (launch: FCBEF and FCCF both drop)
   after 200 ms          FSTAT=0xC0     (FCCF back: the command ran)
   -> still secured, every FLASH/RAM read still 0x00

   FSTAT  <- 30
   $FFFF  <- FF
   FCMD   <- 05          (BLANK CHECK / erase verify)
   FSTAT  <- 80
   after 200 ms          FSTAT=0xC4   FBLANK=1
   -> $E000=FF  $FFBF=FF  $FFFE=FF   RAM $0100 <- 0x5A reads back 0x5A
   -> FOPT went 0xC3 -> 0xC2, i.e. SEC 1:1 -> 1:0
```

Two things worth keeping:

- **The launch write is visible.** FSTAT going 0xC0 -> 0x00 -> 0xC0 across
  the launch is the FLASH module's command handshake happening in real
  time under BDM control. Nothing about that is inferred.
- **FOPT's SEC field is live, not frozen.** It is *loaded* from NVOPT at
  reset, but the unsecure logic updates it in place — which is what makes
  FOPT a usable witness for "did the unsecure work" within a power cycle.

And note what this proves incidentally: **BDM FLASH *writes* are permitted
while the part is secure; only reads are blocked.** The latch write, the
FCMD write and the launch all took effect against a secured part.

Made permanent, and confirmed across a power cycle:

```
mass erase + erase verify  -> access open, FOPT 0xC2
flash_program_byte($FFBF, 0xFE)
   $FFB0..$FFBF  FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FE

--- power cycle, re-enter, re-sync ---
sync 9 248 555 Hz  status 0xC8
FOPT = 0xC2  SEC = 1:0   UNSECURED
$1807=14  $E000=FF  $FFBF=FE  $FFFE=FF
RAM $0100 <- 0xA5 reads back 0xA5
```

The chip is back to exactly the state it was in at the start of the
session. **The recipe, for reuse:**

```
1. enter BDM, sync
2. flash_init_clock(measured_bdc_hz)   # CLKSW=1 so that IS fBus
3. FPROT<-FF, FSTAT<-30, <any flash addr><-FF, FCMD<-41, FSTAT<-80, wait
4. FPROT<-FF, FSTAT<-30, <any flash addr><-FF, FCMD<-05, FSTAT<-80, wait
5. confirm FSTAT.FBLANK = 1 and that a FLASH read is no longer 0x00
6. flash_program_byte($FFBF, 0xFE)     # BEFORE the next reset
```

### Finding 97 — ***THE CHIP IS PROGRAMMED. 195 bytes of CodeWarrior output, byte for byte.***

The real thing, through the shipping paths: `host/srec.py` to parse, the
same page-union erase `host/app.py:/api/flash_srec` computes, and the
firmware's own `flash_write_region` via the `flash_write` JSON verb. No
hand-rolled bypass anywhere.

Image: `NXP_BDM/project/blink/Project/bin/Project.abs.s19` — the SG8
build, $E000 origin. (The SG4 build at $F000 exists alongside it; the
part in the socket is the SG8, per Finding 92's working RAM at $0240.)
NVOPT was appended to the image by hand for the Finding 95 reason.

```
$E000..$E0BF  192 bytes
$FFBF..$FFBF    1 byte   (NVOPT = 0xFE, added by hand)
$FFFE..$FFFF    2 bytes  (reset vector)
pages erased:  $E000, $FE00

erase   $E000  0.044 s      $FE00  0.033 s
program $E000  192 bytes in 1.35 s
        $FFBF    1 byte  in 0.04 s
        $FFFE    2 bytes in 0.02 s
                 195 bytes total

INDEPENDENT READBACK, compared against the .s19 file on the host:
   $E000..$E0BF   MATCH
   $FFBF..$FFBF   MATCH
   $FFFE..$FFFF   MATCH

NEGATIVE CONTROLS -- addresses NOT in the image:
   $E0C0 FF   $E0C1 FF   $E100 FF   $F000 FF
   $FF00 FF   $FFB0 FF   $FFFD FF          all still blank
   flash_blank_check() -> False            (was True before programming)

first 32 bytes as read back off the chip:
   8B 89 9E FE 05 F6 AF 01 9E FF 05 88 8A 81 A7 FC
   C6 E0 85 4C 95 E7 01 C6 E0 84 4C F7 32 E0 86 20
reset vector $FFFE/$FFFF = E0 7B     <- points into the image, as it should
```

The readback is a *separate* comparison from the firmware's own verify:
`flash_write_region` reads every byte back before it returns, and then the
host read the same ranges again over `read_block` and diffed them against
the file's bytes. The negative controls matter as much as the match —
$E0C0, one byte past the end of the image, is still 0xFF, so this is
programmed data and not a read path that returns whatever was asked for.

### Finding 98 — ***AND IT RUNS. PTA0 caught toggling, live, over the BDM link.***

PTA0 has no wire to the Pico, so it cannot be watched directly. It does
not need to be: `target-firmware/blinky/main.c` leaves three fingerprints
that only the target CPU can produce, all readable with non-intrusive BDC
commands while the program runs.

Procedure: power the target up with **BKGD released** (R1 pulls it high,
so the part does NOT latch into background mode), give it 600 ms to run,
then SYNC and read. The CPU is never stopped.

```
sync reported          18 604 652 Hz   <- WRONG, exactly 2x (see below)
working bit clock       9 302 326 Hz   -> SDIDL 0x14, SDIDH 0xA0, link good

$1800 SRS   = 0x82
$1802 SOPT1 = 0x00   reset value 0xC0, and WRITE-ONCE
                     -> the CPU executed main()'s first statement
$0001 PTADD = 0x01   reset value 0x00
                     -> the CPU made PTA0 an output

$0000 PTAD, 60 consecutive non-intrusive reads, bit 0:
   1 1 0 0 0 0 1 1 1 1 0 0 0 0 1 1 1 0 0 0 0 1 1 1 1 0 0 0 0 1 1 1
   1 0 0 0 0 1 1 1 0 0 0 0 1 1 1 1 0 0 0 1 1 1 1 0 0 0 0 1
   16 transitions, a clean square wave of roughly 4 reads per half period
```

SOPT1 is the decisive one. It is write-once and its reset value is 0xC0;
it has read 0xC0 in every session of this project and it read 0xC0 twice
earlier *today* (the session-opening identity check, and Finding 92's
health checks). Nothing the host can do makes it 0x00. `SOPT1 = 0x00;` is
the first line of `main()`.

And PTAD bit 0 is the loop body itself, caught in the act.

**End to end: an S-record built by CodeWarrior, parsed by this project's
host code, erased and programmed into a real MC9S08SG8 by this project's
Pico firmware over four wires, verified byte for byte, and then observed
executing on the target.** That is the whole thing this repository was
written to do.

### Finding 99 — `sync()` reads exactly 2x high against a free-running target

Worth knowing before it wastes someone an hour. Against the running chip,
`sync()` returned 18 604 652 Hz and every transfer at that rate failed
(BDCSCR drifting 14/4A/29/49, then 0xFF after a WRITE_CONTROL). Halving it
by hand to 9 302 326 Hz gave SDIDL = 0x14 and SDIDH = 0xA0 immediately and
everything above worked at that rate.

Exactly 2x, not approximately: the measurement saw 64 cycles of the
target's 128-cycle response, or the response's second half only. In state
S the same code gets 9 248 555 Hz and is correct, so this is specific to
syncing a target that is executing.

Practical rule: after a SYNC, **validate the bit rate against SDIDL = 0x14
before trusting anything**, and try f/2 if it fails. `set_bit_clock` takes
the override directly. Doing that automatically inside `sync()` is an
obvious improvement and is not done yet.

### Finding 100 — reading a RUNNING target needs ENBDM set as well as the halved clock

T13 failed where T11 succeeded, and the difference was one step. On a
free-running target, after SYNC:

```
sync raw = 18 604 652 Hz
read_status x6 at that rate:  25 52 24 52 4A 29     <- drifting nonsense
update_control(set_bits=0x80)  -> wrote 0x90
set_bit_clock(raw / 2) = 9 302 326 Hz
   BDCSCR = 0x98   $1807 -> 14 14 14                <- decoding perfectly
```

Both halves are required. READ_STATUS is answered without ENBDM, so a
non-0xFF status byte is NOT evidence that memory reads will work — it was
the thing that made T13 look like a bit-rate problem when ENBDM was the
missing piece. Memory reads are non-intrusive but still need BDM
*enabled*.

Curiously the WRITE_CONTROL that sets ENBDM lands even when issued at the
wrong (2x) bit rate; it is `set_bit_clock` afterwards that makes reads
decode. Not chased further.

### Finding 101 — the Flask host app drives the whole stack, after two fixes

`host/app.py` had never been run against a working link. It works now,
with two real bugs fixed (both of which made their routes impossible, not
merely awkward):

1. **`/api/sync` entered BDM with `hw_reset_to_bdm()`** — a RESET pulse,
   which datasheet Sec 17.1.1 says this part ignores for BDM entry. Every
   successful entry in this project's history is a POWER-ON reset. Now
   calls `power_on_reset_to_bdm()` (added to `host/bdm_client.py`), and
   then **validates the measured rate by reading SDIDL and retrying at
   half** (Finding 99) instead of trusting SYNC.
2. **`/api/blank_check` and `/api/mass_erase` issued FLASH commands
   without ever writing FCDIV.** FCDIV is volatile and `/api/sync`
   power-cycles the target, so it is always cleared — measured, both
   routes returned "FLASH access error (FACCERR set), FSTAT=0xD0" every
   time. `/api/sync` now records the validated rate as fBus (legitimate
   because CLKSW = 1, Finding 94) and both routes call `flash_init_clock`
   first, inside the serial transaction.

Full HTTP run against the hardware:

```
GET  /api/status        connected False
GET  /api/ports         ['COM1', 'COM10']
POST /api/connect       ok
GET  /api/status        connected True
POST /api/sync          target_freq_hz 9 248 555   sync_raw_hz 9 248 555
GET  /api/read_byte $1807 SDIDL  0x14   OK
GET  /api/read_byte $1806 SDIDH  0xA0   OK
GET  /api/read_byte $1800 SRS    0x82   OK
GET  /api/read_byte $1821 FOPT   0xC2   OK  (SEC = 1:0, unsecured)
GET  /api/read_block $E000,32
     8b899efe05f6af019eff05888a81a7fcc6e0854c95e701c6e0844cf732e08620
     ^ byte for byte the blinky image, through the HTTP API
GET  /api/read_block $FFFE,2     e07b
GET  /api/control                0xC8
POST /api/write_byte $0140<-0x5A ; read back 0x5A
POST /api/write_byte $0140<-0xA5 ; read back 0xA5
POST /api/blank_check            blank False   (was the FACCERR route)
POST /api/disconnect             ok
```

## Where the TENTH session leaves it

**THE PROJECT'S GOAL IS MET.** A from-scratch BDM programmer on a Pico,
four wires, no adapter board, programmed a real MC9S08SG8 with a real
CodeWarrior build and the chip is running it.

DONE AND VERIFIED ON HARDWARE THIS SESSION:

- RAM writes work (Finding 92). The Finding 88/89 "write bug" is a
  localised silicon defect in this die confined to **$0060-$006F**, with a
  fixed per-address AND mask that is invariant to the data written. The
  other 496 RAM bytes take and return arbitrary values. No protocol
  change was needed, and nothing in the FLASH path touches low RAM.
- The part in the socket is the **SG8** (512 B of RAM, $0240 works).
- FLASH init / erase / program / blank check all work (Finding 94).
- The blinky .s19 is programmed and verified byte for byte, with negative
  controls, through `srec.py` + `flash_write_region` (Finding 97).
- The target **executes it**: SOPT1 = 0x00 (write-once, reset 0xC0),
  PTADD = 0x01, and PTAD bit 0 toggling live (Finding 98).
- The Flask app drives all of it over HTTP (Finding 101).

STILL OPEN, in rough priority order:

1. **CPU register reads return nothing** (Finding 93). Everything that
   works is a non-intrusive command and everything silent is active-BDM
   only, so the real question is Finding 76's: what exactly is "state S",
   and how do we get textbook active background mode? `BACKGROUND` (0x90),
   `GO`, `TRACE1` and breakpoints are all in the same boat and are all
   untested against a target that is now running real code — which is a
   much better test bed than a blank chip ever was.
2. **`sync()` should validate itself.** It reads exactly 2x high against a
   running target (Finding 99) and the fix — read SDIDL, retry at half —
   is implemented in `host/app.py` but not in `firmware/bdc.py`, where it
   belongs.
3. `firmware/main.py` gained `eval`/`exec` verbs this session. Useful;
   decide whether they stay in the shipped firmware.
4. The second chip (SG4) has never been tried. Now that the whole flow
   works, swapping it in would confirm the $0060-$006F defect is
   die-specific, and `blink_4kb`'s $F000 image is already built for it.

**HAZARD, repeated because it will bite:** any erase touching page $FE00
blanks NVOPT at $FFBF and secures the part at the next reset (Finding 95).
Always reprogram $FFBF = 0xFE in the same power cycle. If it does get
secured, the recovery is mass erase **followed by an erase-verify** —
the blank check is what actually releases security (Finding 96).

## ELEVENTH session (2026-09-15) — safety, security, and the registers that were never dead

Backend/API pass on top of the TENTH session's working programmer: power-loss
-safe programming, chip security set/clear, a real recovery flow, verify,
dump, chip-info and a pollable live-state feed. No UI in this pass, by
design — the endpoints are built so the next pass has something concrete to
draw.

### Finding 102 — the `eval`/`exec` verbs are gone (default-off, not deleted)

The TENTH session added `{"cmd":"eval"}` / `{"cmd":"exec"}` to
`firmware/main.py` to skip the redeploy-and-reset cycle during debugging.
They are now behind `DEBUG_VERBS = False` in `main.py` (also settable by
dropping a file named `debug_verbs` on the Pico's filesystem), and the verbs
return a refusal explaining how to re-enable them.

The reason is not hypothetical. This same JSON channel now sets and clears
chip security, and on Windows any local process can open COM10 — so
"arbitrary Python on the programmer" and "this port locks and wipes parts"
should not ship in one build. The rest of the protocol is bounded by what
the BDC can do; `exec` is not bounded by anything.

### Finding 103 — ***THE CPU REGISTERS WORK. Finding 93 WAS WRONG.*** delay=0, and read it twice

Finding 93 recorded A/CCR/HX/PC/SP as address-invariant 0xFF — "every
active-BDM-only command is silent" — and built a whole theory on the clean
split between non-intrusive and active-BDM commands. The split was an
artefact of two bugs in `read_reg`, and both are now fixed and measured.

**Bug 1: the answer is in the FIRST marker slot, not the second.** Table
17-1 codes these as `68/d/RD`, so `read_reg` passed `delay=1`. Dumping
every slot of the raw burst with `raw_xfer` (part halted at reset, blinky
in FLASH):

```
  A    op 0x68 -> slots 00 FF FF
  CCR  op 0x69 -> slots 68 FF FF
  PC   op 0x6B -> slots E0 7B FF
  HX   op 0x6C -> slots E0 00 FF
  SP   op 0x6F -> slots 00 FF FF
  READ_STATUS   -> slots C8 FF FF     (control: known-good command)
```

Slot 0 is the data; slot 1 is the pull-up `delay=1` was reading. And the
values are textbook for a part halted at its reset vector: **CCR = 0x68**
(bits 6:5 read 1, I = 1 — the documented reset CCR), **SP = 0x00FF** (the
documented reset SP), **PC = 0xE07B**, which is exactly the reset vector
read back from $FFFE/$FFFF (`e07b`). Three independent right answers at
once.

**Bug 2: A and the H half of H:X answer one TRANSACTION late.** Write a
value, read it back, and the first read returns the PREVIOUS one:

```
   wrote A=0x5A -> reads 00 5A 5A        wrote HX=0xBEEF -> reads F0EF BEEF BEEF
   wrote A=0xA5 -> reads 5A A5 A5        wrote HX=0x0102 -> reads BE02 0102 0102
   wrote A=0x3C -> reads A5 3C 3C        wrote HX=0x1234 -> reads 0134 1234 1234
   wrote A=0x00 -> reads 3C 00 00
   wrote A=0xF0 -> reads 00 F0 F0        (the F0 leading BEEF's first read is
                                          the A write before it — one shared
                                          shift register, one transaction late)
```

CCR and SP round-trip correctly on the FIRST read (`wrote SP=0x0180 ->
0180 0180`, `wrote CCR=0x7F -> 7F 7F`), and PC is stable across repeated
reads. So `read_reg` now issues the read twice and keeps the second answer
for every register — free for the ones that were already right, since they
are side-effect-free reads.

**These are writes we chose coming back, not reset values that could be
coincidence.** A, CCR, HX and SP all round-trip arbitrary data.

**And TRACE1 single-steps the target.** Same session, same link, stepping
the programmed blinky:

```
   PC before = 0xE07B
   after TRACE1 #1  PC = 0xE07E    BDCSCR=0xC8
   after TRACE1 #2  PC = 0xE07F
   after TRACE1 #3  PC = 0xE00E     <- a call into the delay routine
   after TRACE1 #4  PC = 0xE010
```

Consequences, which are large:

- **"State S" IS textbook active background mode.** Finding 76's open
  question is answered: the CPU is halted, the registers are live, and
  single-step works. BDCSCR = 0xC8 (BDMACT = 1) was telling the truth all
  along.
- Finding 93's non-intrusive/active-BDM split is retired. It described a
  host bug, not silicon behaviour.
- Breakpoints, `GO`, and a real debugger UI are now plausible features
  rather than blocked ones.

### Finding 104 — a second `/api/connect` to the same port killed the live connection

Hit for real while testing this session's routes, and it would have hit any
user who clicks Connect twice. Windows gives a COM port to one process
exclusively, so re-opening the port this app already holds fails with
`PermissionError(13, 'Access is denied.')`. `connect()` is careful about
that and leaves the existing handle alone — but `/api/connect`'s error path
then ran `if client.is_connected: client.disconnect()` and closed the
perfectly good connection. Everything afterwards answered "not connected to
a Pico".

Two changes in `host/app.py`:

- A connect to the port already open is now idempotent: it pings and
  returns `{"ok":true,"already_connected":true}` without reopening.
- The teardown on failure only runs if *this call* opened the port
  (`was_connected` captured before the attempt).

Measured after the fix — connect, connect again, then a connect to a port
that does not exist, with a real read between each:

```
2. connect COM10 : {'already_connected': False, 'ok': True}
4. connect AGAIN : {'already_connected': True,  'ok': True}
6. sync          : 9 248 555 Hz
8. connect COM99 : ok False, "could not open port 'COM99'"
9. status        : connected True        <- the live link SURVIVED the failure
10. read $1807   : 0x14                  <- and still decodes
```

### Finding 105 — ***THE SECURE / RECOVER CYCLE IS AUTOMATED AND PROVEN.*** Locked the part on purpose, got it back

Finding 96 was a recipe run by hand. This is the same thing as a shipping
operation (`/api/security` to lock, `/api/unsecure` to recover), driven over
HTTP, with a power cycle on each side so nothing rests on the same power-up
that set it.

**Securing without opening the Finding 95 hazard.** FLASH programming only
drives bits 1 -> 0, so from the unsecured NVOPT = 0xFE the reachable secured
value is **0xFC (SEC = 0:0)**, not 0xFF (SEC = 1:1) — same lock, and page
$FE00 is never erased, so there is no window in which a power loss leaves
the part self-securing. `set_security()` refuses any value needing a 0 -> 1
bit rather than quietly erasing the page to get there.

```
POST /api/security {"confirm":"SECURE"}
   auto-backup first: 8 KB dumped to host/backups/20260916-025450_before-secure_E000-FFFF.s19
                      (starts 8b899efe05f6af01... = the blinky image)
   $FFBF  0xFE -> 0xFC   written=true    fopt still 0xC2 (latched at the last reset)

--- power cycle (/api/sync) ---
   FOPT = 0xC0   SEC = 0:0   secured=true
   $E000..$E03F  all 0x00                  <- LOCKED, for real
   SDIDL still 0x14, SRS 0x82, FSTAT 0xC0  <- registers still answer
```

**Recovering.** And note step 3: the mass erase leaves FOPT at 0xC0, still
secured. It is the blank check that moves it to 0xC2. Finding 96 reproduced
exactly, now under automation:

```
POST /api/unsecure {"confirm":"WIPE"}
   backup           refused, with the reason: a secured part reads 0x00
                    everywhere, so there is nothing readable to back up
   read_security    fopt=0xC0 sec=0 secured=true
   flash_init_clock fcdiv=0xB4 (bus 9 195 402 Hz)
   mass_erase       fstat=0xC0  fopt=0xC0     <- STILL SECURED
   blank_check      blank=true fstat=0xC4 fopt=0xC2 secured=false   <- released here
   flash_readable   $E000 = 0xFF
   security_released fopt=0xC2 sec=2
   restore_nvopt    wrote 0xFE, read 0xFE

--- power cycle (/api/sync) ---
   FOPT = 0xC2  SEC = 1:0  secured=false
   $E000.. = FF FF FF ...   RECOVERED, blank and open
```

One consistency note worth keeping: right after recovery the FLASH module's
own blank check says **not blank** while a byte-by-byte dump says every byte
is 0xFF. Both are right — NVOPT = 0xFE is the single programmed byte in the
array, which is the same thing Finding 95 saw from the other direction.

The guard rails are part of the feature: `/api/security` requires
`{"confirm":"SECURE"}` and `/api/unsecure` requires `{"confirm":"WIPE"}`,
and both take an automatic backup first (see Finding 106).

### Finding 106 — "identify the MCU" has to PROBE, and the $0060-$006F defect has spread

`/api/identify` decodes SDID into words instead of hex, but the interesting
part is what SDID cannot say. Measured:

```
SDIDH 0xA0  SDIDL 0x14  ->  part_id 0x014, rev 0xA
                            family "MC9S08SG8 / MC9S08SG4 (HCS08 SG family)"
                            confidence "id-only", name null
```

The SG8 and the SG4 share one part ID, so a decoder alone cannot name the
part in the socket. `ram_probe=1` settles it the way Finding 92 did, by
writing and restoring one byte:

```
$0240 holds written data  ->  name "MC9S08SG8", ram_bytes 512, flash_kb 8,
                              flash_start $E000, confidence "probed"
(an SG4 would fail that write: 256 B of RAM, no $0240, flash from $F000)
```

`defect_scan=1` measures this die's bad-cell map rather than trusting the
one in this file — and that turns out to matter, because **the defect has
spread since the TENTH session**:

```
addr                   60 61 62 63 64 65 66 67 68 69 6A 6B 6C 6D 6E 6F
TENTH session F92      7F 00 00 FF FF 7C FF FF 7C FF FF 00 7F 00 FF 00
now, scan 1            7F 00 00 FF FF 7C 00 00 7C 00 00 00 7F 00 FF 00
now, scan 2 (same PC)  7F 00 00 FF FF 7C 00 00 7C 00 00 00 7F 00 FF 00
now, scan 3 (power cyc)7F 00 00 FF FF 7C 00 00 7C 00 00 00 7F 00 FF 00
now, scan 4            7F 00 00 FF FF 7C 00 00 7C 00 00 00 7F 00 FF 00
```

$0066, $0067, $0069 and $006A took arbitrary data perfectly a few hours ago
and hold nothing now. Four scans across two power cycles agree, so this is
not measurement noise: the damage in this corner of the die is progressive.
$0063/$0064/$006E are still perfect. Nothing outside $0060-$006F has ever
misbehaved.

Consequence for anything built on top: **a memory-map view must scan for bad
cells, not hard-code Finding 92's table.** That is what this endpoint is
for.

### Finding 107 — page-at-a-time programming, with the NVOPT window closed automatically

`/api/flash_srec` no longer erases a union of pages up front and then writes
chunk by chunk. `flash_program_image()` on the Pico builds the page map from
every chunk and then, per page: **erase -> program -> verify -> next**. A
failure therefore names the page and the address it died on, and every page
before it is confirmed good; there is no state where "something was written
somewhere" is the best available description. That is the honest form of
power-loss protection on this wiring — VDD is a GPIO with no sense line
back, so nothing can detect a sag; what can be done is to never have more
than one page in flight.

The $FE00 hazard is handled inside the same loop: the moment that page is
erased, NVOPT at $FFBF is reprogrammed, before any other byte of the page,
and `security_risk` is True for exactly that window. The blinky image
through the shipping HTTP route:

```
POST /api/flash_srec  (Project.abs.s19, erase_mode=pages, nvopt=0xFE)
   1.5 s, 194 bytes
   page $E000  bytes 192  erased  programmed 192  verified
   page $FE00  bytes 2    erased  nvopt_restored 0xFE  programmed 2  verified
   complete true   security_risk false

POST /api/verify  (same file, independent read-back)   match true, 194 bytes
$FFBF = 0xFE   FOPT = 0xC2 (SEC 1:0, unsecured)   $FFFE/$FFFF = E0 7B
negative controls $E0C0 $E100 $F000 $FF00 $FFB0 $FFFD  all 0xFF
```

Note the .s19 itself does NOT contain $FFBF — the TENTH session had to
append NVOPT by hand to avoid securing the chip (Finding 95, rule 2). That
manual step is now unnecessary: the guard is in the programming loop.

### Finding 108 — ***GO WORKS TOO.*** The target resumes from BDM, and a running target can be polled

Finding 103 said active BDM is real. GO confirms it from the other side: no
power cycle, no reset — just `POST /api/go` against a halted, freshly
programmed part.

```
before GO   PC = 0xE07B   SOPT1 $1802 = 0xC0   PTADD = 0x00   PTAD = 0x00
POST /api/go
after  GO   SOPT1 = 0x00    <- write-once, resets to 0xC0: main() ran
            PTADD = 0x01    <- the CPU made PTA0 an output
            PTAD bit0 x40:  0110001100011000111001110011100110001110
                            16 transitions -- the blink loop, live
```

Same three fingerprints as Finding 98, but reached by resuming the CPU
under BDM control rather than by power-cycling with BKGD released.

**Polling a running target.** `/api/live_state` polls it at ~8 Hz with the
CPU executing: BDCSCR reads 0x89 (ENBDM=1, **BDMACT=0**, CLKSW=1, and DVF
sets sticky, which is expected when a read lands on a busy bus), and PTAD /
PTADD track the program.

Two honesty rules came out of it, both now enforced in `live_state()`:

1. **CPU registers are reported unavailable while BDMACT = 0.** They still
   return bytes — PC came back 0x9C00, 0x4D00, 0xCB00, 0x1A00... a different
   meaningless value every poll — because the register is moving under the
   read. A panel animating that would be showing noise as data. Halted, the
   same field reports PC = 0xE07B with available=true.
2. **A dead link is reported, not rendered.** If BDCSCR reads 0xFF the
   snapshot comes back `{"link_ok": false, "link_error": "..."}` instead of
   0xFF-everything, and any read that throws mid-poll does the same. Seen
   for real: after a while against the running target, reads started
   failing with `DVF ... BDCSCR = 0xFF`.

**`/api/relink` is the recovery for that**, and it is not `/api/sync`:
sync's BDM entry is a power-on reset, which would restart the very program
you are watching. relink only re-measures the bit rate (trying half, per
Finding 99), re-asserts ENBDM (Finding 100), and validates by reading
SDIDL = 0x14. The CPU keeps running throughout.


## TWELFTH session (2026-09-16) — the UI, and two things the backend could not do

Frontend pass on top of the ELEVENTH session's endpoints: chip identity,
live debug, a 2D memory map, real-time programming, and the security /
wipe flow. Scoped to `host/templates/index.html`, `host/static/app.js`,
`host/static/style.css` — plus two small, named backend additions that the
UI genuinely could not be built honestly without (Findings 109 and 110).

### Finding 109 — the Pico answers ONCE per image, so real-time progress had to be driven page by page from the HOST

`flash_program_image()` on the Pico already programs page at a time
(Finding 107), but it is one serial command: the Pico replies when the
WHOLE image is done. Measured, that is 1.5 s for the 194-byte blinky and
would be tens of seconds for a full 8 KB image — a window in which the
host knows *nothing*. Any "progress bar" drawn during that silence would
be a timer pretending to be a measurement, which is exactly what this UI
is not allowed to do.

The fix keeps the firmware untouched. `/api/flash_srec` gained an opt-in
form field `async=1` which runs the same programming on a worker thread,
handing firmware **one page's worth of chunks per call** — firmware builds
its page map from whatever chunks it is given, so one page in means one
page's erase → program → verify out. Each page's real report is published
to a new `GET /api/flash_progress` the moment the Pico returns it:

```
{"state":"running","phase":"page","current_page":57344,"current_index":0,
 "pages_planned":[{"page":57344,"bytes":192},{"page":65024,"bytes":3}],
 "pages":[],            <- only pages the TARGET has confirmed
 "total_bytes":0,"elapsed":0.4}
```

The invariant the UI relies on: `pages` contains only confirmed pages, and
a page that is in flight appears as `current_page` and nowhere else. So
the animation has exactly three honest states per page — planned, on the
wire, confirmed — and never draws a result the target has not given.

Cost of the split: one extra USB round trip per page (16 for a full 8 KB
image), which is noise against ~1.6 ms/byte. The synchronous path is
unchanged and is still the default for scripts.

### Finding 110 — `BACKGROUND` existed in the firmware and had no route

`firmware/main.py` has had `{"cmd":"background"}` since the beginning, and
`bdm_client.py` had no wrapper for it, so nothing above the firmware could
*stop* a running target: the app could `go()` and `step()` but the only way
back to a halt was `/api/sync`, which power-cycles and restarts the
program you were trying to inspect. That is a debugger with a stop button
made of a reset.

Added `BdmClient.background()` and `POST /api/halt`. The route reads
BDCSCR back and reports `halted` from BDMACT rather than assuming the
command worked — BACKGROUND is ignored unless ENBDM is already 1
(Finding 100), and a panel must not claim a halt it did not get.

Measured, against the running blinky:

```
POST /api/halt   -> {"bdcscr":200,"halted":true}     (0xC8 = ENBDM|BDMACT|CLKSW)
```

### Finding 111 — the $0060-$006F defect has spread AGAIN, in under a day

`identify?defect_scan=1`, run at the start of this session against the
same die:

```
                       60 61 62 63 64 65 66 67 68 69 6A 6B 6C 6D 6E 6F
TENTH session F92      7F 00 00 FF FF 7C FF FF 7C FF FF 00 7F 00 FF 00
ELEVENTH session F106  7F 00 00 FF FF 7C 00 00 7C 00 00 00 7F 00 FF 00
now                    7F 00 00 FF FF 7C 00 00 7C 00 00 00 7F 00 FF 00
```

Unchanged since the ELEVENTH session (13 of the 16 cells are damaged;
$0063, $0064 and $006E still take arbitrary data), but the point stands
and is now load-bearing in the UI: the memory map marks defective cells
**only** from a live `defect_scan`, and marks nothing at all until one has
been run. Finding 92's table is never drawn.

### Finding 112 — a serial error came out of /api/ as an HTML page, and the UI reported "Unexpected token '<'"

Hit for real while integrating. `/api/chip_info` raised
`serial.SerialException: Cannot configure port ... PermissionError(13)`
after the Pico's USB CDC went away. Nothing caught it: every route catches
`BdmClientError`, and `SerialException` subclasses `IOError`/`OSError`.
The existing `errorhandler(HTTPException)` (added precisely so /api/ always
answers JSON) does not cover non-HTTP exceptions, so Flask rendered the
Werkzeug debugger's HTML page, `app.js`'s `res.json()` choked on it, and the
UI's error message was **"Unexpected token '<'"** — which says nothing at
all about a dropped USB device.

Fixed with an `errorhandler(Exception)` that, for `/api/` paths only,
prints the traceback to the console (where a bench user wants it) and
returns `{"ok":false,"error":"..."}` with 500. An `OSError` additionally
gets the sentence that actually helps: *the serial handle is no longer
usable, the port most likely re-enumerated, Disconnect and Connect again*.

### Finding 113 — the UI, measured against real hardware

Everything below was exercised in a browser against the real MC9S08SG8 on
COM10, not asserted from the code:

- **Connect → Sync → identify.** Sync through the UI measured 9.249 MHz,
  auto-filled the bus-clock field, and the Chip panel filled itself in:
  MC9S08SG8, confidence "probed", SDIDL 0x14 ✓, FOPT 0xC2 / NVOPT 0xFE
  unsecured, reset source LVD+POR, blank check "programmed".
- **Deep probe.** 13 of the 16 cells in $0060–$006F reported defective and
  drawn in red on the map, at the right addresses ($0063, $0064, $006E
  still clean). Nothing is drawn there until a scan has run.
- **Live polling, both honesty rules.** Halted: PC 0xE07B, CCR 0x68,
  SP 0x00FF, A 0x00 — shown as values. After GO: BDCSCR 0x89, the pill
  flips to RUNNING, and all five registers render as "unavailable / target
  is running" in dashed boxes with the backend's own reason as the
  tooltip. PTA0 tracked the blinky live. A `link_ok:false` snapshot blanks
  the register grid, marks every pin stale, shows BDCSCR as "(no answer)"
  rather than 0xFF, and raises the LINK LOST banner with a Relink button.
- **Halt.** `/api/halt` stopped the running blinky and BDCSCR read back
  0xC8.
- **Real-time programming.** Project.abs.s19 through the UI, sampling the
  DOM every 100 ms during the run:

```
  +349ms  64 cells queued (both pages planned)
  +628ms  row $E000 in flight, 12 cells pulsing   (192 B / 16 B per cell)
  +1429ms still $E000            <- ~950 ms of real page programming
  +1576ms $E000 CONFIRMED (12 written), $FE00 now in flight (1 cell)
  +1744ms both confirmed, 13 cells written, "194 bytes ... 1.3 s"
```

  The dwell times are the hardware's, not a timer's: page $E000 (192 bytes)
  held for ~950 ms and page $FE00 (2 bytes) for ~170 ms, in the ratio the
  byte counts predict.

- The target was left with the blinky programmed and every page verified,
  unsecured (FOPT 0xC2, NVOPT 0xFE).

### Finding 114 — the Pico left the USB bus mid-session; what that means for what is and is not verified

After the programming test above, COM10 disappeared from Windows entirely
(`SerialPort::getportnames()` lists COM1 only; the device shows in PnP with
status "Unknown", i.e. an offline ghost). It did not come back over ~20
minutes of polling. This is a physical replug, not something software can
do from here.

So these UI paths are verified against **recorded** backend payloads (the
exact shapes in `app.py`'s docstrings and the measured outputs in the
ELEVENTH session) rather than against the chip, in a throwaway harness that
overrode `window.fetch` and was deleted afterwards:

- Verify (match and mismatch), Dump/backup, whole-chip scan into the map
  (1216 cells, all classified)
- The lock flow (typed "SECURE", confirm gated on the exact word) and the
  wipe flow (typed "WIPE", the seven-step checklist rendering the real
  step reports)
- The programming FAILURE path, which is the one this file cares most
  about: it names the failing page, lists the pages confirmed good before
  it, and colours them differently on the map —
  `FAILED at page $FE00. 1 page(s) before it are confirmed erased,
  programmed and verified: $E000.`

The lock → unsecure cycle has **not** been re-run against the chip this
session; the ELEVENTH session's Finding 105 is still the hardware evidence
for it. Re-plug the Pico and the whole flow is one Connect away.
