"""
bdc_pio.py — PIO state machine programs implementing the Freescale/NXP S08
Background Debug Controller (BDC) single-wire protocol on the BKGD pin.

Runs on the Raspberry Pi Pico (RP2040) under MicroPython.

------------------------------------------------------------------------
PROTOCOL SUMMARY (bit timings VERIFIED against HCS08RMv1 §7.3.3/§7.3.4;
the *logic* built on top of them in this file and in bdc.py has NOT been
run against a real target yet — see CONTEXT.md for the honest
verified-vs-unverified split):

  * Single open-drain wire (BKGD). Idle state = high (external/internal
    pull-up). A falling edge from the host marks the start of every bit
    time.
  * Data is MSB-first, 16 target-BDC-clock cycles per bit.
  * Host -> target (write) bit:
        '1' : host drives low for  1 target cycle,  then releases
        '0' : host drives low for 13 target cycles, then releases
    Target samples the line around cycle 10 of the 16-cycle window.
  * Target -> host (read) bit:
        host drives low for 1 target cycle (to mark bit start) then
        releases; the target then either releases immediately (bit=1) or
        holds the line low until ~cycle 10 (bit=0). Host samples the pin
        around cycle 10.
  * SYNC (HCS08RMv1 §7.3.4.1): host drives BKGD low for >=128 target
    cycles, briefly drives it HIGH (the "speedup pulse", for a fast rise
    time), then releases to Hi-Z. The target then: waits for BKGD high,
    delays 16 of its own BDC cycles, and drives the line low for exactly
    128 BDC cycles. The host measures that low pulse to compute the
    target's BDC clock period.
------------------------------------------------------------------------

DESIGN: rather than hard-coding cycle counts into the compiled PIO
program (which would require recompiling per target clock speed), the
low-hold duration and total bit-period duration are loaded into the X/Y
scratch registers at runtime from the TX FIFO. This lets one compiled
program work at any target clock speed — you just push different
counts after SYNC calibration.

========================================================================
PIO BLOCK ALLOCATION — read before adding or resizing any program here
========================================================================
Each RP2040 PIO block has exactly **32 instruction slots**, shared by its
4 state machines, and MicroPython never frees a program once loaded (it
caches the load offset per program per block, so re-constructing the same
StateMachine is free, but a *different* program costs more slots). The
original code loaded all three driving programs onto block 0
(26 + 14 + 8 = 48 > 32), which silently fails to load the second/third
program the moment a real target lets execution get that far.

Current deliberate assignment:

    PIO block 0 (state machines 0-3)
        bdc_sync        27 instructions   (SM_SYNC = 0)
        -> 27 / 32 used. Left alone on its own block because it is the
           largest program and the one most likely to grow.

    PIO block 1 (state machines 4-7)
        bdc_sample       5 instructions   (SM_CAPTURE  = 4)
        bdc_sample2      5 instructions   (SM_CAPTURE2 = 7)
        bdc_tx_byte     16 instructions   (SM_TX       = 5)
        -> 26 / 32 used. `bdc_rx_bits` (7 instructions, SM_RX = 6) was
           deleted on 2026-09-15: it was a SECOND state machine for the
           read phase, and handing BKGD over to it is what stopped the
           target answering. bdc_tx_byte now samples as it transmits, so
           one SM does the whole transaction. See the comment where
           bdc_rx_bits used to be, and CONTEXT.md Finding 79.

========================================================================
WHY THE BIT-AT-A-TIME PROGRAMS ARE GONE (2026-09-12)
========================================================================
There used to be a `bdc_write_bit` and a `bdc_read_bit` here, each doing
exactly one bit, with the low/total durations pushed through the FIFO so
one compiled program could serve any target clock. `bdc.py` built a brand
new `StateMachine` object for **every single bit** and tore it down again.

Measured on real hardware with a real target synced at 16.33 MHz: a bit
is 1 us of signal and took **332 us**. A BDC command byte was smeared over
2.7 ms instead of 8 us. See CONTEXT.md "Finding 6".

The replacement clocks the state machine at **exactly one PIO cycle per
target BDC cycle**. That is the whole trick: the protocol is defined in
whole BDC cycles (1 / 13 / 16, sample at 10), so at 1:1 every one of those
becomes a plain `[delay]` count that fits in PIO's 5-bit delay field, no
FIFO-loaded counters and no per-bit Python needed. A whole byte goes out
from one FIFO word, and up to 4 bytes can be queued so a complete
`READ_BYTE <addr16>` leaves the Pico with no gaps at all.

The cost is that the bit shapes now quantise to whole target cycles
instead of `PIO_CYCLES_PER_TARGET_CYCLE` sub-steps. That costs nothing:
every edge the protocol specifies already falls on a whole cycle.

Both programs' waveforms were verified against a scope capture of the real
line at 120 ns/sample: 0xE4 came back as `1110 010(0)`, MSB first, with
correct 3-cycle and 13-cycle lows. See CONTEXT.md "Finding 10".

Concurrency note: programs only need *separate state machines* to run at
the same time, not separate PIO blocks — SMs on one block share
instruction memory but execute independently and have independent pin
mappings. (The old comment here claimed otherwise; it was wrong.) The
capture program deliberately sits on the same block as the bit programs
it watches, which is fine because it only ever reads the pin.

If you add a program or make one bigger, re-count the block it lands on.
"""

