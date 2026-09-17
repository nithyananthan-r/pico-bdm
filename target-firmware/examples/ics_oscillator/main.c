/*
 * ics_oscillator/main.c -- reading and changing the ICS (internal clock
 *                          source) registers; halving the bus clock with
 *                          BDIV and showing the effect
 *                          (S9S08SG8E2MTJ / MC9S08SG8 family)
 *
 * *** READ THE WARNING BELOW BEFORE FLASHING THIS ONTO THE pico-bdm RIG. ***
 * *** Changing the bus clock DROPS THE BDM LINK on this hardware.       ***
 *
 * The ICS is what decides how fast everything else on the chip runs, so it
 * is worth understanding before trusting any timing number from the TPM
 * (../timer_tpm/) or a busy-wait loop.
 *
 * HOW THE CLOCK IS BUILT (datasheet Rev. 8, Chapter 10)
 *   The FLL multiplies its reference by exactly 1024:
 *       DCOOUT = 1024 x reference frequency
 *   The bus is then half of the ICS output, and ICSOUT is DCOOUT divided by
 *   BDIV, so altogether:
 *       BUSCLK = DCOOUT / (2 x BDIV)
 *   Out of reset this part is in FEI mode (FLL Engaged, Internal reference)
 *   with BDIV = 01 (divide by 2), which is where the familiar
 *   "bus = DCO / 4" comes from.
 *
 * WHY YOU CANNOT JUST COMPUTE THE FREQUENCY YOU WANT
 *   The internal reference is untrimmed after a power-on reset -- TRIM
 *   resets to 0x80 and FTRIM to 0 -- and the datasheet only bounds the
 *   untrimmed DCO output loosely: fdco_ut = 25.6 / 36.86 / 42.66 MHz
 *   (min / typ / max). That is a +/-20% spread, so the reset bus clock is
 *   somewhere between about 6.4 and 10.7 MHz depending on the individual
 *   part, its supply and its temperature. On this project's chip the bus
 *   clock measured 9,248,555 Hz (CONTEXT.md; the BDC SYNC rate is the bus
 *   rate on this part because BDCSCR CLKSW = 1).
 *
 *   ICSTRM ($004A) and ICSSC's FTRIM bit ($004B bit 0) are the coarse and
 *   fine adjustments that pull the internal reference onto a known value.
 *   Exact frequency targeting is an EMPIRICAL procedure: write a trim,
 *   measure the resulting clock against something you trust, adjust, repeat.
 *   You cannot compute the right ICSTRM from the datasheet. Freescale ships
 *   a factory trim value in the non-volatile NVICSTRM/NVFTRIM bytes for
 *   parts that were trimmed in test; a real application copies those into
 *   ICSTRM/ICSSC at startup. This example deliberately does NOT touch the
 *   trim -- it only changes BDIV, which is an exact power-of-two divide and
 *   needs no measurement to predict.
 *
 * WHAT THIS PROGRAM DOES
 *   Reads the four ICS registers into RAM at $0100 so they can be inspected
 *   over BDM, sets BDIV to divide-by-4 (halving the bus clock from its
 *   reset divide-by-2), then blinks PTA0 with a fixed busy-wait loop. The
 *   blink is the demonstration: the loop count never changes, so the blink
 *   running at half speed is the bus clock change made visible.
 *
 * Registers used, all verified against docs/mc9s08sg8.h (the real
 * CodeWarrior header for this part) on 2026-09-17:
 *   ICSC1   $0048  ICSC1_CLKS (2-bit), ICSC1_RDIV (3-bit), ICSC1_IREFS,
 *                  ICSC1_IRCLKEN, ICSC1_IREFSTEN
 *   ICSC2   $0049  ICSC2_BDIV (merged 2-bit field), ICSC2_RANGE, ICSC2_HGO,
 *                  ICSC2_LP, ICSC2_EREFS, ICSC2_ERCLKEN, ICSC2_EREFSTEN
 *   ICSTRM  $004A  8-bit coarse trim (ICSTRM_TRIM0..TRIM7)
 *   ICSSC   $004B  ICSSC_FTRIM (bit 0), ICSSC_OSCINIT, ICSSC_CLKST (2-bit),
 *                  ICSSC_IREFST
 *
 * BDIV encoding, verified against datasheet Rev. 8 Table 10-x (ICSC2 field
 * descriptions):
 *   00 divide by 1   01 divide by 2 (RESET DEFAULT)   10 divide by 4
 *   11 divide by 8
 *
 * Build: see README.md in this folder.
 */

