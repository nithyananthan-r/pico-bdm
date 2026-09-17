# ics_oscillator — reading and changing the ICS (internal clock source)

Snapshots the four ICS registers into RAM at `$0100`, then sets `BDIV` to
divide-by-4 (halving the bus clock from its reset divide-by-2) and blinks
PTA0 with a fixed busy-wait loop, so the blink running at half speed *is*
the clock change made visible.

**Status: verified on real hardware, with one deliberate exception** — see
the warning below. Read it before flashing this.

> ## ⚠ Running this DROPS THE BDM LINK on the pico-bdm rig
>
> `BDCSCR`'s `CLKSW` bit selects what clocks the BDC serial interface:
> 0 = alternate BDC clock, **1 = MCU bus clock**. On this rig `CLKSW` reads
> 1 — `/api/chip_info` reports `bdc_clock_hz == bus_clock_hz` for exactly
> that reason, and `host/app.py` already relies on it ("SYNC's measured rate
> is fBus on this part, CLKSW=1").
>
> So halving the bus clock halves the BDC clock underneath the host, and
> every subsequent BDM transfer decodes at the wrong bit rate.
>
> **This is recoverable, not fatal.** `/api/sync` does a *power-on* reset
> entry into background mode, which happens before `main()` runs and so
> before `BDIV` is touched; it re-measures the rate from scratch. That
> recovery was exercised and worked (see below). But it does mean this
> example cannot be left free-running and watched over BDM the way the RTC
> and TPM examples can.

## Building it in CodeWarrior for Microcontrollers 6.3 (Special Edition)

Same wizard steps as [`../../blinky/README.md`](../../blinky/README.md):
File -> New -> Bareboard Project; device **MC9S08SG8**; connection
"None"/"Generic"; language C, no Processor Expert; replace the generated
`Sources/main.c` with [main.c](main.c); Project -> Make. Expect **0 errors,
0 warnings**. The `.s19` lands under
`<project>\<Derivative>_Data\Debug\Bin\*.abs.s19`.

## How the clock is built

The FLL multiplies its reference by exactly 1024, and the bus is half the
ICS output:

```
DCOOUT = 1024 x reference
BUSCLK = DCOOUT / (2 x BDIV)
```

Out of reset the part is in **FEI** mode (FLL Engaged, Internal reference)
with `BDIV = 01` (÷2) — which is where the familiar "bus = DCO / 4" comes
from.

`BDIV` encoding, from the ICSC2 field descriptions:
`00` ÷1, `01` ÷2 (**reset default**), `10` ÷4, `11` ÷8.

`ICSC2_BDIV` is a merged 2-bit field — a plain assignment target, **not** a
function-style macro. `ICSC2_BDIV(2)` does not compile.

## Why exact frequency targeting needs measurement, not arithmetic

The internal reference is untrimmed after a power-on reset (`TRIM` resets to
`0x80`, `FTRIM` to 0), and the datasheet bounds the untrimmed DCO only
loosely: `fdco_ut` = 25.6 / 36.86 / 42.66 MHz (min/typ/max). That ±20%
spread puts the reset bus clock somewhere between ~6.4 and ~10.7 MHz
depending on the individual part, its supply and its temperature.

`ICSTRM` (`$004A`, coarse) and `ICSSC`'s `FTRIM` bit (`$004B` bit 0, fine)
are what pull the reference onto a known value — and using them is an
**empirical loop**: write a trim, measure the resulting clock against
something you trust, adjust, repeat. You cannot compute the right `ICSTRM`
from the datasheet. A real application copies the factory values from the
non-volatile `NVICSTRM`/`NVFTRIM` bytes at startup.

This example deliberately does **not** touch the trim. It changes only
`BDIV`, which is an exact power-of-two divide and needs no measurement to
predict.

## What was actually measured

Flashed (221 bytes at `$E000`, verified byte-for-byte on read-back). Read
directly off the halted chip after a power-on BDM entry, **before `main()`
ran**:

```
ICSC1  = 0x04     IREFS = 1 -> internal reference selected: FEI mode
ICSC2  = 0x40     BDIV = 01 -> divide by 2, the documented reset default
ICSTRM = 0x80     the documented reset value (untrimmed)
ICSSC  = 0x10     IREFST = 1 (internal ref is the live source), FTRIM = 0
```

All four match what [main.c](main.c)'s comments predict.

Then single-stepped from reset (which keeps the target in background mode
and the link solid):

```
after 60 steps:  RAM $0100..$0103 = 04 40 80 10
                 -- the program's own snapshot, byte-for-byte identical to
                    the four registers read independently above
after 4 more:    the ICSC2_BDIV = 2 write executes, and the next BDM
                 transfer FAILS outright
```

That last step is the warning above, demonstrated rather than predicted:
the instant `BDIV` changed, the BDC clock changed with it and the link
stopped decoding. `/api/sync` then recovered it cleanly at 9,195,402 Hz,
and the rig was reflashed with blinky and re-verified afterwards.

**Not verified:** the halved-speed blink itself, since observing it would
require the link to survive the very change that breaks it. Confirming that
would need an instrument on pin 20, or setting `CLKSW = 0` first so the BDC
runs off the alternate clock (a constant ÷2 of the DCO output) instead of
the bus.