import rp2
from rp2 import PIO, StateMachine, asm_pio
from machine import Pin, mem32
import time

# ---------------------------------------------------------------------------
# BKGD pad configuration -- see CONTEXT.md Finding 69.
#
# The RP2040's power-on default for EVERY pad is PUE=0 / PDE=1, i.e. an
# internal ~60 kohm PULL-DOWN. `pio_gpio_init()` does not touch the pull
# bits, and MicroPython's `Pin(n, Pin.IN)` leaves them alone unless you pass
# an explicit `pull=`. So a pin that only ever goes through PIO -- which is
# exactly what BKGD does -- keeps the reset-default pull-DOWN forever, and
# it was measured live on this rig doing precisely that (GPIO15 PADS=0x56,
# PDE=1, while GPIO12/14 read 0x52, PDE=0).
#
# On a pseudo-open-drain single-wire bus fought only by the target's own
# on-chip pull-up (17.5-52 kohm per the HCS08 spec), a 60 kohm pull-down
# parks the "high" level at ~2.1-2.8 V. The RP2040's own Schmitt input
# calls that HIGH; the S08's VIH is 0.7 x VDD = 2.31 V, so the TARGET may
# not. That is the same divider trap Finding 48 identified on GPIO14.
#
# So: force PUE=1 / PDE=0 and the strongest drive on the BDM pins, every
# time a state machine is (re)bound, and make it switchable so the old
# behaviour can be measured back.
# ---------------------------------------------------------------------------
PADS_BANK0_BASE = 0x4001C000

# (pue, pde, drive) -- drive 0=2mA 1=4mA 2=8mA 3=12mA. Set via set_pad_policy().
PAD_POLICY = (1, 0, 3)


def set_pad_policy(pue=1, pde=0, drive=3):
    """Change the pad policy applied by apply_pad() from here on."""
    global PAD_POLICY
    PAD_POLICY = (1 if pue else 0, 1 if pde else 0, int(drive) & 3)
    return PAD_POLICY


def read_pad(pin_num):
    v = mem32[PADS_BANK0_BASE + 0x04 + 4 * pin_num] & 0xFF
    return {"pin": pin_num, "pads": v, "od": (v >> 7) & 1, "ie": (v >> 6) & 1,
            "drive": (v >> 4) & 3, "pue": (v >> 3) & 1, "pde": (v >> 2) & 1,
            "schmitt": (v >> 1) & 1}


def apply_pad(pin_num):
    """Apply PAD_POLICY to one pad. Cheap (one RMW), so call it liberally
    -- anything that constructs a Pin() can undo it."""
    pue, pde, drive = PAD_POLICY
    addr = PADS_BANK0_BASE + 0x04 + 4 * pin_num
    v = mem32[addr]
    v &= ~0x3C                       # clear DRIVE[5:4], PUE[3], PDE[2]
    v |= (drive << 4) | (pue << 3) | (pde << 2)
    v |= 0x40                        # IE=1, input buffer enabled (must stay on)
    mem32[addr] = v


