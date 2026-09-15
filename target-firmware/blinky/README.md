# blinky — minimal S9S08SG8E2MTJ bring-up test

A starter program for writing and building your own code for the target
chip, separate from pico-bdm itself (pico-bdm only *flashes* the `.s19`
this produces — it doesn't compile anything).

## Building it in CodeWarrior for Microcontrollers 6.3 (Special Edition)

1. File -> New -> Bareboard Project.
2. Device: search for **MC9S08SG8** (covers the SG8/SG4 family, which is
   what the S9S08SG8E2MTJ belongs to) and select it.
3. Connection: pick "None" / "Generic" -- you're not using CodeWarrior's
   debugger with this custom hardware (see CONTEXT.md at the repo root
   for why), only its compiler and linker.
4. Language: C. Skip Processor Expert (choose "no PE" / bareboard) --
   simpler for a first bring-up test, avoids generating extra files you'd
   have to also understand.
5. Finish the wizard, then replace the generated `main.c` under
   `<project>/Sources/` with [main.c](main.c) from this folder.
6. Build (Project -> Make, or the hammer icon). Watch the Build/Messages
   panel for the header path CodeWarrior generated for `derivative.h` --
   confirm it resolved to something like `MC9S08SG8.h` and not an error;
   that header is what defines `SOPT1`, `PTAD`, `PTADD_PTADD0`, etc.
7. The output `.s19` lands under `<project>/SG8_Data/Debug/Bin/` (project
   name and exact path vary slightly by wizard choices) -- that's the
   file to open in pico-bdm's **Flash** panel (`host/templates/index.html`,
   currently running at http://127.0.0.1:5000 -- start it with
   `python host/app.py` if it's not already running).

## Before you trust it beyond blinking

- The `SOPT1 = 0x00` watchdog-disable line in [main.c](main.c) is
  **confirmed correct** against the MC9S08SG8 datasheet §5.7.3 (checked
  2026-09-11): on this family the COP is controlled by a two-bit `COPT`
  field at SOPT1[7:6] that resets to 1:1 (so SOPT1 resets to 0xC0), and
  there is no `COPE`, `BKGDPE` or `RSTPE` bit in the register — so writing
  0x00 disables the COP with no risk of also disabling BDM access. The
  earlier "unverified, check the datasheet" caveat here and in the source
  has been removed.
- Adjust `PTADD_PTADD0`/`PTAD_PTAD0` to whichever pin you actually have
  something (LED, scope probe) wired to -- PTA0 was picked as a generic
  example, not because it matches your board.
- Flash it with **Erase mode: touched pages** (the pico-bdm UI default).
  Every region is read back and verified on the Pico before programming
  reports success, so a bad write fails loudly instead of silently.
