# Reference documents

Primary sources for every register address, bit meaning, and timing figure
used in this project. When CONTEXT.md or a code comment cites a section,
table, or page number, it's citing one of these two PDFs — check the
document, not a paraphrase of it, before changing anything that touches
hardware.

- **`MC9S08SG8_datasheet_rev8.pdf`** — MC9S08SG8/SG4 MCU Series Data Sheet,
  Rev. 8 (NXP/Freescale). The device-specific reference: pinout (Table 2-1),
  memory map (Figure 4-1), FLASH programming (Chapter 4), BDC/BKGD protocol
  and development support (Chapter 17), and every peripheral register
  (ICS, RTC, TPM, SCI, ADC, etc.) with its real address and bit layout.
  This is the one to open first for anything chip-specific.

- **`HCS08_Family_Reference_Manual.pdf`** — HCS08 Family Reference Manual
  (HCS08RMv1), the architecture-level companion to the datasheet above:
  CPU instruction set, the BDC opcode table (Table 7-1), and BDC command
  timing that the datasheet only summarizes.

- **`mc9s08sg8.h`** — the actual CodeWarrior-generated C header for this
  part (from `CodeWarrior for Microcontrollers V6.3\lib\hc08c\device\include\`).
  This is the ground truth for register/bit-field **macro names** used in
  target C code (`RTCSC_RTCLKS`, `ICSC2_BDIV`, etc.) and for the exact
  address of every register — cross-check a macro or address against this
  file before using it in new target firmware, rather than guessing a name
  by analogy with another Freescale part's header. A wrong guess here
  produced a real compile error and, before that, a misdiagnosed "RAM
  defect" (see CONTEXT.md's correction to Finding 92) — both traced back to
  assuming a register layout instead of reading this file.

## Why these live in the repo instead of a temp folder

Every one of these was previously sitting only in a local scratch/temp path
outside the repository, which meant a fresh session (or a background agent
in its own worktree) had no way to reach them. They're checked into
`docs/` so any future session — human or agent — has them without
re-downloading or re-locating anything.