# State machine ids. 0-3 are PIO block 0, 4-7 are PIO block 1. See the
# block-allocation table in the module docstring above before changing.
# Pre-encoded `set(pindirs, 0)` for StateMachine.exec().
#
# exec() accepts an instruction STRING, but it assembles that string on
# every call: measured on this board, one `sm.exec("set(pindirs, 0)")` at
# the end of every transaction put **9.2 ms** into a transfer whose signal
# is 30 us. Harmless for a handful of register reads; fatal for FLASH
# programming, which is tens of thousands of transfers.
#
# `asm_pio_encode` does the assembly once, here, and exec() takes the
# resulting integer directly. The second argument is the side-set/delay
# bit count the instruction was written for, which is 0.
SET_PINDIRS_IN = rp2.asm_pio_encode("set(pindirs, 0)", 0)

SM_SYNC = 0
SM_RAW = 1
SM_CAPTURE = 4
SM_TX = 5
SM_RX = 6


# ---------------------------------------------------------------------------
# bdc_raw_dirs: a totally unopinionated waveform player. ONE instruction.
#
# It shifts a stream of bits out of the TX FIFO, MSB first, one bit per PIO
# cycle, straight into `pindirs`:
#
#     bit = 1  ->  pindir = out, output latch is 0  ->  BKGD DRIVEN LOW
#     bit = 0  ->  pindir = in                      ->  BKGD Hi-Z (pull-up)
#
# Clock the SM at the target's BDC clock and one bit = one BDC cycle, so the
# host can describe ANY waveform cycle by cycle: any bit period, any '1'/'0'
# low width, any inter-byte gap, any deliberately malformed frame.
#
# WHY THIS EXISTS (2026-09-14). `bdc_tx_byte` hard-codes the protocol's
# 16-cycle bit with 3-cycle and 13-cycle lows as PIO `[delay]` counts, so the
# only thing a host can sweep is the *clock*, which scales the period and both
# low widths together and never changes their RATIO. If the target's sample
# point is not where we think it is, every bit decodes to the same value, every
# opcode arrives as 0x00 or 0xFF, and no amount of opcode/clock/phase sweeping
# can see it — which is exactly the shape of this project's dead end. This
# program makes the ratio sweepable.
#
# `fifo_join=PIO.JOIN_TX` gives an 8-word TX FIFO = 256 contiguous BDC cycles
# = two whole 16-cycle-per-bit bytes with no Python in between. There is no RX
# FIFO on this SM and it never needs one.
#
# Stalls with pindirs at the last bit shifted, so ALWAYS end the stream with a
# 0 bit or the line is left driven low.
# ---------------------------------------------------------------------------
@asm_pio(out_init=PIO.IN_LOW, out_shiftdir=PIO.SHIFT_LEFT,
         autopull=True, pull_thresh=32, fifo_join=PIO.JOIN_TX)
def bdc_raw_dirs():
    out(pindirs, 1)


def make_raw_state_machine(pin_num, freq, sm_id=SM_RAW):
    """bdc_raw_dirs on `pin_num`. Lives on PIO block 0 (SM 1) next to
    bdc_sync: 27 + 1 = 28 of block 0's 32 instruction slots."""
    p = Pin(pin_num, Pin.IN)
    apply_pad(pin_num)
    return StateMachine(sm_id, bdc_raw_dirs, freq=freq, out_base=p, in_base=p)


