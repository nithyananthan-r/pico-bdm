/*
 * sci_uart/main.c -- SCI (serial / UART) transmit: send a string out TxD
 *                    (S9S08SG8E2MTJ / MC9S08SG8 family)
 *
 * *** NOT VERIFIED AGAINST A REAL RECEIVER. Nothing is wired to the SCI ***
 * *** pins on the pico-bdm rig -- see the honesty note at the bottom.   ***
 *
 * Transmits "HELLO\r\n" in a loop at 9600 8N1, then pauses, forever.
 *
 * PINS (from the project's own confirmed 20-TSSOP pinout, CONTEXT.md
 * Finding 116, which was read out of datasheet Table 2-1 rather than
 * inherited from a previous note):
 *   PTB1 / TxD = physical pin 15   <-- this example drives this one
 *   PTB0 / RxD = physical pin 16
 * The SCI takes over the pin automatically when SCIC2_TE is set; you do NOT
 * set PTBDD for TxD yourself, and doing so is not what enables the output.
 *
 * BAUD RATE
 *   baud = f_bus / (16 x SBR)     where SBR is the 13-bit SCIBD field
 *   For 9600 baud at this rig's measured 9,248,555 Hz bus clock:
 *       SBR = 9248555 / (16 x 9600) = 60.2  ->  60
 *       actual baud = 9248555 / (16 x 60) = 9634  (+0.35%, well inside the
 *       ~2% a UART receiver tolerates)
 *
 *   BUT: that bus clock is the UNTRIMMED internal DCO (see
 *   ../ics_oscillator/) and the datasheet allows fdco_ut to vary from
 *   25.6 to 42.66 MHz -- a +/-20% spread. A 20% baud error is far outside
 *   what any UART will decode. SO: TRIM THE ICS BEFORE RELYING ON THIS AT
 *   ALL. Deriving SBR from a nominal bus clock that was never measured is
 *   the single most common reason a first S08 UART brings up garbage.
 *
 * Registers used, all verified against docs/mc9s08sg8.h (the real
 * CodeWarrior header for this part) on 2026-09-17:
 *   SCIBD   $0038  16-bit; SCIBDH ($0038) / SCIBDL ($0039) as bytes.
 *                  SBR is 13 bits: SCIBDH_SBR (bits 4:0 of the high byte,
 *                  = SBR12:SBR8) plus all eight bits of SCIBDL.
 *                  SCIBDH's top bits are LBKDIE/RXEDGIE, NOT baud bits --
 *                  which is why this writes SCIBDH and SCIBDL separately
 *                  rather than storing a 16-bit value into SCIBD.
 *   SCIC1   $003A  SCIC1_M (0 = 8-bit data), SCIC1_PE (0 = no parity),
 *                  SCIC1_LOOPS, SCIC1_SCISWAI, ...
 *   SCIC2   $003B  SCIC2_TE (transmitter enable), SCIC2_RE, SCIC2_TIE, ...
 *   SCIS1   $003C  SCIS1_TDRE (bit 7, transmit data register empty),
 *                  SCIS1_TC (bit 6, transmission complete)
 *   SCID    $003F  data register -- write to transmit
 *
 * Build: see README.md in this folder.
 */

#include <hidef.h>      /* for EnableInterrupts, disable via CodeWarrior */
#include "derivative.h" /* device-specific register/bit definitions */

/* SBR divisor for ~9600 baud at a 9.25 MHz bus -- see the note above about
 * why this MUST be recomputed for your actual, measured bus clock. */
#define SCI_SBR 60U

static void sci_init(void) {
  /* Set the baud divisor while the transmitter and receiver are still
   * disabled (SCIC2 is 0x00 out of reset), which is the safe order --
   * changing SBR mid-character corrupts it.
   *
   * Written as two byte writes: the high byte carries only SBR12:SBR8 in
   * its low five bits, and writing 0 to the rest also leaves the LIN break
   * and RxD edge interrupts disabled, which is what we want. */
  SCIBDH = (unsigned char)((SCI_SBR >> 8) & 0x1FU);
  SCIBDL = (unsigned char)(SCI_SBR & 0xFFU);

  /* 8 data bits, no parity, one stop bit ("8N1"). All of these are the
   * reset state of SCIC1 already; assigned explicitly so the frame format
   * is stated in the code rather than assumed. */
  SCIC1 = 0x00;

  /* Enable the transmitter. This is also what hands PTB1 over to the SCI
   * and drives the idle-high line state. */
  SCIC2_TE = 1;
}

static void sci_putc(unsigned char c) {
  /* Wait for room in the transmit data register. TDRE is set when SCID can
   * accept another byte -- it does NOT mean the previous byte has finished
   * going out on the wire (that is TC). Polling TDRE is what lets the SCI
   * double-buffer: the next byte is loaded while the current one shifts. */
  while (!SCIS1_TDRE) {
    /* wait */
  }
  /* Reading SCIS1 above and then writing SCID is the documented sequence
   * that clears TDRE. */
  SCID = c;
}

static void sci_puts(const char *s) {
  while (*s != '\0') {
    sci_putc((unsigned char)*s);
    s++;
  }
}

static void delay(unsigned int loops) {
  volatile unsigned int i = loops; /* volatile: see ../../blinky/main.c */
  while (i--) {
  }
}

void main(void) {
  /* Disable the COP watchdog -- same reasoning as ../../blinky/main.c. */
  SOPT1 = 0x00;

  sci_init();

  for (;;) {
    sci_puts("HELLO\r\n");

    /* Wait until the last byte has actually left the shifter before going
     * idle. TC (not TDRE) is the "the wire is finished" flag -- this is the
     * one to poll before, say, disabling the transmitter or dropping a
     * half-duplex driver enable. */
    while (!SCIS1_TC) {
      /* wait */
    }

    delay(30000);
  }
}

/*
 * ===================== WHAT IS AND IS NOT VERIFIED =====================
 * NOT verified: that real, correctly-framed bytes appear on TxD. The
 * pico-bdm rig has exactly three wires to the target -- RESET (pin 1),
 * BKGD (pin 2) and VDD (pin 3) (CONTEXT.md Finding 117). PTB1/TxD
 * (pin 15) is not connected to anything, so there is no receiver and no
 * scope on it, and no amount of running this proves the waveform is right.
 *
 * To actually check it: wire PTB1 (pin 15) and a common ground to a 3.3 V
 * USB-serial adapter, open it at 9600 8N1, and expect "HELLO" once a
 * second or so. If you get consistent garbage, suspect the baud rate
 * before the code -- recompute SCI_SBR from your MEASURED bus clock.
 * =======================================================================
 */
