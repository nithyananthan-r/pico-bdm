/*
 * adc_read/main.c -- ADC single-conversion reads, using the INTERNAL
 *                    reference channels so the result can be checked with
 *                    no analog signal wired up at all
 *                    (S9S08SG8E2MTJ / MC9S08SG8 family)
 *
 * The obvious ADC example reads a potentiometer on an external pin -- which
 * proves nothing on a rig where no port pin is wired (CONTEXT.md Finding
 * 117), because a floating input returns whatever it feels like. This one
 * is built to be self-checking instead.
 *
 * The ADC's channel mux has INTERNAL inputs that need no external circuit:
 *   ADCH = 29 (11101) -> VREFH, which is tied to VDD on this part
 *   ADCH = 30 (11110) -> VREFL, which is tied to VSS
 * Converting those two is a complete end-to-end test of the ADC: in 10-bit
 * mode VREFH must come back at or very near full scale (0x3FF = 1023) and
 * VREFL at or very near 0x000, because they ARE the two ends of the
 * converter's own reference. Anything else means the ADC is misconfigured.
 * Channel 26 is the on-chip temperature sensor and 27 the internal bandgap;
 * both are also read here as a bonus, though their values need the
 * datasheet's conversion formulae to interpret.
 *
 * WHERE THE RESULTS GO
 * Each conversion is stored to a fixed RAM window at $0100 so it can be
 * read straight back over BDM (/api/read_block?addr=0x0100&len=8) while the
 * target runs -- no UART, no debugger, no wiring:
 *   $0100/$0101  VREFH result, high byte then low   (expect 0x03 0xFF-ish)
 *   $0102/$0103  VREFL result                        (expect 0x00 0x00-ish)
 *   $0104/$0105  temperature sensor (AD26)
 *   $0106/$0107  internal bandgap  (AD27)
 *   $0108        increments once per pass, so a reader can tell the loop
 *                is live and not see a stale snapshot
 * $0100 is inside RAM ($0080-$027F per datasheet Figure 4-1). RAM does NOT
 * start at $0060 -- that is TPM2/RTC register space; see CONTEXT.md
 * Finding 120.
 *
 * Registers used, all verified against docs/mc9s08sg8.h (the real
 * CodeWarrior header for this part) on 2026-09-17:
 *   ADCSC1  $0010  ADCSC1_ADCH (merged 5-bit channel select),
 *                  ADCSC1_COCO (bit 7, conversion complete, read-only),
 *                  ADCSC1_AIEN, ADCSC1_ADCO (continuous conversion)
 *                  WRITING ADCSC1 IS WHAT STARTS A CONVERSION.
 *   ADCSC2  $0011  ADCSC2_ADACT, ADCSC2_ADTRG, ADCSC2_ACFE, ADCSC2_ACFGT
 *   ADCR    $0012  16-bit result; ADCRH ($0012) / ADCRL ($0013) as bytes
 *   ADCCFG  $0016  ADCCFG_ADICLK (2-bit), ADCCFG_MODE (2-bit),
 *                  ADCCFG_ADIV (2-bit), ADCCFG_ADLSMP, ADCCFG_ADLPC
 *   SPMSC1  $1809  SPMSC1_BGBE -- bandgap buffer enable (needed for AD27)
 *   APCTL1  $0017  ADPC0..7 -- disables the digital input buffer on pins
 *                  used as analog inputs. NOT needed here: the internal
 *                  channels have no pin, so there is no digital buffer to
 *                  turn off. You WOULD need it for a real external channel.
 *
 * Channel numbers verified against MC9S08SG8 datasheet Rev. 8, the ADCH
 * channel-select table in Chapter 9:
 *   01010 AD10 PTC2/ADP10    11010 AD26  Temperature Sensor
 *   01011 AD11 PTC3/ADP11    11011 AD27  Internal Bandgap
 *                            11101 VREFH (= VDD)
 *                            11110 VREFL (= VSS)
 *                            11111 Module Disabled
 *
 * Build: see README.md in this folder.
 */

#include <hidef.h>      /* for EnableInterrupts, disable via CodeWarrior */
#include "derivative.h" /* device-specific register/bit definitions */

#define ADC_RESULTS ((volatile unsigned char *)0x0100)

/* Channel numbers -- see the table in the header comment above. */
#define ADCH_TEMP    26U
#define ADCH_BANDGAP 27U
#define ADCH_VREFH   29U
#define ADCH_VREFL   30U
#define ADCH_OFF     31U /* 11111 = module disabled (its reset value) */

