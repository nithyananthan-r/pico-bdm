/*
 * gpio_blink/main.c -- GPIO input with pull-up, mirrored to an output pin
 *                      (S9S08SG8E2MTJ / MC9S08SG8 family)
 *
 * The plain "toggle a pin in a busy-wait loop" blinky already exists and is
 * proven on this rig -- see ../../blinky/main.c. Rather than duplicate it,
 * this example covers the other half of GPIO: reading a pin, enabling its
 * internal pull-up, and driving a second pin from what was read.
 *
 * PTA1 (physical pin 19) is configured as an input with its internal
 * pull-up enabled; PTA0 (physical pin 20) is an output that continuously
 * mirrors PTA1. Ground PTA1 and PTA0 follows it low.
 *
 * WHY A PULL-UP MATTERS HERE, AND WHY IT MAKES THIS TESTABLE:
 * nothing on the pico-bdm rig is wired to any port pin (only RESET, BKGD
 * and VDD are -- CONTEXT.md Finding 117). A floating CMOS input reads an
 * arbitrary, drifting value, so without the pull-up this program has no
 * defined behaviour to check. With PTAPE_PTAPE1 = 1 the unconnected input
 * is held high, so PTA0 must sit at a steady 1 -- an unambiguous result
 * that can be read back over BDM with no wires added.
 *
 * Registers used, all verified against docs/mc9s08sg8.h (the real
 * CodeWarrior header for this part) on 2026-09-17:
 *   PTAD   $0000  Port A data        -- PTAD_PTAD0..PTAD_PTAD3 only
 *   PTADD  $0001  data direction     -- PTADD_PTADD0..PTADD_PTADD3 only
 *   PTAPE  $1840  pull-up enable     -- PTAPE_PTAPE0..PTAPE_PTAPE3 only
 * NOTE: Port A on this part is FOUR BITS WIDE. The header defines PTAD0-3
 * and nothing above that; there is no PTAD_PTAD4..7. Port B is the full
 * eight bits (PTBD_PTBD0..PTBD_PTBD7).
 *
 * Build: see README.md in this folder.
 */

#include <hidef.h>      /* for EnableInterrupts, disable via CodeWarrior */
#include "derivative.h" /* device-specific register/bit definitions */

void main(void) {
  /* Disable the COP watchdog. Same reasoning as ../../blinky/main.c:
   * COPT is a two-bit field at SOPT1[7:6] (reset value 0xC0 = COP on,
   * longest timeout); there is no COPE/BKGDPE/RSTPE bit in SOPT1 on this
   * part, so writing 0x00 turns the COP off with no risk of also cutting
   * off BDM access. SOPT1 is write-once -- it must happen first. */
  SOPT1 = 0x00;

  PTADD_PTADD1 = 0; /* PTA1 = input  (pin 19) */
  PTAPE_PTAPE1 = 1; /* ...with its internal pull-up on, so an unconnected
                     * pin reads a defined 1 instead of floating */
  PTADD_PTADD0 = 1; /* PTA0 = output (pin 20) */

  for (;;) {
    /* Read the input and drive the output to match. Reading PTAD returns
     * the PIN state for bits configured as inputs, so this really is the
     * voltage on pin 19, not a latch we wrote earlier. */
    PTAD_PTAD0 = PTAD_PTAD1;
  }
}
