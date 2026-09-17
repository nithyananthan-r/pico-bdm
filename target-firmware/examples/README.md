# MC9S08SG8 example firmware

Minimal, self-contained starting points for the target chip
(S9S08SG8E2MTJ / MC9S08SG8 family), one folder per peripheral. Each is a
single `main.c` built the same way as
[`../blinky/`](../blinky/) — a bareboard CodeWarrior 6.3 project with no
Processor Expert — plus a README stating exactly what was and was not
verified.

pico-bdm only **flashes** the `.s19` these produce; it does not compile
anything.

## The examples

| Folder | What it shows | Verification status |
|---|---|---|
| [`gpio_blink/`](gpio_blink/) | Reading an input pin with its internal pull-up on, mirrored to an output pin | **Hardware verified** |
| [`timer_tpm/`](timer_tpm/) | TPM2 free-running off the bus clock (/128) timing a variable-duty square wave | **Hardware verified** |
| [`rtc_timebase/`](rtc_timebase/) | RTC off the 1 kHz LPO as a 1-second time base, polling `RTIF` | **Hardware verified** |
| [`ics_oscillator/`](ics_oscillator/) | Reading the ICS registers and changing the bus clock with `BDIV` | **Hardware verified**, except the halved-speed blink — ⚠ **running it drops the BDM link** |
| [`sci_uart/`](sci_uart/) | SCI transmit: baud divisor, `TE`, polling `TDRE`/`TC` | **Partially verified** — configured and actively transmitting, but no bytes received (TxD is not wired) |
| [`adc_read/`](adc_read/) | ADC single conversions on the internal VREFH / VREFL / bandgap / temperature channels | **Hardware verified** (self-checking, needs no external signal) |

For a plain "toggle one output pin" starting point, use
[`../blinky/`](../blinky/) rather than anything here — it is the original
proven program and `gpio_blink/` deliberately covers different ground.

## Building any of them

Identical to [`../blinky/README.md`](../blinky/README.md), which is the
canonical description:

1. File -> New -> Bareboard Project.
2. Device: **MC9S08SG8**.
3. Connection: "None" / "Generic" — only the compiler and linker are used,
   not CodeWarrior's debugger.
4. Language: C, skip Processor Expert.
5. Replace the generated `Sources/main.c` with the example's `main.c`.
6. Project -> Make (the hammer icon). Every example here builds with
   **0 errors and 0 warnings**.
7. The output lands under `<project>\<Derivative>_Data\Debug\Bin\*.abs.s19`
   — load that in pico-bdm's **Flash** panel, erase mode "touched pages".

## The rule these were written to

**Every register name, bit-field macro and address used in these examples
was checked against [`../../docs/mc9s08sg8.h`](../../docs/mc9s08sg8.h)** —
the real CodeWarrior-generated header for this exact part — before being
used, and every bit-field *encoding* was checked against the register
tables in
[`../../docs/MC9S08SG8_datasheet_rev8.pdf`](../../docs/MC9S08SG8_datasheet_rev8.pdf).
Not by analogy with another Freescale part, and not from a quick datasheet
skim.

That rule exists because guessing produced two real failures earlier in this
project: a macro invented by analogy (`RTCSC_RTCLKS(1)` — the real macro is
a plain field assignment, `RTCSC_RTCLKS`) that would not compile, and a
misdiagnosed "defective RAM" region that turned out to be the TPM2 and RTC
registers behaving perfectly normally (CONTEXT.md Finding 120).

Two traps worth carrying forward:

- **Merged bit-fields are assignment targets, not function-style macros.**
  `TPM2SC_PS = 7` compiles; `TPM2SC_PS(7)` does not.
- **`pdftotext -layout` scrambles the column order of several of this
  datasheet's tables** — including the RTC prescaler table (13-3) and the
  ADC conversion-mode table (9-7), in both cases in ways that suggest a
  plausible but wrong encoding. `pdftotext -raw` returns them in true
  reading order. Where a table has a companion that says the same thing
  differently (Table 13-6's prescaler *periods* against Table 13-3's
  divide-by *values*), check both.

## What "hardware verified" means here

The rig has exactly three wires to the target — RESET (pin 1), BKGD (pin 2)
and VDD (pin 3). **No port pin is connected to anything**
(CONTEXT.md Finding 117). So "observed" never means an oscilloscope; it
means one of:

- polling a port data register over BDM with host-side wall-clock
  timestamps while the target free-runs (good to ~30 Hz — fine for the
  ~1 s waveforms here, useless for anything fast), or
- single-stepping from reset and reading registers or RAM at each step,
  which keeps the BDM link solid and works even for programs the link
  cannot survive free-running.

Every example was also read back from FLASH byte-for-byte against its own
`.s19` after programming.

Measured figures are in each folder's README. The two independent bus-clock
measurements are worth noting together: the BDC SYNC bit rate
(9.20–9.30 MHz) and `timer_tpm`'s counted TPM period (9.09 MHz) agree to
within 1–2%, which cross-validates both the TPM clock-source encoding and
this project's "the SYNC rate is fBus" assumption.
