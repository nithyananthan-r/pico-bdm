/*
 * timer_tpm/main.c -- TPM2 as a free-running time base, driving a
 *                     variable-duty square wave on PTA0
 *                     (S9S08SG8E2MTJ / MC9S08SG8 family)
 *
 * The busy-wait delay() in ../../blinky/main.c burns an uncalibrated number
 * of loop iterations -- it blinks, but you cannot say how fast. This is the
 * fix: TPM2 counts bus clocks in hardware, so the waveform is a known
 * number of bus cycles wide regardless of what the compiler did to the
 * loop, and changing the duty is one constant.
 *
 * HOW THE TIMING WORKS
 *   TPM2 clock  = bus clock / 128          (CLKSx = 01, PS = 111)
 *   period      = 65536 TPM2 ticks         (TPM2MOD = 0xFFFF, free-running)
 *   PTA0 high while TPM2CNT < PWM_DUTY, low above it -- so the duty cycle
 *   is simply PWM_DUTY / 65536.
 *
 * With the bus clock measured on this rig at 9,248,555 Hz (see below), one
 * TPM2 tick is 128 / 9248555 = 13.84 us, and one full 65536-tick sweep is
 * 0.907 s. PWM_DUTY = 0x4000 therefore gives a 0.907 s period that is high
 * for 0.227 s and low for 0.680 s -- slow enough to watch over the BDM link.
 *
 * "PRECISE" IS RELATIVE TO THE BUS CLOCK, AND THIS PART'S IS UNTRIMMED.
 * TPM2 counts bus cycles exactly. The bus clock itself, out of reset, is
 * the UNTRIMMED internal DCO divided down: the datasheet specs fdco_ut at
 * 25.6 / 36.86 / 42.66 MHz (min/typ/max, Table A-x), which after the ICS's
 * divide-by-2 and the reset BDIV=01 divide-by-2 lands the bus anywhere from
 * 6.4 to 10.7 MHz part-to-part. So this gives an exact cycle count and only
 * a roughly-known wall-clock time. Trim the ICS first if you need real
 * seconds -- see ../ics_oscillator/.
 *
 * Registers used, all verified against docs/mc9s08sg8.h (the real
 * CodeWarrior header for this part) on 2026-09-17:
 *   TPM2SC       $0060  status/control
 *                TPM2SC_CLKSx  merged 2-bit field, NOT a macro taking an
 *                              argument -- write `TPM2SC_CLKSx = 1;`
 *                TPM2SC_PS     merged 3-bit prescaler field, same form
 *   TPM2CNT      $0061  16-bit free-running counter (read-only value;
 *                       ANY write to it clears the counter)
 *   TPM2MOD      $0063  16-bit modulo
 *   PTAD/PTADD   $0000/$0001
 *
 * Bit encodings verified against MC9S08SG8 datasheet Rev. 8:
 *   Table 16-6 TPM-Clock-Source Selection -- CLKSB:CLKSA
 *       00 No clock selected (TPM counter disabled)
 *       01 Bus rate clock            <-- what this example uses
 *       10 Fixed system clock
 *       11 External source
 *   Table 16-7 Prescale Factor Selection -- PS2:PS1:PS0
 *       000=1  001=2  010=4  011=8  100=16  101=32  110=64  111=128
 *
 * Build: see README.md in this folder.
 */

#include <hidef.h>      /* for EnableInterrupts, disable via CodeWarrior */
#include "derivative.h" /* device-specific register/bit definitions */

/* Duty cycle as a fraction of the 65536-tick period. 0x4000 = 25%.
 * Change this one constant to vary the on/off ratio. */
#define PWM_DUTY 0x4000U

void main(void) {
  /* Disable the COP watchdog -- same reasoning as ../../blinky/main.c:
   * COPT is a two-bit field at SOPT1[7:6], there is no BKGDPE bit to lose,
   * and SOPT1 is write-once so this must come first. */
  SOPT1 = 0x00;

  PTADD_PTADD0 = 1; /* PTA0 (physical pin 20) = output */

  /* Free-running over the counter's full range. Set the modulo BEFORE
   * starting the clock so the first period is already the right length. */
  TPM2MOD = 0xFFFFU;

  /* Start TPM2. Writing TPM2SC as one byte sets the clock source and the
   * prescaler together and leaves TOIE/CPWMS clear:
   *   CLKSx = 01 -> bus rate clock   (Table 16-6)
   *   PS    = 111 -> divide by 128   (Table 16-7)
   * Written as the two named merged fields rather than a magic 0x0F so the
   * intent survives being read later. */
  TPM2SC_CLKSx = 1;
  TPM2SC_PS = 7;

  for (;;) {
    /* Read the 16-bit counter. The TPM latches the low byte when the high
     * byte is read, so this single 16-bit access is coherent -- the two
     * halves cannot come from different counter values. */
    if (TPM2CNT < PWM_DUTY) {
      PTAD_PTAD0 = 1;
    } else {
      PTAD_PTAD0 = 0;
    }
  }
}

/*
 * DOING THIS IN HARDWARE INSTEAD
 * TPM2 has two real PWM output channels, so the compare above can be done
 * by the timer with no CPU involvement at all: set TPM2C1SC_MS1x = 2 and
 * TPM2C1SC_ELS1x = 2 for edge-aligned PWM, put the duty in TPM2C1V, and the
 * waveform appears on TPM2CH1 = PTB4 (physical pin 8). That is the right
 * choice for a real design. It is not used here because nothing on the
 * pico-bdm rig is wired to PTB4 (CONTEXT.md Finding 117), so a
 * hardware-generated edge would be unobservable, whereas the software
 * version's effect on PTAD can be read straight back over BDM.
 */