# ---------------------------------------------------------------------------
# bdc_tx_byte: the ONE program that does a whole BDC transaction.
#
# *** The state machine MUST be clocked at the target's BDC clock, i.e. one
# PIO cycle per target BDC cycle. *** Everything below counts in those
# cycles, and the whole design depends on that 1:1 ratio.
#
# Per bit, exactly 16 cycles:
#     '1' -> BKGD low for  3 cycles, then released (high) for 13
#     '0' -> BKGD low for 13 cycles, then released (high) for  3
# The target samples around cycle 10, so it sees high for a '1' and low for
# a '0'. A '1' could be as short as 1 cycle low; 3 is used because it gives
# the external pull-up a slightly kinder edge and is still nowhere near
# cycle 10.
#
# *** SPEED-UP PULSE (added 2026-09-13 — this was a real bug) ***
# The end of every host-driven low used to be a bare `set(pindirs, 0)`, i.e.
# release to Hi-Z and let the pull-up do the rising edge. Measured on the
# real board at 27 ns/sample with a 10k pull-up (R1) and ~30 pF of stray:
#
#     '1' bit: LOW 297-324 ns   (spec: 3 cycles = 178 ns at 16.9 MHz)
#     '0' bit: LOW 854-915 ns   (spec: 13 cycles = 770 ns)
#     gap between two consecutive '0' bits: **61 ns of high**, i.e. one
#     sample, out of a 178 ns window.
#
# ~130 ns of every rising edge was RC, which is 2.2 BDC cycles at 16.9 MHz.
# For back-to-back '0' bits that leaves the line barely, or at the target's
# higher VIH not at all, above threshold between bits — so the target sees
# one continuous low instead of a framed bit stream. The fix is a side-set
# bit mapped onto the same GPIO: side-set carries the output VALUE while
# `set(pindirs, ...)` carries the direction, so "drive high for one cycle,
# then release and restore the latch to 0" costs one instruction per path.
#
# *** IT ALSO SAMPLES (2026-09-15 — this is the whole ball game) ***
# Every bit, at cycle 10, this program reads BKGD back into the ISR. Cycle
# 10 is exactly where the BDC spec puts the host's sample point for a
# *target*-driven bit, so a transmitted 0xFF byte — whose bit shape (3
# cycles low, release, a 13-cycle window) IS the BDC read-bit marker —
# turns into a complete read phase, on this same state machine, fed from
# this same FIFO, with no handover and no Python anywhere.
#
# WHY THAT MATTERS. The read phase used to live on a second state machine
# (`bdc_rx_bits`, deleted) that had to be started after the TX SM stopped.
# Measured side by side on real hardware in state S (CONTEXT.md Finding
# 79): `[0xE4, 0xFF]` as one contiguous burst draws a genuine 13-cycle
# target-driven low in the read window; the identical waveform with an
# SM handover in the middle draws 46 us of dead silence and reads 0xFF.
# Do not reintroduce a two-SM read path.
#
# During our OWN command bytes the cycle-10 sample just reads back what we
# are driving ('1' -> released, reads high; '0' -> still low, reads low),
# so the sampled bytes for the command should equal the command itself.
# That is a free, per-transaction check that the bit grid is aligned;
# bdc.py's `_xfer` returns those bytes too and checks them.
#
# *** FOUR BYTES PER FIFO WORD? NO — TRIED AND REVERTED (2026-09-15) ***
# There was a version of this program that packed four transmitted bytes
# into each 32-bit FIFO word (autopull/autopush at a 32-bit threshold, the
# byte boundary existing only in the FIFO thresholds), so a 16-byte burst
# could preload whole. It was 13 instructions and it looked strictly
# better. On hardware it was a clear regression: READ_STATUS went from a
# rock-steady 0xD6 to 0xD5/0xD6/0xCD run to run, and every READ_BYTE came
# back as alternating-bit mush (0x55 0xAA 0x52 0x25 ...). The echo check
# still passed, so our own drive and our own sampling were fine; what
# broke was the TARGET's decoding of our '0' bits.
#
# The cause is the two cycles of `out` + `jmp` overhead. In the layout
# below they are absorbed INSIDE the bit's low period (cycles 1 and 2,
# when both bit shapes are low anyway), which is why this version fits a
# 16-cycle bit with a 13-cycle '0' low. Moving `out` to the top of the
# loop to get a clean stall point costs one cycle at the end, and paying
# for it out of the '0' low — 13 cycles down to 12 — is apparently past
# this target's sampling window for a host-driven '0'.
#
# So: 13 cycles low for a '0', not 12, and the FIFO stays one byte per
# word. Four bytes is enough for every BDC command this driver issues,
# because memory reads fetch their data with READ_LAST (`E8/SS/RD`, no
# delay) rather than with READ_BYTE's own delayed data phase. See
# bdc.py::read_byte.
#
# The cost of keeping `out` inside the low is that an empty TX FIFO stalls
# the SM at cycle 1, holding BKGD LOW. That is why the `pull(block)` at
# the top is load-bearing: it is the per-byte stall point, and it stalls
# with the line released.
#
# ---------------------------------------------------------------------------
@asm_pio(set_init=PIO.IN_LOW, sideset_init=PIO.IN_LOW,
         out_shiftdir=PIO.SHIFT_LEFT, in_shiftdir=PIO.SHIFT_LEFT,
         autopull=False, autopush=True, push_thresh=8)
