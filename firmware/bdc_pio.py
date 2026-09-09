"""
bdc_pio.py — PIO state machine programs implementing the Freescale/NXP S08
Background Debug Controller (BDC) single-wire protocol on the BKGD pin.

Runs on the Raspberry Pi Pico (RP2040) under MicroPython.

------------------------------------------------------------------------
PROTOCOL SUMMARY (see README.md warning — verify against a logic analyzer
before trusting this on real silicon; these are the standard published
S08/RS08 BDC timing figures, not something Anthropic/Claude can verify
against your specific part without hardware in hand):

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
  * SYNC: host drives BKGD low for >=128 target cycles (use a duration
    long enough to cover even a very slow target clock, e.g. a few ms),
    then releases. Target responds with a low pulse ~128 target cycles
    wide. Host measures that pulse to compute the target's BDC clock
    period, and uses it to set the SM clock divider for everything else.
------------------------------------------------------------------------

DESIGN: rather than hard-coding cycle counts into the compiled PIO
program (which would require recompiling per target clock speed), the
low-hold duration and total bit-period duration are loaded into the X/Y
scratch registers at runtime from the TX FIFO. This lets one compiled
program work at any target clock speed — you just push different
counts after SYNC calibration.
"""

import rp2
from rp2 import PIO, StateMachine, asm_pio
from machine import Pin
import time


# ---------------------------------------------------------------------------
# bdc_write_bit: send ONE bit (0 or 1) whose "low duration" and "total
# duration" (in PIO cycles) are supplied via the TX FIFO before each bit.
#   FIFO word 1: low_cycles   (how long to hold BKGD low)
#   FIFO word 2: total_cycles (total bit-time length; must be > low_cycles)
# ---------------------------------------------------------------------------
@asm_pio(set_init=PIO.IN_LOW, out_init=PIO.IN_LOW, autopull=False, autopush=False)
def bdc_write_bit():
    pull(block)             # low_cycles -> OSR
    mov(x, osr)
    pull(block)             # total_cycles -> OSR
    mov(y, osr)

    set(pindirs, 1)          # drive BKGD low (start of bit time)
    label("hold_low")
    jmp(x_dec, "hold_low")   # burn `low_cycles` PIO cycles while low
    # NOTE: x_dec decrements then jumps if pre-decrement value != 0, so this
    # loop runs `low_cycles` times (off-by-one accounted for by caller).

    set(pindirs, 0)          # release -> pulled high externally
    label("hold_total")
    jmp(y_dec, "hold_total")  # burn remaining PIO cycles to fill total_cycles
    # caller is responsible for making (total_cycles - low_cycles) match the
    # release-phase loop length; see bdc.py for the exact accounting.


# ---------------------------------------------------------------------------
# bdc_read_bit: read ONE bit from the target.
#   FIFO word 1: start_low_cycles (host's short low pulse to start the bit)
#   FIFO word 2: sample_delay_cycles (how long after release to sample)
#   FIFO word 3: total_cycles (remaining time to pad out the bit window)
# Pushes the sampled pin level (0 or 1) to the RX FIFO.
# ---------------------------------------------------------------------------
@asm_pio(set_init=PIO.IN_LOW, out_init=PIO.IN_LOW, autopull=False, autopush=False)
def bdc_read_bit():
    pull(block)
    mov(x, osr)               # start_low_cycles
    pull(block)
    mov(y, osr)               # sample_delay_cycles

    set(pindirs, 1)            # drive low briefly to start the bit
    label("startlow")
    jmp(x_dec, "startlow")

    set(pindirs, 0)            # release; target now drives (or doesn't)
    label("waitsample")
    jmp(y_dec, "waitsample")   # wait until the sample point

    set(pindirs, 0)            # ensure input mode
    in_(pins, 1)                # sample BKGD level into ISR
    push(block)                  # send sampled bit to host

    pull(block)                # total_cycles (pad remainder of bit window)
    mov(x, osr)
    label("padtotal")
    jmp(x_dec, "padtotal")


