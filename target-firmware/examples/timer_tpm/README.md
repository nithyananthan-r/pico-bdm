# timer_tpm — TPM2 as a free-running time base, driving a variable-duty square wave

TPM2 runs free from the bus clock with a /128 prescaler and a full 16-bit
modulo; software compares `TPM2CNT` against a duty constant to drive PTA0
(physical pin 20). Changing one `#define` changes the duty cycle.

**Status: verified on real hardware.**

## Building it in CodeWarrior for Microcontrollers 6.3 (Special Edition)

Same wizard steps as [`../../blinky/README.md`](../../blinky/README.md):
File -> New -> Bareboard Project; device **MC9S08SG8**; connection
"None"/"Generic"; language C, no Processor Expert; replace the generated
`Sources/main.c` with [main.c](main.c); Project -> Make. Expect **0 errors,
0 warnings**. The `.s19` lands under
`<project>\<Derivative>_Data\Debug\Bin\*.abs.s19`.

## Register encodings, checked rather than assumed

Both of the fields this example writes are *merged bit-field* macros in
`docs/mc9s08sg8.h` — plain assignment targets, **not** function-style
macros. `TPM2SC_PS(7)` does not compile.

| Field | Written | Meaning | Source |
|---|---|---|---|
| `TPM2SC_CLKSx` | 1 | Bus rate clock | datasheet Table 16-6 (00 none, 01 bus, 10 fixed system, 11 external) |
| `TPM2SC_PS` | 7 | Divide by 128 | datasheet Table 16-7 (000=1 … 111=128) |
| `TPM2MOD` | 0xFFFF | free-running over the full 16-bit range | — |

## What was actually measured

Flashed (188 bytes at `$E000`, verified byte-for-byte on read-back both
before and after the run), then left running with PTAD (`$0000`) polled over
BDM at ~30 Hz for 40 s:

```
1213 samples, 87 edges
duty cycle        25.5 %        (expected 25.0 % = 0x4000 / 65536)
full period       0.9231 s      (mean of 85 cycles)
high ~0.20-0.24 s, low ~0.70-0.76 s
```

### This doubles as an independent measurement of the bus clock

The period is `65536 x 128 / f_bus` by construction, so the measurement can
be inverted:

```
implied f_bus = 65536 x 128 / 0.9231 s = 9,087,306 Hz
```

The BDM link measures the bus clock completely independently — `/api/sync`
times the BDC bit rate, which *is* the bus rate on this part because
`BDCSCR CLKSW = 1` — and reported **9,195,402 – 9,302,326 Hz** across the
same session. The two methods agree to within about 1–2%.

That agreement is worth more than either number alone: it confirms at once
that `CLKSx = 01` really does select the bus clock, that `PS = 111` really
is ÷128, and that this project's long-standing "the SYNC rate is fBus"
assumption holds.

## "Precise" means precise in bus cycles, not in seconds

TPM2 counts bus cycles exactly. The bus clock itself, out of reset, is the
**untrimmed** internal DCO divided down, and the datasheet allows
`fdco_ut` = 25.6 / 36.86 / 42.66 MHz (min/typ/max) — a ±20% part-to-part
spread. So this example gives an exact cycle count and only an
approximately-known wall-clock time. If you need real seconds, either trim
the ICS (see [`../ics_oscillator/`](../ics_oscillator/)) or use the RTC off
the LPO (see [`../rtc_timebase/`](../rtc_timebase/)), which does not depend
on the bus clock at all.

## Doing it in hardware instead

TPM2 has two real PWM channels, so the software compare in the loop can be
handed to the timer entirely: `TPM2C1SC_MS1x = 2`, `TPM2C1SC_ELS1x = 2`,
duty in `TPM2C1V`, output on TPM2CH1 = **PTB4 (physical pin 8)**. That is
the right choice for a real design. It is not used here because no port pin
on this rig is wired to anything (CONTEXT.md Finding 117), so a
hardware-generated edge would be unobservable — whereas the software
version's writes to PTAD can be read straight back over BDM.