#include <hidef.h>      /* for EnableInterrupts, disable via CodeWarrior */
#include "derivative.h" /* device-specific register/bit definitions */

/* A fixed window in RAM to leave the "before" register values in, so they
 * can be read back over BDM with /api/read_block?addr=0x0100&len=4 without
 * having to look up where the linker put a named variable.
 * $0100 is inside this part's RAM -- RAM is $0080-$027F (datasheet
 * Figure 4-1, and lib\hc08c\prm\mc9s08sg8.prm). Note that RAM does NOT
 * start at $0060: $0060-$006E are the TPM2 and RTC registers. That
 * correction is CONTEXT.md Finding 120. */
#define ICS_SNAPSHOT ((volatile unsigned char *)0x0100)

static void delay(unsigned int loops) {
  /* `volatile` is load-bearing: the loop body is empty, so without it the
   * optimizer is free to delete the whole thing. Same reasoning as
   * ../../blinky/main.c. */
  volatile unsigned int i = loops;
  while (i--) {
    /* burn cycles -- the point is that this count is CONSTANT while the
     * bus clock underneath it changes */
  }
}

void main(void) {
  /* Disable the COP watchdog -- same reasoning as ../../blinky/main.c. */
  SOPT1 = 0x00;

  /* Snapshot the ICS as the chip came out of reset, before changing it.
   * Expected on an untrimmed MC9S08SG8 at power-on: ICSC1 = 0x04
   * (IREFS = 1, internal reference selected -> FEI mode), ICSC2 = 0x40
   * (BDIV = 01, divide by 2), ICSTRM = 0x80, ICSSC with FTRIM = 0. */
  ICS_SNAPSHOT[0] = ICSC1;
  ICS_SNAPSHOT[1] = ICSC2;
  ICS_SNAPSHOT[2] = ICSTRM;
  ICS_SNAPSHOT[3] = ICSSC;

  PTADD_PTADD0 = 1; /* PTA0 (physical pin 20) = output */

  /* Halve the bus clock: BDIV 01 (divide by 2, the reset value) -> 10
   * (divide by 4). Only the BDIV field is touched, so RANGE/HGO/LP and the
   * external-reference bits keep their reset values.
   *
   * ICSC2_BDIV is a merged 2-bit field -- a plain assignment, NOT a
   * function-style macro. `ICSC2_BDIV(2)` does not compile. */
  ICSC2_BDIV = 2;

  /* The datasheet notes the BDIV bits may be changed at any time and the
   * switch to the new frequency happens on the following clock; there is no
   * lock or status bit to wait on for a BDIV change specifically. (Waiting
   * on ICSSC_CLKST is for changing CLOCK MODE via ICSC1_CLKS, which this
   * example does not do.) */

  for (;;) {
    PTAD_PTAD0 = 1;
    delay(20000);
    PTAD_PTAD0 = 0;
    delay(20000);
  }
}

/*
 * ============================ WARNING ============================
 * ON THE pico-bdm RIG, RUNNING THIS BREAKS THE BDM LINK UNTIL RESYNC.
 *
 * BDCSCR's CLKSW bit selects what clocks the BDC serial interface:
 *   0 = alternate BDC clock source,  1 = MCU bus clock
 * On this rig CLKSW reads 1 (confirmed live via /api/live_state and
 * /api/chip_info, which reports bdc_clock_hz == bus_clock_hz for exactly
 * this reason). The BDM protocol is self-clocked at 16 BDC cycles per bit
 * against a rate the host measured once with SYNC -- so halving the bus
 * clock halves the BDC clock underneath the host's feet, and every
 * subsequent BDM transfer is decoded at the wrong bit rate.
 *
 * That is recoverable, not fatal: reset the target and re-run SYNC
 * (/api/sync, or the Connection panel) to re-measure the new rate. But it
 * means this example cannot be left running while you watch it over BDM the
 * way the RTC and TPM examples can, which is why its hardware-verification
 * status in ../README.md is what it is. Read that before flashing it.
 * =================================================================
 */
