# rtc_timebase — RTC as a 1-second time base

Toggles PTA0 (physical pin 20) once per second using the RTC running from
the 1 kHz low-power oscillator, polling `RTCSC_RTIF` rather than taking an
interrupt.

**Status: verified on real hardware.**

## Building it in CodeWarrior for Microcontrollers 6.3 (Special Edition)

Same wizard steps as [`../../blinky/README.md`](../../blinky/README.md):

1. File -> New -> Bareboard Project.
2. Device: **MC9S08SG8**.
3. Connection: "None" / "Generic" — only the compiler and linker are used.
4. Language: C, no Processor Expert.
5. Replace the generated `Sources/main.c` with [main.c](main.c) from here.
6. Project -> Make (the hammer icon). Expect **0 errors, 0 warnings**.
7. The `.s19` lands under `<project>\<Derivative>_Data\Debug\Bin\*.abs.s19` —
   that is the file to load in pico-bdm's Flash panel.

## The prescaler question, settled

An earlier session in this project measured this kind of RTC delay at
**~2.11 s when 1.0 s was intended**, and suspected the RTC prescaler table
had been misread — specifically that Table 13-3's decimal divide-by column
ordering (1, 2, 4, 10, 16, 100, 500, 1000 for RTCPS 8–15) had been
reconstructed wrongly from a scrambled PDF text extraction.

**That suspicion was wrong. The table reading was right all along**, and the
2x was never in the prescaler. Three independent confirmations:

1. **Table 13-3** (prescaler divide-by values) re-extracted in `pdftotext
   -raw` reading order instead of `-layout` column order:
   `Off 2³ 2⁵ 2⁶ 2⁷ 2⁸ 2⁹ 2¹⁰ 1 2 2² 10 2⁴ 10² 5x10² 10³` — so RTCPS 8–15
   really are ÷1, ÷2, ÷4, ÷10, ÷16, ÷100, ÷500, ÷1000.
2. **Table 13-6** (prescaler *period*, which has no superscripts to garble)
   gives the 1 kHz column directly: RTCPS `1110` -> 0.5 s, `1111` -> **1 s**.
   Every row agrees with Table 13-3.
3. The datasheet's **own worked example** (Figure 13-6) states
   "RTCPS is set to 0xA or divide-by-4" — and 0xA = 10 -> 2² = 4. Anchored.

So where did the 2x come from? Two candidates, both real, and this build
distinguishes them:

- **`RTCMOD` is a modulo, not a count.** The flag period is
  `(RTCMOD + 1)` prescaler ticks. The earlier build used `RTCMOD = 1`,
  which is *two* ticks, not one.
- **"Period" is ambiguous for a toggling pin.** The pin is toggled once per
  flag, so a full square-wave cycle is twice the flag interval.

The earlier build was `RTCPS = 14` (÷500) with `RTCMOD = 1` (2 ticks)
= 1.0 s between flags = a **2.0 s full square wave**. It was working
correctly; the "~2.11 s" was simply its full period, measured against an
expectation of 1.0 s that referred to the flag interval. Both effects are
exactly 2x, and here they cancelled — the prescaler was never at fault.

## What was actually measured

Flashed to the target (187 bytes, both pages verified byte-for-byte on
read-back) and left running, with PTAD (`$0000`) polled over BDM with
host-side wall-clock timestamps at ~30 Hz for 30 s:

```
885 samples, 27 toggle intervals
mean toggle interval   1.0764 s      (nominal 1.000 s)
full square-wave period 2.153 s
min 0.936 s / max 1.227 s  -- spread is BDM polling jitter, not the RTC;
                              the 27-interval mean spans 29 s, so it is
                              good to well under 1%
```

The earlier session's "~2.11 s" and this build's 2.153 s full period are the
same measurement. The residual **+7.6% over nominal is the LPO itself**: it
implies the low-power oscillator is running at ~929 Hz rather than 1000 Hz,
i.e. a period of 1076 µs — comfortably inside the datasheet's `tLPO` spec of
**700 µs min / 1500 µs max**. Nothing here is out of tolerance; the LPO is
simply not a precision reference.

## If you need better than ±30%

Use `RTCLKS = 01` (ERCLK) with a real crystal, or `RTCLKS = 1x` (IRCLK) with
a trimmed internal reference. The 1 kHz LPO is chosen here because it needs
no external parts and keeps running in the low-power modes — accuracy is
explicitly not what it is for.