def bdc_tx_byte():
    pull(block)                    .side(0)     # next byte -> OSR (stall
                                                # point; BKGD released here)
    set(y, 7)                      .side(0)     # 8 bits to send
    label("bit")
    set(pindirs, 1)                .side(0)     # cyc 0   drive BKGD low
    out(x, 1)                      .side(0)     # cyc 1   X = data bit
    jmp(not_x, "zero")             .side(0)     # cyc 2
    # ---- '1' / read marker: 3 cycles low, then Hi-Z. NO SPEED-UP. ----
    #
    # There used to be a `.side(1)` here, driving BKGD high for one cycle
    # before releasing it, same as the '0' path below. On a bit we are
    # merely transmitting that is harmless. On a READ MARKER it is a
    # collision: a '1' bit and a read marker are the same waveform, so
    # during a read phase this drove the RP2040's 12 mA output HIGH into
    # the target's data-'0' low. Caught on a scope capture of a real
    # READ_STATUS in state S (CONTEXT.md Finding 84) — the target's low is
    # visibly interrupted by a 132 ns high spike and then resumes:
    #
    #     L0.26  H0.13  L1.06      <- our marker, OUR SPIKE, target's '0'
    #
    # and the low bits of the returned byte flipped run to run. The
    # speed-up exists for back-to-back '0' bits, which only get 3 cycles of
    # high between them; a '1' bit has 13 cycles, and the 10k pull-up's
    # ~300 ns rise is over by cycle 3 of those — long before the target
    # samples. So the '0' path keeps its speed-up and this one does not.
    nop()                          .side(0)     # cyc 3   stay released
    set(pindirs, 0)                .side(0) [5] # cyc 4   release, to cyc 9
    in_(pins, 1)                   .side(0) [4] # cyc 10  SAMPLE, to cyc 14
    jmp(y_dec, "bit")              .side(0)     # cyc 15 -> 16 cycles/bit
    jmp("byte_done")               .side(0)
    label("zero")
    # ---- '0': 13 cycles low, then speed-up high, then Hi-Z ----
    nop()                          .side(0) [6] # cyc 3..9   still low
    in_(pins, 1)                   .side(0) [2] # cyc 10  SAMPLE, to cyc 12
    nop()                          .side(1)     # cyc 13  DRIVE HIGH
    set(pindirs, 0)                .side(0)     # cyc 14  release
    jmp(y_dec, "bit")              .side(0)     # cyc 15
    label("byte_done")
    nop()                          .side(0)
    # falls off the end -> wraps to the top -> blocks on `pull`, with BKGD
    # left released and the output latch back at 0, so the next
    # `set(pindirs, 1)` drives LOW again.


