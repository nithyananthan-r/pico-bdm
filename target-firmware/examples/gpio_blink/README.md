# gpio_blink — GPIO input with pull-up, mirrored to an output pin

PTA1 (physical pin 19) is an input with its internal pull-up enabled; PTA0
(physical pin 20) is an output that continuously mirrors it.

**Status: verified on real hardware.**

## Why this and not another blinker

The plain "toggle a pin in a busy-wait loop" program already exists and is
proven on this rig — [`../../blinky/`](../../blinky/). Copying it here
would add nothing, so this example covers the half of GPIO that blinky does
not: **reading** a pin, enabling its **pull-up**, and driving another pin
from the result. Start from `../../blinky/main.c` if all you want is an
output.

## Building it in CodeWarrior for Microcontrollers 6.3 (Special Edition)

Same wizard steps as [`../../blinky/README.md`](../../blinky/README.md):
File -> New -> Bareboard Project; device **MC9S08SG8**; connection
"None"/"Generic"; language C, no Processor Expert; replace the generated
`Sources/main.c` with [main.c](main.c); Project -> Make. Expect **0 errors,
0 warnings**. The `.s19` lands under
`<project>\<Derivative>_Data\Debug\Bin\*.abs.s19`.

## Port A is four bits wide on this part

`docs/mc9s08sg8.h` defines `PTAD_PTAD0` through `PTAD_PTAD3` and nothing
above — there is no `PTAD_PTAD4..7`, and the same goes for `PTADD` and
`PTAPE`. This matches the 20-TSSOP pinout (CONTEXT.md Finding 116), where
Port A appears only as PTA0–PTA3 on pins 20, 19, 18 and 17. **Port B is the
full eight bits.** Note that pico-bdm's own live-state panel reports PTA0
through PTA7 because it prints the raw register byte; the top four bits are
not real pins on this device.

Registers used, all verified against `docs/mc9s08sg8.h`:

| Register | Address | Used for |
|---|---|---|
| `PTAD` | `$0000` | read the input pin / drive the output |
| `PTADD` | `$0001` | data direction |
| `PTAPE` | `$1840` | internal pull-up enable |

## Why the pull-up is the point

Nothing on the pico-bdm rig is wired to any port pin — only RESET, BKGD and
VDD are connected (CONTEXT.md Finding 117). A floating CMOS input reads an
arbitrary, drifting value, so without `PTAPE_PTAPE1 = 1` this program would
have no defined behaviour to check at all. With the pull-up on, the
unconnected input is held high and PTA0 must sit at a steady 1 — an
unambiguous result, with no wires added.

## What was actually measured

Flashed (174 bytes at `$E000`, verified byte-for-byte on read-back). The
BDM link would not re-establish against this program while it was
free-running, so it was verified instead by **single-stepping from reset**,
which keeps the target in active background mode and the link solid
throughout:

```
after 57 steps (startup code + main's port configuration):
   PTADD = 0x01     PTA0 output, PTA1 input          as intended
   PTAPE = 0x02     pull-up enabled on PTA1          as intended
   PTAD  = 0x02     PTA1 reads HIGH -- the pull-up is holding the
                    unconnected input up, exactly as designed

two steps further, once the mirror loop has run:
   PTAD  = 0x03     PTA0 (output) now follows PTA1 (input) = 1
```

`PTAD = 0x03` is the whole example working end to end: a pulled-up input
read back as 1 and copied onto an output.

### A bonus demonstration of the same point

Intermediate samples showed PTAD bit 2 flickering (values `0x06`, and later
`0x04`/`0x05` while blinky was running). PTA2 is an unconnected input with
**no** pull-up enabled — so it floats and reads whatever it likes, sample to
sample. That is precisely the failure mode the pull-up on PTA1 exists to
avoid, visible side by side in the same register.

## To exercise it properly

Ground pin 19 and PTA0 follows it low; release it and the pull-up takes
PTA0 back high. On this rig that needs a jumper, since pin 19 is not
connected to anything.
