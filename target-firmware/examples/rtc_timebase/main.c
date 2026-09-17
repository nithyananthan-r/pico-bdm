/*
 * rtc_timebase/main.c -- RTC as a 1-second time base, toggling PTA0
 *                        (S9S08SG8E2MTJ / MC9S08SG8 family)
 *
 * The RTC is a tiny 8-bit counter with its own prescaler that can run from
 * the 1 kHz low-power oscillator (LPO). Unlike the TPM (../timer_tpm/) it
 * does not depend on the bus clock at all, so it keeps time while the bus
 * clock is untrimmed, changed, or stopped in a low-power mode.
 *
 * WHAT THIS PROGRAM DOES
 *   RTCLKS = 00  -> clock source is the 1 kHz LPO
 *   RTCPS  = 15  -> prescaler divides by 1000, i.e. one prescaler tick/sec
 *   RTCMOD = 0   -> RTIF is set on EVERY prescaler tick
 *   so RTIF goes off once a second, and PTA0 is toggled each time.
 *
 * READ THIS BEFORE BELIEVING ANY "1 SECOND" CLAIM -- TWO TRAPS:
 *
 * 1. RTCMOD IS A MODULO, NOT A COUNT. The flag period is
 *    (RTCMOD + 1) prescaler ticks, not RTCMOD ticks. RTCMOD = 0 means "set
 *    RTIF on each rising edge of the prescaler output" -- the datasheet
 *    says so explicitly in Table 13-5 -- so RTCMOD = 0 is one tick, and
 *    RTCMOD = 1 is TWO ticks, i.e. half the rate you probably wanted. An
 *    earlier build in this project used RTCPS = 14 with RTCMOD = 1 and was
 *    surprised by the result; see this folder's README.md.
 *
 * 2. "PERIOD" IS AMBIGUOUS FOR A TOGGLING PIN. PTA0 is TOGGLED once per
 *    RTIF, so the interval between edges is 1 s but a full square-wave
 *    cycle (low->high->low) is 2 s. Both numbers are correct and they
 *    differ by 2x. State which one you mean when you report a measurement.
 *
 * ACCURACY: the LPO is a cheap on-chip RC oscillator, not a crystal. The
 * datasheet specs its period (tLPO) at 700 us min / 1500 us max over
 * -40..125 C -- so "1 kHz" is really 667 Hz to 1429 Hz, and this "1 second"
 * can legitimately be anywhere from 0.7 s to 1.5 s on a healthy part. On
 * this project's chip the toggle interval measured 1.0764 s (mean of 27
 * intervals over 30 s), implying the LPO is running at about 929 Hz -- in
 * spec, just not accurate. Use ERCLK from a crystal if you need better.
 *
 * Registers used, all verified against docs/mc9s08sg8.h (the real
 * CodeWarrior header for this part) on 2026-09-17:
 *   RTCSC   $006C  status/control
 *           RTCSC_RTCLKS  merged 2-bit field  <-- plain assignment
 *           RTCSC_RTCPS   merged 4-bit field  <-- plain assignment
 *           RTCSC_RTIF    bit 7, write 1 to clear
 *   RTCCNT  $006D  8-bit counter (read-only; writes have no effect)
 *   RTCMOD  $006E  8-bit modulo
 *
 * DO NOT write `RTCSC_RTCLKS(0)`. These are plain field macros, not
 * function-style ones -- calling them fails to compile with
 * "C1844: Call-operator applied to non-function". That exact mistake is
 * what led to docs/mc9s08sg8.h being checked into this repo at all; see
 * CONTEXT.md Finding 120.
 *
 * Bit encodings verified against MC9S08SG8 datasheet Rev. 8, Table 13-2
 * (RTCLKS: 00 = 1 kHz LPO, 01 = ERCLK, 1x = IRCLK) and the prescaler
 * divide-by values cross-checked in BOTH Table 13-3 and Table 13-6 --
 * see README.md for why one table alone was not enough.
 *
 * Build: see README.md in this folder.
 */

#include <hidef.h>      /* for EnableInterrupts, disable via CodeWarrior */
#include "derivative.h" /* device-specific register/bit definitions */

void main(void) {
  /* Disable the COP watchdog -- same reasoning as ../../blinky/main.c. */
  SOPT1 = 0x00;

  PTADD_PTADD0 = 1; /* PTA0 (physical pin 20) = output */
  PTAD_PTAD0 = 0;   /* start from a known level so the first edge is real */

  /* Order matters: writing RTCMOD resets both the prescaler and RTCCNT to
   * 0x00, and so does changing RTCPS or RTCLKS. Set the modulo first, then
   * the source and prescaler -- the last write starts a clean period. */
  RTCMOD = 0;            /* RTIF on every prescaler tick (see trap 1 above) */
  RTCSC_RTCLKS = 0;      /* clock source = 1 kHz LPO */
  RTCSC_RTCPS = 15;      /* prescaler = divide by 1000 -> 1 tick per second.
                          * Writing a non-zero RTCPS is also what STARTS the
                          * prescaler; RTCPS = 0 means "off". */

  for (;;) {
    /* Poll the flag rather than taking the interrupt -- no vector table or
     * ISR needed, which keeps this example to one file. RTIE stays 0. */
    if (RTCSC_RTIF) {
      RTCSC_RTIF = 1;                /* write 1 to clear (writing 0 does nothing) */
      /* Toggle. Reading PTAD_PTAD0 on a pin configured as an output returns
       * the output latch, so this reads back what we last drove. `!` (not
       * `~`) keeps the result a clean 0/1 for the one-bit field. */
      PTAD_PTAD0 = !PTAD_PTAD0;      /* one edge per second */
    }
  }
}
