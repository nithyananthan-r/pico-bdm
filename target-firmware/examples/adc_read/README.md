# adc_read — ADC single conversions on the internal reference channels

Converts VREFH, VREFL, the temperature sensor and the internal bandgap in a
loop, storing each 10-bit result to a fixed RAM window at `$0100` where it
can be read back over BDM while the target runs.

**Status: verified on real hardware** — including a self-checking result
that needs no external analog signal.

## Building it in CodeWarrior for Microcontrollers 6.3 (Special Edition)

Same wizard steps as [`../../blinky/README.md`](../../blinky/README.md):
File -> New -> Bareboard Project; device **MC9S08SG8**; connection
"None"/"Generic"; language C, no Processor Expert; replace the generated
`Sources/main.c` with [main.c](main.c); Project -> Make. Expect **0 errors,
0 warnings**. The `.s19` lands under
`<project>\<Derivative>_Data\Debug\Bin\*.abs.s19`.

## Why internal channels instead of a potentiometer

No port pin on this rig is wired to anything (CONTEXT.md Finding 117), so
an external-channel example would convert a floating pin and prove nothing.
The ADC's channel mux has inputs that need no external circuit at all:

| `ADCH` | Channel | What it is |
|---|---|---|
| 26 (`11010`) | AD26 | on-chip temperature sensor |
| 27 (`11011`) | AD27 | internal bandgap (needs `SPMSC1_BGBE = 1`) |
| 29 (`11101`) | VREFH | tied to VDD — the converter's own top reference |
| 30 (`11110`) | VREFL | tied to VSS |
| 31 (`11111`) | — | module disabled (the reset value) |

Converting VREFH and VREFL is a complete end-to-end self-test: they *are*
the two ends of the converter's reference, so in 10-bit mode they must come
back at essentially full scale and essentially zero. Anything else means
the ADC is misconfigured.

`SPMSC1_BGBE` (`$1809` bit 0) is easy to miss — the bandgap buffer is
enabled outside the ADC's own registers, and without it AD27 reads
meaningless.

## Configuration, checked against the datasheet

| Field | Written | Meaning | Source |
|---|---|---|---|
| `ADCCFG_ADICLK` | 1 | bus clock / 2 | Table 9-8 (00 bus, 01 bus/2, 10 ALTCLK, 11 ADACK) |
| `ADCCFG_ADIV` | 1 | divide by 2 | Table 9-6 (00=1, 01=2, 10=4, 11=8) |
| `ADCCFG_MODE` | 2 | **10-bit** | Table 9-7 (00=8-bit, 01=Reserved, **10=10-bit**, 11=Reserved) |
| `ADCCFG_ADLSMP` | 1 | long sample time | — |

`MODE` is the trap here: 10-bit is encoding `10`, **not** `01`. The
`-layout` text extraction of Table 9-7 scrambles the column order badly
enough to suggest otherwise; re-extracting with `pdftotext -raw` gives the
rows in true reading order and settles it. (Same class of extraction
hazard as the RTC prescaler table — see
[`../rtc_timebase/README.md`](../rtc_timebase/README.md).)

ADCK works out to bus/4 ≈ 2.3 MHz, inside the specified `fADCK` range of
0.4–8.0 MHz (Table A-12, `ADLPC = 0`).

## What was actually measured

Flashed (292 bytes at `$E000`, verified byte-for-byte on read-back), left
running, and `$0100`–`$0108` read over BDM:

```
VREFH    = 0x03F8 = 1016 / 1023   99.3 % of full scale
VREFL    = 0x0000 =    0          exactly zero
TEMP     = 0x01BB =  443          43.3 % of full scale
BANDGAP  = 0x0189 =  393          38.4 % of full scale
pass counter = 112                the loop is live, not a stale snapshot
```

VREFH at essentially full scale and VREFL at exactly zero is the ADC
working. (VREFH reading 1016 rather than 1023 is ~0.7% of full-scale error,
unremarkable for an untrimmed converter.)

### The bandgap reading yields the supply voltage

The datasheet's documented use of AD27 is to work back to VDD, since the
bandgap is a known voltage: `VBG` = 1.18 / **1.20** / 1.21 V
(min/typ/max, Section A.6).

```
VDD = VBG x 1023 / ADCR_bandgap = 1.20 x 1023 / 393 = 3.12 V
```

3.12 V is a sensible figure for this rig, where VDD is driven from the Pico
(CONTEXT.md Finding 117) — a further independent sign the conversions are
real rather than noise.

## What was NOT verified

**The temperature value is not a datasheet-compliant temperature
measurement.** Section 9.1.4 requires the temperature sensor to be read
"with long sample and a maximum of 1 MHz clock", and ADCK here is ~2.3 MHz.
That is fine for VREFH/VREFL/bandgap, which only need `fADCK` inside
0.4–8.0 MHz, but it is above what 9.1.4 asks for. To fix it, set
`ADCCFG_ADIV = 3` (divide by 8), giving ADCK ≈ 578 kHz. It is deliberately
left at 1 in [main.c](main.c) so that the source matches the build that was
actually measured above, rather than quietly differing from it.

No **external** analog channel was tested, because none is wired. If you do
wire one: enable the matching `APCTL1_ADPCn` bit to disconnect that pin's
digital input buffer (not needed for internal channels, which have no pin),
and expect a floating input to return a drifting, meaningless value and a
rail-tied input to return ~0x000 or ~0x3FF.