static unsigned int adc_convert(unsigned char channel) {
  /* Writing ADCSC1 starts a conversion on the selected channel. ADCO stays
   * 0, so this is a SINGLE conversion; AIEN stays 0, so completion is
   * polled rather than taking an interrupt. Assigning the whole byte (not
   * just the ADCH field) guarantees ADCO and AIEN really are clear. */
  ADCSC1 = channel; /* ADCH = channel, ADCO = 0, AIEN = 0 */

  /* COCO goes high when the result is ready. Reading ADCR (specifically
   * ADCRL) is what clears COCO, so there is no flag to clear by hand. */
  while (!ADCSC1_COCO) {
    /* wait -- a 10-bit conversion is ~20 ADCK cycles */
  }

  /* Read the 16-bit result. ADCRH must be read before ADCRL; the 16-bit
   * access through ADCR does that in the right order. */
  return (unsigned int)ADCR;
}

static void store(unsigned char slot, unsigned int value) {
  ADC_RESULTS[slot]     = (unsigned char)(value >> 8);
  ADC_RESULTS[slot + 1] = (unsigned char)(value & 0xFFU);
}

void main(void) {
  unsigned char pass = 0;

  /* Disable the COP watchdog -- same reasoning as ../../blinky/main.c. */
  SOPT1 = 0x00;

  /* Enable the bandgap buffer so channel AD27 has something to measure.
   * This lives in SPMSC1 ($1809), not in the ADC's own registers -- an easy
   * one to miss, and without it AD27 reads meaningless. Only BGBE is set;
   * the other SPMSC1 bits (LVD configuration) keep their reset values. */
  SPMSC1_BGBE = 1;

  /* ADC configuration:
   *   ADICLK = 01 -> bus clock / 2 as the conversion clock (ADCK)
   *   ADIV   = 01 -> divide that by 2 again
   *   so ADCK = bus / 4 = ~2.3 MHz at this rig's 9.25 MHz bus, comfortably
   *   inside the ADC's specified fADCK range: Table A-12 gives 0.4 MHz min
   *   to 8.0 MHz max with ADLPC = 0 (high speed), or 4.0 MHz max with
   *   ADLPC = 1. Running ADCK too FAST is the usual cause of noisy or
   *   plainly wrong results.
   *   MODE   = 10 -> 10-bit conversion  (Table 9-7: 00 = 8-bit,
   *                  01 = Reserved, 10 = 10-bit, 11 = Reserved -- note
   *                  10-bit is NOT encoding 01, which is easy to assume)
   *
   * CAVEAT FOR THE TEMPERATURE CHANNEL ONLY: datasheet section 9.1.4 says
   * the temperature sensor must be read "with long sample and a maximum of
   * 1 MHz clock". ADCK here is ~2.3 MHz, which is fine for VREFH, VREFL and
   * the bandgap (all well inside the 0.4-8.0 MHz fADCK spec) but is ABOVE
   * what 9.1.4 asks for, so the AD26 value this example records is not a
   * datasheet-compliant temperature measurement. To make it one, change
   * ADCCFG_ADIV to 3 (divide by 8), giving ADCK = bus/2/8 = ~578 kHz. It is
   * left at 1 here so the code matches the build that was actually measured
   * on hardware -- see README.md.
   *   ADLSMP = 1  -> long sample time. Costs conversion rate, buys settling
   *                  time for a high-impedance source; the safe default
   *                  when you are not chasing throughput. */
  ADCCFG_ADICLK = 1;
  ADCCFG_ADIV   = 1;
  ADCCFG_MODE   = 2;
  ADCCFG_ADLSMP = 1;

  /* Software trigger (ADTRG = 0) and compare function off -- both are the
   * reset state of ADCSC2; set explicitly so the trigger source is stated. */
  ADCSC2 = 0x00;

  PTADD_PTADD0 = 1; /* PTA0 = output: a heartbeat, so it is obvious over BDM
                     * that the loop is running and not stuck in a poll */

  for (;;) {
    store(0, adc_convert(ADCH_VREFH));    /* expect ~0x03FF */
    store(2, adc_convert(ADCH_VREFL));    /* expect ~0x0000 */
    store(4, adc_convert(ADCH_TEMP));
    store(6, adc_convert(ADCH_BANDGAP));

    pass++;
    ADC_RESULTS[8] = pass;
    PTAD_PTAD0 = (unsigned char)(pass & 1U);
  }
}