# ---------------------------------------------------------------------------
# *** bdc_rx_bits IS GONE (2026-09-15). ***
#
# It was a second state machine that owned BKGD for the read phase, and
# handing the pin from the TX SM to it was THE bug. Measured back to back
# on real hardware, same command, same bit rate, same session (CONTEXT.md
# Finding 79):
#
#     [0xE4, 0xFF] contiguous, one SM   -> marker bit 2 held low for 13.4
#                                          target cycles by the TARGET,
#                                          reproducible, absent in three
#                                          negative controls
#     [0xE4] then a separate RX SM      -> 46 us of dead silence, 0xFF
#
# Reads are now done by transmitting 0xFF bytes through `bdc_tx_byte`,
# which samples at cycle 10 of every bit. A 0xFF bit and a BDC read-bit
# marker are the same waveform, so the command and its read phase are one
# uninterrupted burst out of one FIFO. See bdc.py `_xfer`.
#
# The dead program's docstring used to claim the read marker's low was too
# short (1 cycle vs the datasheet's "at least two target BDC cycles") and
# that this was why every read in the project's history returned 0xFF.
# That fix was deployed and measured: reads stayed 0xFF at every address
# (Finding 78). The marker width was not the cause; the handover was.
#
# Instruction-budget note: PIO block 1 now holds bdc_sample (5) +
# bdc_sample2 (5) + bdc_tx_byte (16) = 26/32. Re-adding a 7-instruction
# read program would put it at 33 and it would fail to load.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# bdc_sync: perform the SYNC handshake and measure the target's response
# pulse width. 27 instructions — lives alone on PIO block 0.
#
# FIFO words consumed, in order:
#   1: host_low_cycles  — how long to hold BKGD low to request sync.
#                         Loop is 1 PIO cycle/iteration.
#   2: wait_budget      — shared timeout for "wait for line high" AND
#                         "wait for falling edge". Both loops are 2 PIO
#                         cycles/iteration.
#   3: measure_max      — max iterations of the pulse-width measuring
#                         loop, which is also 2 PIO cycles/iteration.
#
# Pushes exactly ONE 32-bit result word, which the host decodes as:
#   0xFFFFFFFF -> BKGD went high, but no falling edge arrived inside
#                 wait_budget. Target present-but-silent, or not powered,
#                 or BKGD not actually connected. NOT a range problem —
#                 retrying with a slower probe clock will not help.
#   0xFFFFFFFE -> BKGD never returned high after we released it. Line
#                 shorted low, target driving it, or no pull-up.
#   0          -> measuring counter ran out before the line rose: the
#                 pulse is LONGER than measure_max iterations. This is an
#                 overflow, not a failure — retry with a SLOWER probe
#                 clock to cover a longer real-time window.
#   anything else -> `remaining`, the leftover counter value. The host
#                 computes:
#                     elapsed_pio_cycles = 2 * (measure_max - remaining)
#                 (the "* 2" because the measuring loop is two PIO
#                 instructions per decrement) and from that:
#                     target_bdc_hz = 128 * probe_freq_hz
#                                     / elapsed_pio_cycles
#                 since the target's response pulse is exactly 128 of its
#                 own BDC clock cycles wide.
#
# Bugs this replaced (all found in the 2026-09-11 validation pass):
#   * the old version pushed the *remaining* count but bdc.py used it as
#     if it were the elapsed count — the relationship was inverted;
#   * the old counter was `set(x, 31)`, a 5-bit immediate, so it could
#     measure at most ~93 PIO cycles — useless for a real target. The
#     counter is now loaded from the FIFO and is a full 32 bits;
#   * counter wrap and a genuine timeout pushed values the host could not
#     tell apart, so a slow-but-working target was reported as a wiring
#     fault. They are now distinct result codes;
#   * there was no "wait for BKGD to actually be high" step before
#     hunting for the target's falling edge, so the RP2040 input
#     synchroniser delay plus the pull-up's RC rise time meant the tail of
#     our OWN drive-low could be read as the target's response
#     (HCS08RMv1 §7.3.4.1 specifies the target waits for BKGD high first).
# ---------------------------------------------------------------------------
@asm_pio(
    set_init=PIO.IN_LOW,
    in_shiftdir=PIO.SHIFT_LEFT,
    autopull=False,
    autopush=False,
)
def bdc_sync():
    pull(block)
    mov(x, osr)                   # host_low_cycles
    set(pindirs, 1)               # drive BKGD low for the sync request
    label("synclow")
    jmp(x_dec, "synclow")         # 1 PIO cycle per iteration

    # --- speedup pulse (HCS08RMv1 §7.3.4.1) ---------------------------
    # Briefly drive BKGD HIGH before releasing to Hi-Z so the rising edge
    # is fast, instead of waiting on the pull-up's RC. The output latch is
    # then restored to 0 so the next `set(pindirs, 1)` drives LOW again.
    # Duration scales with the probe clock (2 PIO cycles), which keeps it
    # a small fraction of a target BDC cycle at every probe frequency we
    # use.
    set(pins, 1)             [1]
    set(pindirs, 0)               # release to Hi-Z (input)
    set(pins, 0)                  # restore output latch low (no pin effect)

    pull(block)
    mov(y, osr)                   # wait_budget, shared by both wait loops

    # Wait for BKGD to actually be high before looking for the target's
    # falling edge. Bounded by the same budget so a line stuck low can
    # never hang the state machine (and therefore never hang the Pico).
    label("wait_high")
    jmp(pin, "wait_fall")         # pin high -> proceed
    jmp(y_dec, "wait_high")
    jmp("stuck_low")

    # Hunt for the start of the target's response pulse.
    label("wait_fall")
    jmp(pin, "still_high")
    jmp("got_fall")
    label("still_high")
    jmp(y_dec, "wait_fall")
    jmp("timeout")

    # Measure the low pulse: count DOWN from measure_max until the line
    # rises. 2 PIO cycles per decrement (the two jmps below).
    label("got_fall")
    pull(block)
    mov(x, osr)                   # measure_max
    label("measure")
    jmp(pin, "emit")              # line rose -> X holds `remaining`
    jmp(x_dec, "measure")
    jmp("emit")                   # fell through with X == 0 -> overflow

    label("timeout")
    mov(x, invert(null))          # X = 0xFFFFFFFF
    jmp("emit")

    label("stuck_low")
    mov(x, invert(null))
    jmp(x_dec, "emit")            # X = 0xFFFFFFFE (x_dec always decrements)

    label("emit")
    in_(x, 32)
    push(block)
    # falls off the end -> wraps to the top -> blocks on the first pull,
    # so the SM is immediately re-armed for the next attempt.


