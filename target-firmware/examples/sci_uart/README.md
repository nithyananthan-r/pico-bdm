# sci_uart — SCI (serial / UART) transmit

Sends `"HELLO\r\n"` out TxD in a loop at ~9600 8N1, polling `SCIS1_TDRE`
between bytes and `SCIS1_TC` before going idle.

**Status: partially verified on real hardware.** The SCI is confirmed
configured and actively transmitting, but **no real bytes were received or
scoped**, because nothing is wired to the TxD pin. Details below — do not
read this as "the UART is proven working".

## Building it in CodeWarrior for Microcontrollers 6.3 (Special Edition)

Same wizard steps as [`../../blinky/README.md`](../../blinky/README.md):
File -> New -> Bareboard Project; device **MC9S08SG8**; connection
"None"/"Generic"; language C, no Processor Expert; replace the generated
`Sources/main.c` with [main.c](main.c); Project -> Make. Expect **0 errors,
0 warnings**. The `.s19` lands under
`<project>\<Derivative>_Data\Debug\Bin\*.abs.s19`.

## Pins

From this project's own confirmed 20-TSSOP pinout (CONTEXT.md Finding 116,
read out of datasheet Table 2-1):

- **PTB1 / TxD = physical pin 15** — driven by this example
- **PTB0 / RxD = physical pin 16**

The SCI takes the pin over automatically when `SCIC2_TE` is set. You do not
set `PTBDD` for TxD yourself, and doing so is not what enables the output.

## Baud rate

```
baud = f_bus / (16 x SBR)
```

`SBR` is a **13-bit** field spread across two registers: `SCIBDH` bits 4:0
(= SBR12:SBR8) and all eight bits of `SCIBDL`. `SCIBDH`'s top bits are
`LBKDIE` and `RXEDGIE`, not baud bits — which is why [main.c](main.c)
writes the two bytes separately and masks the high one, rather than storing
a 16-bit value into `SCIBD`.

For 9600 baud at this rig's measured 9,248,555 Hz bus clock:
`SBR = 9248555 / (16 x 9600) = 60.2 -> 60`, giving an actual 9634 baud
(+0.35%, well inside the ~2% a receiver tolerates).

**Recompute `SCI_SBR` for your own measured bus clock before trusting
this.** The reset bus clock is the untrimmed internal DCO (see
[`../ics_oscillator/`](../ics_oscillator/)), and the datasheet allows
`fdco_ut` to vary ±20% part to part. A 20% baud error is far outside what
any UART will decode, and deriving `SBR` from a nominal bus clock that was
never measured is the most common reason a first S08 UART brings up
garbage.

## What WAS verified

Flashed (247 bytes at `$E000`, verified byte-for-byte on read-back), left
running, and the SCI registers read back over BDM:

```
SCIBD = 0x003C = 60      exactly the computed divisor  -> ~9634 baud
SCIC1 = 0x00             8 data bits, no parity, 1 stop bit (8N1)
SCIC2 = 0x08             TE = 1, transmitter enabled
SCIS1 = 0xC0   (twice)   TDRE = 1 and TC = 1 -- transmitter idle/complete
SCIS1 = 0x00   (once)    TDRE = 0 and TC = 0 -- caught MID-BYTE, with the
                         data register full and the shifter busy
```

That last sample is the meaningful one: catching `SCIS1 = 0x00` shows the
transmitter was actually shifting a character out at that instant, not
merely sitting configured. So the SCI is enabled, clocked at the intended
divisor, and cycling through busy/complete states as it sends.

Every register and bit-field macro used was checked against
`docs/mc9s08sg8.h` before use: `SCIBD`/`SCIBDH`/`SCIBDL` (`$0038`),
`SCIC1` (`$003A`), `SCIC2` with `SCIC2_TE` (`$003B`), `SCIS1` with
`SCIS1_TDRE`/`SCIS1_TC` (`$003C`), `SCID` (`$003F`).

## What was NOT verified

**That correctly-framed bytes appear on the wire.** The rig has exactly
three wires to the target — RESET (pin 1), BKGD (pin 2) and VDD (pin 3)
(CONTEXT.md Finding 117). PTB1/TxD is not connected to anything, so there
is no receiver and no scope on it. Bit timing, framing, polarity and idle
level are all unconfirmed, and nothing short of an instrument on pin 15
would confirm them.

To check it for real: wire PTB1 (pin 15) and a common ground to a 3.3 V
USB-serial adapter, open it at 9600 8N1, and expect `HELLO` roughly once a
second. If you get consistent garbage, suspect the baud rate before the
code.