# ---------------------------------------------------------------------------
# bdc_sync: perform the SYNC handshake and measure the target's response
# pulse width in PIO clock cycles. Push the measured width to RX FIFO
# (0 == no response / timeout, caller should retry with a longer low pulse
# or check wiring).
#   FIFO word 1: host_low_cycles (how long to hold BKGD low to request sync)
#   FIFO word 2: timeout_cycles (give up waiting for target's edges after this)
# ---------------------------------------------------------------------------
@asm_pio(set_init=PIO.IN_LOW, out_init=PIO.IN_LOW, autopull=False, autopush=False)
def bdc_sync():
    pull(block)
    mov(x, osr)                # host_low_cycles
    pull(block)
    mov(y, osr)                # timeout_cycles (reused per wait phase)

    set(pindirs, 1)             # drive BKGD low for the sync request
    label("synclow")
    jmp(x_dec, "synclow")

    set(pindirs, 0)             # release and let target respond

    # Wait for target to drive the line low (start of its response pulse),
    # counting elapsed cycles in Y as a timeout guard.
    label("wait_fall")
    jmp(pin, "still_high")       # PIO 'jmp pin' tests the mapped jmp-pin
    jmp("got_fall")
    label("still_high")
    jmp(y_dec, "wait_fall")
    jmp("timeout")

    label("got_fall")
    # Now measure the low pulse width by counting cycles until it rises
    # again, using X as the measuring counter (counts UP is not native to
    # PIO, so we count DOWN from a large value and report cycles_used =
    # start_value - remaining on the host side).
    set(x, 31)                    # low 5 bits of a larger software-extended
                                    # counter; see bdc.py for how this is
                                    # combined with a wrap-count in practice
    label("measure")
    jmp(pin, "rose")
    jmp(x_dec, "measure_cont")
    jmp("measure_wrap")
    label("measure_cont")
    jmp("measure")
    label("measure_wrap")
    # signal a wrap via a sentinel push, host-side loop re-issues with a
    # coarser measuring clock divider if this happens (see bdc.py SYNC_
    # calibration loop, which retries at progressively slower SM clocks)
    in_(x, 32)
    push(block)
    jmp("done")

    label("rose")
    in_(x, 32)
    push(block)
    jmp("done")

    label("timeout")
    set(x, 0)
    in_(x, 32)
    push(block)

    label("done")
    nop()


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
# ---------------------------------------------------------------------------
CYCLES_PER_SAMPLE = 3  # must match the instruction sequence below


@asm_pio(
    set_init=PIO.IN_LOW,
    in_shiftdir=PIO.SHIFT_LEFT,
    autopush=True,
    push_thresh=32,
    fifo_join=PIO.JOIN_RX,
)
def bdc_sample():
    pull(block)
    mov(y, osr)                 # sample_count -> Y

    wait(0, pin, 0)              # ARM: block here until the line first goes
                                   # low -- this is the capture trigger.
    label("loop")
    in_(pins, 1)              [1]  # sample 1 bit; [1] delay -> 2 cycles here
    jmp(y_dec, "loop")             # + 1 cycle for the jmp = 3 cycles/sample


def make_state_machine(prog, pin_num, freq, sm_id=0):
    """
    Helper to instantiate a StateMachine running `prog` on GPIO `pin_num`
    at state-machine clock `freq` Hz, with that pin used as both the
    set-pin (drive) and the jmp-pin / in-pin (sense) so the single wire
    can be driven and read by the same program.

    `sm_id` selects which of the 8 hardware state machines (0-3 = PIO
    block 0, 4-7 = PIO block 1) to use. Programs that need to run
    CONCURRENTLY (e.g. a driving program alongside the passive capture
    program below) must use different PIO blocks, since each block's
    32-word instruction memory is shared by its 4 state machines.
    """
    p = Pin(pin_num, Pin.IN)
    sm = StateMachine(
        sm_id,
        prog,
        freq=freq,
        set_base=p,
        in_base=p,
        jmp_pin=p,
    )
    return sm


def make_capture_state_machine(pin_num, freq, sm_id=4):
    """
    Like make_state_machine, but for bdc_sample specifically: only binds
    in_base (read-only) so it's safe to run at the same time as a driving
    SM on the same pin. Defaults to sm_id=4 (first SM of PIO block 1) so
    it never shares instruction memory with the driving programs on block
    0 (sm_id 0-3).
    """
    p = Pin(pin_num, Pin.IN)
    sm = StateMachine(
        sm_id,
        bdc_sample,
        freq=freq,
        in_base=p,
    )
    return sm