# ---------------------------------------------------------------------------
# bdc_sample: passive "logic analyzer" capture, used for self-diagnosis when
# you don't have a separate logic analyzer. Arms itself, then WAITS for the
# line to first go low (edge trigger) before sampling -- this means the
# caller doesn't need to precisely time-align starting this SM with the
# actual write/read/sync operation that drives the pin; it can be started
# any moment before, and it'll only start recording once the real signal
# begins. This SM only ever reads the pin (in_base) -- it never sets
# pindirs -- so it can safely coexist with the driving SM on the same GPIO.
#
#   FIFO word 1: sample_count (must be a multiple of 32; samples are packed
#                MSB-first, 32 per word, into the RX FIFO via autopush)
#
# Each sample takes CYCLES_PER_SAMPLE PIO cycles (see the constant below) --
# resolution is controlled by choosing the SM clock frequency, not by
# changing this program.
#
# *** Do NOT re-add fifo_join=PIO.JOIN_RX here. ***
# JOIN_RX gives up the TX FIFO to double the RX FIFO depth (4 -> 8 words).
# But this program takes sample_count through the TX FIFO, and bdc.py's
# _capture() calls sm.put() on it — with no TX FIFO that put() spins
# forever inside MicroPython's blocking C code, before the operation being
# captured is ever started. THAT was the real cause of the "Scope capture
# hangs the Pico" bug tracked in CONTEXT.md (it was not, as previously
# hypothesised, a GPIO FUNCSEL / PIO-block conflict — RP2040 PIO *inputs*
# are not gated by GPIO function select at all, so two SMs reading the
# same pin was never the problem).
#
# If the shallower 4-word RX FIFO ever becomes the bottleneck, the fix is
# to load the count via sm.exec() into Y rather than to re-join the FIFOs.
# 5 instructions.
# ---------------------------------------------------------------------------
CYCLES_PER_SAMPLE = 3  # must match the instruction sequence below


@asm_pio(
    in_shiftdir=PIO.SHIFT_LEFT,
    autopush=True,
    push_thresh=32,
)
def bdc_sample():
    pull(block)
    mov(y, osr)                 # sample_count -> Y

    wait(0, pin, 0)              # ARM: block here until the line first goes
                                   # low -- this is the capture trigger.
    label("loop")
    in_(pins, 1)              [1]  # sample 1 bit; [1] delay -> 2 cycles here
    jmp(y_dec, "loop")             # + 1 cycle for the jmp = 3 cycles/sample


# ---------------------------------------------------------------------------
# bdc_sample2: TWO-pin passive capture — in_base and in_base+1 sampled
# together, 2 bits per sample, 16 samples per 32-bit word. Trigger is
# in_base+1 (i.e. BKGD, when in_base is RESET) going low.
#
# This exists because the single most load-bearing open question in this
# project is whether the ~8 us low pulses on BKGD are a BDC SYNC response
# (128 BDC cycles at 16.3 MHz) or the target's own internal reset drive
# (34 bus cycles at 4.19 MHz) — the two are the same width, which is why
# every one-pin capture so far has been ambiguous. Watching RESET at the
# same time separates them: a reset drives BOTH pins, a BDC response only
# drives BKGD.
#
# CONTEXT.md's "next steps" listed this as a five-line change; it is.
# 5 instructions.
# ---------------------------------------------------------------------------
# The trigger used to be `wait(0, pin, 1)`, which hard-codes "in_base+1"
# (BKGD) as the trigger and cannot be changed without reassembling. The
# jmp_pin-based arm loop below is the SAME instruction count but takes its
# trigger from the StateMachine's `jmp_pin`, which is a runtime argument —
# so the same program can trigger on BKGD or on RESET. Being able to trigger
# on RESET is what lets the target's own reset train be observed with the
# reset edge as the time origin instead of our own BKGD drive.
@asm_pio(
    in_shiftdir=PIO.SHIFT_LEFT,
    autopush=True,
    push_thresh=32,
)
def bdc_sample2():
    pull(block)
    mov(y, osr)                 # sample_count -> Y
    label("arm")
    jmp(pin, "arm")             # ARM: spin until jmp_pin reads LOW
    label("loop")
    in_(pins, 2)              [1]  # 2 bits/sample, 3 cycles per sample
    jmp(y_dec, "loop")


SM_CAPTURE2 = 7


def make_capture2_state_machine(base_pin_num, freq, trig_pin_num=None,
                                sm_id=SM_CAPTURE2):
    """Two-pin read-only capture on base_pin_num and base_pin_num+1.

    `trig_pin_num` selects the falling-edge trigger (default: base+1, i.e.
    BKGD when base is RESET).

    NOTE: the Pin objects are constructed WITHOUT a mode argument on
    purpose. `Pin(n, Pin.IN)` re-initialises the pad and so silently
    cancels a `drive_pin()` hold — which made every "hold RESET low and
    capture" experiment measure nothing at all (CONTEXT.md Finding 24).
    RP2040 PIO *inputs* are not gated by GPIO function select, so no pad
    reconfiguration is needed to read a pin from PIO.
    """
    p = Pin(base_pin_num)
    Pin(base_pin_num + 1)
    apply_pad(base_pin_num)
    apply_pad(base_pin_num + 1)
    trig = Pin(base_pin_num + 1 if trig_pin_num is None else trig_pin_num)
    return StateMachine(sm_id, bdc_sample2, freq=freq, in_base=p, jmp_pin=trig)


def make_state_machine(prog, pin_num, freq, sm_id):
    """
    Helper to instantiate a StateMachine running `prog` on GPIO `pin_num`
    at state-machine clock `freq` Hz, with that pin used as both the
    set-pin (drive) and the jmp-pin / in-pin (sense) so the single wire
    can be driven and read by the same program.

    `sm_id` is REQUIRED (no default) and selects which of the 8 hardware
    state machines runs the program: 0-3 = PIO block 0, 4-7 = PIO block 1.
    Use the SM_* constants at the top of this module — the assignment is
    deliberate and load-bearing, because each block only has 32
    instruction slots. See the block-allocation table in the module
    docstring.
    """
    p = Pin(pin_num, Pin.IN)
    apply_pad(pin_num)
    # sideset_base is the same physical pin: bdc_tx_byte uses side-set to
    # carry the output VALUE (for the speed-up pulse) while `set` carries
    # the direction. Programs without a side-set field simply ignore it.
    sm = StateMachine(
        sm_id,
        prog,
        freq=freq,
        set_base=p,
        in_base=p,
        jmp_pin=p,
        sideset_base=p,
    )
    return sm


def make_capture_state_machine(pin_num, freq, sm_id=SM_CAPTURE):
    """
    Like make_state_machine, but for bdc_sample specifically: only binds
    in_base (read-only) so it's safe to run at the same time as a driving
    SM on the same pin. Defaults to SM_CAPTURE (state machine 4, PIO block
    1) — it shares that block with the bit-level driving programs, which
    is fine: they are different state machines, and the three programs
    together fit inside the block's 32 instruction slots.
    """
    p = Pin(pin_num, Pin.IN)
    apply_pad(pin_num)
    sm = StateMachine(
        sm_id,
        bdc_sample,
        freq=freq,
        in_base=p,
    )
    return sm
