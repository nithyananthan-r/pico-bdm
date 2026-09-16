"""
bdc.py — higher-level Background Debug Controller driver for S08/RS08
targets, built on the PIO primitives in bdc_pio.py.

Runs on the Pico under MicroPython.

*** OPCODE STATUS (updated 2026-09-11) ***
The BDC command opcode BYTE VALUES below were independently verified
against HCS08RMv1 Table 7-1 (SYNC excluded — it has no opcode, it's a
special pulse). They are considered correct.

That verification covers the *values only*. It says nothing about whether
the protocol logic that uses them is right — the same review pass that
confirmed the opcodes also found several real bugs in code built on top
of correctly-valued opcodes (inverted SYNC measurement, wrong FSTAT
FBLANK mask, FACCERR never cleared, page-erase clobbering an
already-programmed chunk).

*** STATE OF PLAY (2026-09-13, third live session, measured on real
hardware after the wiring was corrected) ***

The second session's conclusion — that the target was suffering a VDD
brownout and the ~8 us pulse was a power dip — was CORRECT for the wiring
as it then was (C1 had been fitted across the RESET net instead of
VDD-VSS). That is fixed. Everything in the old version of this docstring
about brownouts is withdrawn; see CONTEXT.md "THIRD live session".

What is now established by measurement:

  * **The target's BDC works and answers SYNC on demand.** Phase-locked to
    a reset we drive ourselves, it returns a clean 128-cycle pulse 8/8
    times: 7.52-7.60 us = **16.84-17.0 MHz**, which is the MC9S08SG8's
    FEI-default DCO (BDCSCR.CLKSW resets to 0 = alternate BDC clock).
    Use `sync_locked()`, not `sync()` — see its docstring.
  * The part free-runs in a blank-FLASH illegal-opcode reset loop with a
    period of exactly 40.0 us, ~8.6 us of it with RESET asserted. Every
    transaction has to fit in the ~30 us gap, and a SYNC or command fired
    at a random phase mostly misses.
  * **No BDC command has ever executed, and the host side is now
    exhausted as an explanation.** All 256 opcode values (which covers a
    wrong opcode, wrong bit order and wrong polarity), 14 bit clocks from
    8 to 24 MHz, and every phase of the reset loop at 1 us resolution have
    been swept with a waveform verified correct to the nanosecond. The
    read phase never shows the target driving the line, ever.

Consequences for anyone reading values out of this driver:

  * every byte `read_byte`/`read_status` returns is still meaningless. A
    read with no answer comes back 0xFF, because 0xFF is what the pull-up
    plus our own read-bit markers look like.
  * **that also defeats `flash_init_clock`'s DIVLD sanity check** — it
    tests bit 7 of a read-back FCDIV, and an unanswered read has bit 7
    set. It currently returns success against this silent target. Do not
    trust it until a command is known to execute.
  * no write of any kind lands, BDCSCR included, so nothing above the bit
    layer here (memory access, FLASH, breakpoints) has been exercised.
"""

import time
from machine import Pin, mem32
from bdc_pio import (
    bdc_tx_byte,
    bdc_sync,
    make_state_machine,
    make_capture_state_machine,
    make_capture2_state_machine,
    make_raw_state_machine,
    apply_pad,
    read_pad,
    set_pad_policy,
    CYCLES_PER_SAMPLE,
    SM_SYNC,
    SET_PINDIRS_IN,
    SM_RAW,
    SM_TX,
)

# ---------------------------------------------------------------------------
# BDC command opcodes (S08) — values verified against HCS08RMv1 Table 7-1.
# ---------------------------------------------------------------------------
CMD_ACK_ENABLE     = 0xD5
CMD_ACK_DISABLE    = 0xD6
CMD_BACKGROUND     = 0x90
CMD_READ_STATUS    = 0xE4
CMD_WRITE_CONTROL  = 0xC4
CMD_READ_BYTE      = 0xE0
CMD_READ_BYTE_WS   = 0xE1
CMD_READ_LAST      = 0xE8
CMD_WRITE_BYTE     = 0xC0
CMD_WRITE_BYTE_WS  = 0xC1
CMD_READ_BKPT      = 0xE2
CMD_WRITE_BKPT     = 0xC2
CMD_GO             = 0x08
CMD_TRACE1         = 0x10
CMD_TAGGO          = 0x18
CMD_READ_A         = 0x68
CMD_READ_CCR       = 0x69
CMD_READ_PC        = 0x6B
CMD_READ_HX        = 0x6C
CMD_READ_SP        = 0x6F
CMD_WRITE_A        = 0x48
CMD_WRITE_CCR      = 0x49
CMD_WRITE_PC       = 0x4B
CMD_WRITE_HX       = 0x4C
CMD_WRITE_SP       = 0x4F

# ---------------------------------------------------------------------------
# BDCSCR — the BDC status/control register, read by CMD_READ_STATUS and
# written by CMD_WRITE_CONTROL. Layout per HCS08RMv1 Figure 7-5:
#
#   bit7 ENBDM   enable BDM (must stay 1 while we're using BDM)
#   bit6 BDMACT  BDM active (status, set by hardware)
#   bit5 BKPTEN  BDC breakpoint ENABLE — the BKPT register does NOTHING
#                until this is 1, and BDCSCR resets with it 0
#   bit4 FTS     force/tag select: 0 = force (halt when the address is
#                reached), 1 = tag (halt when the tagged opcode executes)
#   bit3 CLKSW   BDC clock source select — must stay 1 for the bit timing
#                this driver calibrates against
#   bit2 WS      wait/stop status (sticky)
#   bit1 WSF     wait/stop failure (sticky)
#   bit0 DVF     data valid failure (sticky)
# ---------------------------------------------------------------------------
SCR_ENBDM  = 0x80
SCR_BDMACT = 0x40
SCR_BKPTEN = 0x20
SCR_FTS    = 0x10
SCR_CLKSW  = 0x08
SCR_WS     = 0x04
SCR_WSF    = 0x02
SCR_DVF    = 0x01

# Bits of BDCSCR that are host-writable control bits (everything else is
# hardware status we must not try to force).
SCR_WRITABLE_MASK = SCR_ENBDM | SCR_BKPTEN | SCR_FTS | SCR_CLKSW  # 0xB8

# ---------------------------------------------------------------------------
# FLASH module registers (confirmed against the S9S08SG8/SG4 datasheet
# high-page register map).
# ---------------------------------------------------------------------------
REG_FCDIV = 0x1820
REG_FOPT  = 0x1821
REG_FCNFG = 0x1823
REG_FPROT = 0x1824
REG_FSTAT = 0x1825
REG_FCMD  = 0x1826

# FSTAT bit layout, MC9S08SG8 datasheet Rev.8 Figure 4-9 / Table 4-12:
#   FCBEF[7] FCCF[6] FPVIOL[5] FACCERR[4] 0[3] FBLANK[2] 0[1] 0[0]
# (FBLANK was previously coded as 0x02 here — wrong bit, fixed 2026-09-11.)
FSTAT_FCBEF   = 0x80
FSTAT_FCCF    = 0x40
FSTAT_FPVIOL  = 0x20
FSTAT_FACCERR = 0x10
FSTAT_FBLANK  = 0x04

# Write-1-to-clear error flags in FSTAT. Per datasheet §4.5.5, FACCERR
# "must be cleared by writing a 1 to FACCERR in FSTAT before any command
# can be processed" — and FCDIV cannot be (re)written at all while it is
# set. Leaving it set permanently wedges the FLASH interface.
FSTAT_ERR_CLEAR = FSTAT_FACCERR | FSTAT_FPVIOL

FCMD_BLANK_CHECK  = 0x05
FCMD_BYTE_PROGRAM = 0x20
FCMD_BURST_PROGRAM = 0x25
FCMD_PAGE_ERASE   = 0x40
FCMD_MASS_ERASE   = 0x41

PAGE_SIZE = 512  # bytes, per SG8/SG4 FLASH organization

# ---------------------------------------------------------------------------
# Chip security (NVOPT / FOPT), and the hazard that goes with it.
#
# NVOPT lives at $FFBF -- inside the LAST FLASH PAGE, $FE00-$FFFF, the same
# page as the reset vector. Erasing that page blanks NVOPT to 0xFF, i.e.
# SEC01:SEC00 = 1:1 = SECURED, and the part latches security from NVOPT at
# every reset. So an erase of page $FE00 that is not followed by
# reprogramming $FFBF *in the same power cycle* locks the chip at the next
# power-up. Measured, and recovered from, in the TENTH session
# (CONTEXT.md Finding 95).
#
# FOPT ($1821) is the RUNTIME copy: loaded from NVOPT at reset, but updated
# in place by the unsecure logic, which makes it the witness for "did the
# unsecure actually take" within a power cycle (Finding 96).
#
# SEC01:SEC00 = 1:0 is the ONLY unsecured encoding; 0:0, 0:1 and 1:1 all
# mean secured.
#
# NVOPT_SECURED is 0xFC and not 0xFF on purpose: FLASH programming can only
# drive bits 1 -> 0, so from the unsecured 0xFE the only security state
# reachable WITHOUT erasing page $FE00 (and therefore without opening the
# Finding 95 window) is SEC = 0:0. Same lock, no hazard.
# ---------------------------------------------------------------------------
ADDR_NVOPT = 0xFFBF
NVOPT_PAGE = 0xFE00
NVOPT_UNSECURED = 0xFE       # KEYEN=1 FNORED=1 SEC=1:0
NVOPT_SECURED = 0xFC         # KEYEN=1 FNORED=1 SEC=0:0
SEC_MASK = 0x03
SEC_UNSECURED = 0x02

# System / identity registers used by chip_info() and the live panel.
REG_SRS    = 0x1800
REG_SOPT1  = 0x1802
REG_SDIDH  = 0x1806
REG_SDIDL  = 0x1807
REG_SPMSC1 = 0x1809

# Direct-page port registers (MC9S08SG8 register map). PTA/PTB are bonded
# out on this package; PTC is read anyway and reported raw, flagged as
# possibly-absent rather than silently presented as real pin state.
REG_PTAD  = 0x0000
REG_PTADD = 0x0001
REG_PTBD  = 0x0002
REG_PTBDD = 0x0003
REG_PTCD  = 0x0004
REG_PTCDD = 0x0005

# fFCLK must land in 150-200 kHz (datasheet §4.7.1). Aim for the middle of
# that window rather than its exact upper edge, so rounding and a slightly
# off nominal bus clock can't push us out of spec.
FCLK_TARGET_HZ = 175_000
FCLK_MIN_HZ = 150_000
FCLK_MAX_HZ = 200_000

# FPROT value that fully unprotects the FLASH array. Bit0 (FPDIS) = 1
# disables protection; the FPS field above it then doesn't matter.
# CONFIDENCE: MEDIUM — this is the conventional S08 "unprotect everything"
# value (it's what USBDM writes), but the exact FPROT bit names for this
# part were NOT re-read from the datasheet during the 2026-09-11 pass.
# Verify against MC9S08SG8 §4.5.6 before trusting it on a protected part.
FPROT_UNPROTECT_ALL = 0xFF

# ---------------------------------------------------------------------------
# Bit-timing. The tx/rx PIO programs are written so that ONE PIO CYCLE IS
# ONE TARGET BDC CYCLE. Every edge the BDC protocol specifies (low for 1 or
# 13 cycles, sample at 10, bit time 16) already falls on a whole target
# cycle, so nothing is lost by quantising to them — and in exchange those
# counts fit in PIO's 5-bit `[delay]` field, which is what lets a whole byte
# be clocked out from a single FIFO word with no Python in the middle.
#
# There used to be a PIO_CYCLES_PER_TARGET_CYCLE oversampling factor here.
# It bought sub-cycle edge placement the protocol never asks for, and it
# forced the bit-at-a-time PIO programs whose per-bit StateMachine
# construction cost 332 us for a 1 us bit on real hardware — see CONTEXT.md
# "Finding 6".
# ---------------------------------------------------------------------------
SM_MAX_FREQ_HZ = 120_000_000         # RP2040 tops out ~133 MHz; leave margin
SM_MIN_FREQ_HZ = 2_000               # sys_clk/65536 (16.8 fixed-point clkdiv)
BDC_CYCLES_PER_BIT = 16

# ---------------------------------------------------------------------------
# SYNC probe ladder.
#
# Each entry is (probe_clock_hz, window_seconds, host_low_seconds). The PIO
# program measures the target's 128-cycle response pulse by counting down a
# 32-bit counter at `probe_clock_hz`, two PIO cycles per decrement;
# `window_seconds` sets both that counter's start value and the timeout
# budget for waiting on the target's edges. `host_low_seconds` is how long
# the host holds BKGD low to request the SYNC.
#
# host_low_seconds used to be derived from `probe_freq * window_s`, i.e. the
# same product as the timeout budget, which made the first rung hold BKGD
# low for a full **10 ms** and the last rung for a full **1 second**. That
# is internally consistent (>= 128 cycles of the slowest target that rung
# can measure) but absurd in practice: real S08 BDC clocks are 1-20 MHz, so
# 128 cycles is 6-128 us. It also made every sync() cost >= 10 ms and made
# `capture_sync` useless as a diagnostic, because a 256-sample capture only
# ever saw the first few us of a 10 ms flat low. Measured on hardware, the
# length made no difference to the target's response (CONTEXT.md Finding 5
# correction), so these are now sized generously but sanely:
#   200 us covers any target down to 640 kHz, 2 ms down to 64 kHz,
#   20 ms down to 6.4 kHz.
#
# The two knobs trade off against each other:
#   * a HIGHER probe clock measures the pulse more finely (better
#     resolution on the derived target frequency);
#   * a LONGER window tolerates a SLOWER target before the counter runs
#     out.
# So the ladder walks from "fine resolution, short window" toward "coarse
# resolution, long window". We only step down when the PIO actually
# reports an overflow (or no response), which is why it's useful for the
# firmware to distinguish those two cases at all — the previous version
# pushed an indistinguishable value for both and just retried identically.
#
# Slowest target covered by each rung (a 128-cycle pulse must fit in the
# window): 10 ms -> ~12.8 kHz BDC, 100 ms -> ~1.3 kHz, 1 s -> ~128 Hz.
# ---------------------------------------------------------------------------
SYNC_ATTEMPTS = (
    (25_000_000, 0.010, 0.000_200),
    (5_000_000, 0.100, 0.002),
    (1_000_000, 1.000, 0.020),
)

# Result codes pushed by bdc_pio.bdc_sync (see its docstring).
SYNC_RESULT_NO_FALL = 0xFFFFFFFF
SYNC_RESULT_STUCK_LOW = 0xFFFFFFFE
SYNC_RESULT_OVERFLOW = 0

# Plausibility band for a derived BDC clock. Anything outside this is
# treated as a bad measurement rather than reported as fact.
TARGET_FREQ_MIN_HZ = 100
TARGET_FREQ_MAX_HZ = 30_000_000


# ---------------------------------------------------------------------------
# Direct PIO CTRL access, for the ONE place where MicroPython's call overhead
# is the thing being measured.
#
# `StateMachine.active(1)` costs ~10 us and the `ticks_us` busy-wait wrapped
# around it another ~40 us — measured, not guessed. That is 5-50x a whole BDC
# bit at 16.9 MHz, and it put the read phase of a command ~55 us after the
# command byte ended, i.e. one and a half of the target's 40 us reset cycles
# later. Writing the SM_ENABLE bit straight into PIO1's CTRL register through
# the RP2040's atomic-SET alias is a single store.
#
# RP2040 datasheet 3.7: PIO1_BASE = 0x50300000, CTRL at offset 0x000
# (bits [3:0] = SM_ENABLE), and every peripheral aliases +0x2000 = atomic
# bit-set, +0x3000 = atomic bit-clear. State machines 4-7 in this file's
# numbering are PIO1's own 0-3.
# ---------------------------------------------------------------------------
_PIO1_BASE = 0x5030_0000
PIO1_CTRL_SET = _PIO1_BASE + 0x2000
PIO1_CTRL_CLR = _PIO1_BASE + 0x3000
SM_TX_MASK = 1 << (SM_TX - 4)

# Same trick for PIO block 0, where the raw waveform player lives.
_PIO0_BASE = 0x5020_0000
PIO0_CTRL_SET = _PIO0_BASE + 0x2000
PIO0_CTRL_CLR = _PIO0_BASE + 0x3000
SM_RAW_MASK = 1 << SM_RAW


class BdcError(Exception):
    pass


class Bdc:
    def __init__(self, bkgd_pin, reset_pin, power_pin=None):
        self.bkgd_pin_num = bkgd_pin
        self.reset_pin_num = reset_pin
        # RESET is open-drain with an internal pull-up and the target can
        # legitimately drive it low during its own reset sequence, so never
        # construct it as an output: we only ever drive it LOW (see
        # _reset_assert) and otherwise leave it floating as an input.
        self.reset = Pin(reset_pin, Pin.IN)
        # power_pin, if given, actively drives target VDD (push-pull) so we
        # can perform a genuine power-on reset in software. Per the
        # MC9S08SG8 datasheet Sec 17.1.1: "Other causes of reset including
        # an external pin reset or an internally generated error reset
        # ignore the state of the BKGD pin" -- ONLY a power-on reset lets
        # holding BKGD low force active background mode on this part. The
        # RESET-pin method (hardware_reset_to_bdm below) can never work on
        # this chip; this is not a fallback, it's the only documented path.
        self.power_pin_num = power_pin
        self.power = Pin(power_pin, Pin.OUT, value=1) if power_pin is not None else None
        self.target_freq_hz = None
        self.sm_freq = None
        # Set by sync_locked(): the reset-loop phase at which this target
        # actually answers. Anything phase-sensitive should reuse it.
        self.best_delay_us = None
        # Cached tx/rx state machines, built once per sync() instead of once
        # per BIT (which is what used to cost 332 us per 1 us bit).
        self._tx = None
        self._rx = None
        # GPIO function-select on the BKGD pin is a single global setting: it
        # points at SIO, PIO block 0, or PIO block 1, and only the selected
        # one can drive the pin. Anything that constructs a plain Pin() or a
        # state machine on the other block steals it, so the cached tx/rx SMs
        # (PIO block 1) have to be rebound afterwards. This flag tracks that.
        self._pio_bound = False
        # CONTEXT.md Finding 69: BKGD is the one pin that never goes through
        # a Pin() constructor here, so it kept the RP2040's power-on default
        # pad setting -- PUE=0/PDE=1, an internal ~60k PULL-DOWN fighting the
        # target's own on-chip pull-up. Force a sane pad now and after every
        # rebind (make_*_state_machine does it too).
        apply_pad(bkgd_pin)
        apply_pad(reset_pin)

    def pad(self, pin_num):
        return read_pad(int(pin_num))

    def pad_policy(self, pue=1, pde=0, drive=3):
        pol = set_pad_policy(pue, pde, drive)
        apply_pad(self.bkgd_pin_num)
        apply_pad(self.reset_pin_num)
        self._pio_bound = False
        return {"pue": pol[0], "pde": pol[1], "drive": pol[2]}

    # -- low level pin control -------------------------------------------
    def _reset_assert(self):
        self.reset.init(Pin.OUT, value=0)

    def _reset_release(self):
        self.reset.init(Pin.IN)  # let external/internal pull-up bring it high

    def reset_target(self, settle_ms=5):
        """Plain RESET pulse -- does NOT force active background mode, so
        the target just reboots and runs its own code normally. Use
        hardware_reset_to_bdm() instead when you want it to halt into BDM."""
        self._reset_assert()
        time.sleep_ms(settle_ms)
        self._reset_release()
        time.sleep_ms(settle_ms)

    def _power_drive_12ma(self):
        """Bump the power GPIO's pad drive to 12 mA. The RP2040 default is
        4 mA, and an MC9S08SG8 running at the FEI default draws more than
        that -- a sagging VDD would brown the target out instead of
        powering it, which looks exactly like 'the chip is dead'."""
        if self.power_pin_num is None:
            return
        addr = 0x4001C000 + 0x04 + 4 * self.power_pin_num  # PADS_BANK0.GPIOn
        mem32[addr] = (mem32[addr] & ~0x30) | 0x30  # DRIVE = 3 = 12 mA

    def power_on_reset_to_bdm(self, off_ms=100, on_reset_hold_ms=10,
                              bkgd_hold_us=100, release_bkgd=True,
                              hold_reset=True, settle_ms=2):
        """Genuine power-cycle BDM entry -- the ONLY documented path on this
        chip (datasheet Sec 17.1.1: external-pin and internal-error resets
        both ignore BKGD; only power-on reset latches active background).

        The ordering here is copied from the one known-working MC9S08SG8
        BDM programmer (mztulip/hcs08_bdm_programmer, `hcs08_prog.c`
        main() + `bdm.c` enter_background()) -- see CONTEXT.md Finding 63.
        The part that every previous attempt in this project got wrong is
        `hold_reset`: **RESET is asserted LOW across the entire power-up
        and only released ~10 ms later.** The reset SOURCE is then a
        genuine power-on reset (the one source Sec 17.1.1 says honours
        BKGD/MS), but the moment the chip EXITS reset is chosen by us,
        with BKGD comfortably low -- instead of a sub-microsecond race
        against the VDD ramp, after which the part is already free-running
        and every later reset is an "internally generated error reset"
        that Sec 17.1.1 says ignores BKGD.
        """
        if self.power is None:
            raise BdcError("no power_pin configured -- VDD must be wired to a GPIO")
        self._power_drive_12ma()
        bkgd = Pin(self.bkgd_pin_num, Pin.OUT, value=0)
        apply_pad(self.bkgd_pin_num)
        self._pio_bound = False
        if hold_reset:
            self._reset_assert()          # RESET LOW before power is cut
        self.power.value(0)               # VDD actively pulled to 0 (discharges C1)
        time.sleep_ms(int(off_ms))
        self.power.value(1)               # <-- POR happens here, BKGD+RESET low
        time.sleep_ms(int(on_reset_hold_ms))   # chip held in reset by the pin
        if hold_reset:
            self._reset_release()         # <-- chip EXITS reset here, BKGD low
        time.sleep_us(int(bkgd_hold_us))
        if release_bkgd:
            bkgd.init(Pin.IN)
            time.sleep_ms(int(settle_ms))

    def bdm_connect(self, variants=None, ms=25):
        """The whole documented BDM-entry-and-identify sequence, in one shot.

        This is the sequence the only known-working MC9S08SG8 programmer
        uses (CONTEXT.md Finding 63) plus the checks that make the answer
        unambiguous. It needs target VDD wired to the power GPIO.

        For each (off_ms, on_reset_hold_ms, bkgd_hold_us) variant it:
          1. power-cycles with RESET AND BKGD held low, releases RESET,
             then releases BKGD  -> should latch active background mode;
          2. asks `bdm_check` whether the 40 us blank-FLASH reset loop has
             STOPPED (the command-free proof that the CPU is halted);
          3. if it has, runs a plain (not phase-locked) sync and then reads
             the three registers whose reset values are known and are
             neither 0x00 nor 0xFF, so a real read cannot be confused with
             a floating line:
               $1807 SDIDL  -> 0x14 on an MC9S08SG8 (datasheet Fig 5-7)
               $1806 SDIDH  -> bit 7 = 1, bits 3:0 = 0 (Fig 5-6)
               $1800 SRS    -> POR should be set (0x82) after a power-on
                               reset (Table 5-3)
             plus READ_STATUS (BDCSCR), which in active BDM should show
             ENBDM=1, BDMACT=1, CLKSW=1.
        """
        if self.power is None:
            raise BdcError("no power_pin configured -- move target VDD to "
                           "the Pico's GPIO%s and retry"
                           % (self.power_pin_num,))
        if variants is None:
            # (off_ms, on_reset_hold_ms, bkgd_hold_us)
            variants = ((100, 10, 2000), (250, 50, 2000), (100, 1, 200),
                        (500, 10, 20000), (100, 10, 100))
        results = []
        for off_ms, hold_ms, bkgd_us in variants:
            self.power_on_reset_to_bdm(off_ms=off_ms, on_reset_hold_ms=hold_ms,
                                       bkgd_hold_us=bkgd_us)
            chk = self.bdm_check(ms)
            entry = {"off_ms": off_ms, "on_reset_hold_ms": hold_ms,
                     "bkgd_hold_us": bkgd_us,
                     "reset_low_pct": chk["reset_low_pct"],
                     "sync_hits": chk["sync_hits"],
                     "sync_pulse_us": chk["sync_pulse_us"]}
            halted = chk["reset_low_pct"] < 2.0 or chk["sync_hits"] >= 10
            entry["halted"] = halted
            if halted:
                try:
                    entry["target_freq_hz"] = self.sync()
                    entry["bdcscr"] = self.read_status()
                    entry["sdidh_1806"] = self.read_byte(0x1806)
                    entry["sdidl_1807"] = self.read_byte(0x1807)
                    entry["srs_1800"] = self.read_byte(0x1800)
                except BdcError as e:
                    entry["error"] = str(e)
            results.append(entry)
            if halted and entry.get("sdidl_1807") not in (None, 0x00, 0xFF):
                return {"connected": True, "results": results}
        return {"connected": False, "results": results}

    def bdm_check(self, ms=30):
        """Is the target halted in active background mode?

        Two independent, command-free observables (see CONTEXT.md Findings
        25 and 47):
          reset_low_pct  ~24%/13% = still free-running in its blank-FLASH
                         reset loop;  ~0% = the CPU is halted.
          sync_hits      a halted target has no 40 us reset loop, so plain
                         (non-phase-locked) sync should succeed nearly
                         every time instead of ~20%.
          sync_pulse_us  active BDM resets CLKSW to 1 (bus clock), so the
                         128-cycle answer grows ~4x (7.6 us -> ~30 us).
        """
        r = self.probe_pin(self.reset_pin_num, ms)
        hits, widths = 0, []
        for _ in range(12):
            try:
                f = self.sync()
            except BdcError:
                continue
            if f:
                hits += 1
                widths.append(round(128.0 / f * 1e6, 2))
        return {
            "reset_low_pct": r["low_pct"],
            "reset_transitions": r["transitions"],
            "sync_hits": hits,
            "sync_attempts": 12,
            "sync_pulse_us": widths,
        }

    def hardware_reset_to_bdm(self, settle_ms=5, reset_ms=None,
                              bkgd_hold_us=1000, pre_low_ms=1,
                              release_bkgd=True):
        """Hold BKGD low across a RESET pulse to force active background
        mode on power-up/reset, per AN3335 'hardware method'.

        Timing knobs, all of them things the S08's documented mode-latch
        behaviour could plausibly care about, so they are exposed rather
        than baked in:

          pre_low_ms    how long BKGD is already low BEFORE RESET is
                        asserted (the MS latch samples BKGD around the
                        rising edge of the internal reset, but a target
                        that is free-running needs BKGD settled first).
          reset_ms      how long RESET is held low (defaults to settle_ms).
          bkgd_hold_us  how long BKGD stays low AFTER RESET is released.
                        This is the one that matters: the *internal* reset
                        of a free-running S08 can outlast the external pin
                        (this target drives RESET itself for ~8.6 us after
                        we let go), and the mode is latched at the end of
                        the internal sequence, not at our release.
          release_bkgd  set False to leave BKGD held low on return, so the
                        caller can measure the target while the supposed
                        BDM-entry condition is still applied.
        """
        if reset_ms is None:
            reset_ms = settle_ms
        # NOTE: requires the SM to be stopped / pin under plain GPIO control
        # during this sequence -- see main.py orchestration.
        bkgd = Pin(self.bkgd_pin_num, Pin.OUT, value=0)
        apply_pad(self.bkgd_pin_num)
        self._pio_bound = False  # that Pin() just took the pin back to SIO
        if pre_low_ms:
            time.sleep_ms(int(pre_low_ms))
        self._reset_assert()
        time.sleep_ms(int(reset_ms))
        self._reset_release()
        time.sleep_us(int(bkgd_hold_us))
        if release_bkgd:
            bkgd.init(Pin.IN)  # release BKGD, let pull-up bring it high
            time.sleep_ms(settle_ms)

    # -- SYNC calibration ---------------------------------------------------
    def sync_locked(self, host_low_us=20, d_from=8, d_to=110, d_step=2,
                    trials=3):
        """Phase-locked SYNC — the reliable one. Returns a dict.

        A free-running S08 with blank FLASH sits in an illegal-opcode reset
        loop (40.0 us on this target). A SYNC fired at a random point in that
        loop answers only ~20% of the time, and when it does answer the
        answer is usually **truncated** by the next reset — which makes the
        derived frequency too HIGH, not too low, because the frequency is
        128 / pulse_width. That is how this project spent two sessions
        believing numbers between 16.3 and 28.5 MHz.

        Two ideas make it deterministic:

        1. Drive the reset ourselves (`after_reset_sync`) so the target's
           cycle is phase-locked to us, and scan `delay_us` across more than
           one full loop period.
        2. **Take the MAXIMUM pulse width seen, not the first or the mean.**
           Truncation can only ever shorten the response; nothing can
           lengthen it past 128 BDC cycles. So the longest pulse observed
           across the scan is the true one, and every short one is noise.

        Measured on this hardware: at the best delay the target answers 8/8
        with a 7.52-7.60 us pulse = 16.84-17.0 MHz, which is the
        MC9S08SG8's FEI-default DCO (BDCSCR.CLKSW resets to 0 = alternate
        BDC clock = that DCO).

        NOTE: this asserts RESET, so do not call it on a target you have
        successfully halted in active BDM — it would knock it back out.
        """
        probe_freq, window_s, _ = SYNC_ATTEMPTS[0]
        measure_iters = max(1, int(probe_freq * window_s / 2))
        best_d = None
        best_cycles = 0
        hits = 0
        attempts = 0
        for d in range(int(d_from), int(d_to), int(d_step)):
            for _ in range(int(trials)):
                attempts += 1
                raw = self.after_reset_sync(d, host_low_us)
                if raw in (SYNC_RESULT_NO_FALL, SYNC_RESULT_STUCK_LOW,
                           SYNC_RESULT_OVERFLOW):
                    continue
                hits += 1
                elapsed = 2 * (measure_iters - raw)
                if elapsed > best_cycles:
                    best_cycles = elapsed
                    best_d = d
        if not best_cycles:
            raise BdcError(
                "phase-locked SYNC found no response in %d attempts across "
                "delays %d-%d us -- target silent" % (attempts, d_from, d_to)
            )
        target_freq = 128.0 * probe_freq / best_cycles
        if not (TARGET_FREQ_MIN_HZ <= target_freq <= TARGET_FREQ_MAX_HZ):
            raise BdcError(
                "phase-locked SYNC derived an implausible %.0f Hz BDC clock"
                % target_freq
            )
        self.target_freq_hz = target_freq
        self.best_delay_us = best_d
        self._configure_bit_timing(target_freq)
        return {
            "target_freq_hz": target_freq,
            "pulse_us": best_cycles / (probe_freq / 1e6),
            "best_delay_us": best_d,
            "hits": hits,
            "attempts": attempts,
        }

    def sync(self, retries=None):
        """
        Perform the SYNC handshake to discover the target's BDC clock
        frequency, and configure the state machine's clock divider
        accordingly. Returns the measured target frequency in Hz.

        *** PREFER sync_locked() AGAINST A FREE-RUNNING TARGET. ***
        This one fires at an arbitrary point in the target's reset loop, so
        against the S08 on this bench it answers only ~20% of the time, and
        when it does the answer is usually TRUNCATED by the target's next
        reset — which makes the derived frequency too HIGH (frequency is
        128 / pulse_width). Values from 16.3 to 28.5 MHz have come out of
        this path for one target whose real BDC clock is 16.84 MHz.
        `sync_locked()` drives the reset itself and takes the longest pulse
        of a scan, which is provably the untruncated one.

        `retries` caps how many rungs of SYNC_ATTEMPTS to walk (default:
        all of them). Each rung is a genuinely different probe — a slower
        clock with a proportionally longer measuring window — so a retry
        can succeed where the previous one overflowed or timed out.

        See bdc_pio.bdc_sync for the PIO-side algorithm and its exact
        result encoding.
        """
        attempts = SYNC_ATTEMPTS
        if retries is not None:
            attempts = SYNC_ATTEMPTS[: max(1, min(int(retries), len(SYNC_ATTEMPTS)))]

        last_reason = "no attempts made"

        for probe_freq, window_s, host_low_s in attempts:
            # Both the wait-for-edge loops and the measuring loop are
            # 2 PIO cycles per iteration; the host-low loop is 1.
            host_low_cycles = max(1, int(probe_freq * host_low_s))
            wait_iters = max(1, int(probe_freq * window_s / 2))
            measure_iters = wait_iters

            sm = make_state_machine(bdc_sync, self.bkgd_pin_num, probe_freq, SM_SYNC)
            # bdc_sync lives on PIO block 0; the tx/rx SMs live on block 1,
            # so this steals the pin's function select from them.
            self._pio_bound = False
            sm.active(1)
            sm.put(host_low_cycles)
            sm.put(wait_iters)
            sm.put(measure_iters)
            # Every loop inside bdc_sync is bounded by one of the counts we
            # just pushed, so this get() is guaranteed to return.
            result = sm.get() & 0xFFFFFFFF
            sm.active(0)

            if result == SYNC_RESULT_STUCK_LOW:
                last_reason = (
                    "BKGD never returned high after we released it -- line "
                    "shorted low, target driving it, or no pull-up"
                )
                continue
            if result == SYNC_RESULT_NO_FALL:
                last_reason = (
                    "no response pulse from target within %d ms -- check "
                    "wiring (BKGD, RESET, GND) and that the target is "
                    "powered" % int(window_s * 1000)
                )
                continue
            if result == SYNC_RESULT_OVERFLOW:
                last_reason = (
                    "target response pulse longer than the %d ms measuring "
                    "window -- target BDC clock is slower than this rung "
                    "can measure" % int(window_s * 1000)
                )
                continue

            # `result` is the LEFTOVER counter value, so elapsed time is the
            # difference from where it started, times 2 PIO cycles per
            # decrement. (The old code used the leftover value directly as
            # if it were the elapsed count -- exactly backwards.)
            elapsed_cycles = 2 * (measure_iters - result)
            if elapsed_cycles <= 0:
                last_reason = "implausible SYNC measurement (0 cycles elapsed)"
                continue

            # The response pulse is exactly 128 target BDC cycles wide:
            #   elapsed_cycles / probe_freq = 128 / target_freq
            target_freq = 128.0 * probe_freq / elapsed_cycles
            if not (TARGET_FREQ_MIN_HZ <= target_freq <= TARGET_FREQ_MAX_HZ):
                last_reason = (
                    "implausible SYNC measurement: derived %.0f Hz BDC clock"
                    % target_freq
                )
                continue

            self.target_freq_hz = target_freq
            self._configure_bit_timing(target_freq)
            return self.target_freq_hz

        raise BdcError("SYNC failed: " + last_reason)

    def _configure_bit_timing(self, target_freq):
        """Pick the state-machine clock used for every subsequent bit.

        One PIO cycle per target BDC cycle — see the BDC_CYCLES_PER_BIT
        comment near the top of this file for why that ratio, and not an
        oversampled one, is the right choice here.
        """
        sm_freq = int(target_freq)
        if sm_freq > SM_MAX_FREQ_HZ:
            raise BdcError(
                "target BDC clock %.0f Hz is faster than the %d Hz maximum "
                "state-machine clock this driver will use"
                % (target_freq, SM_MAX_FREQ_HZ)
            )
        # The RP2040's PIO clock divider is 16.8 fixed point, so the slowest
        # achievable SM clock is sys_clk/65536 (~1.9 kHz at 125 MHz).
        # Asking for less than that fails inside StateMachine() with a much
        # less helpful message.
        if sm_freq < SM_MIN_FREQ_HZ:
            raise BdcError(
                "target BDC clock %.0f Hz is too slow for this driver "
                "(below the RP2040's %d Hz minimum state-machine clock)"
                % (target_freq, SM_MIN_FREQ_HZ)
            )
        self.sm_freq = sm_freq
        self._pio_bound = False   # force the tx/rx SMs to be rebuilt

    def set_bit_clock(self, hz):
        """Force the bit clock WITHOUT running SYNC.

        SYNC derives the target's BDC clock from the assumption that the
        ~8 us low pulse it measures is a 128-cycle SYNC response. If that
        assumption is wrong (it is the same width as this part's 34-bus-cycle
        internal reset drive at the default 4.19 MHz bus clock), every bit
        this driver sends is off by the same factor — so being able to sweep
        the bit rate independently of SYNC is the only way to test it.
        """
        hz = int(hz)
        self.target_freq_hz = float(hz)
        self._configure_bit_timing(float(hz))
        return hz

    def _bit_window_us(self):
        """Real time one full 16-cycle bit window takes, in us, rounded up
        with a little margin."""
        return int(BDC_CYCLES_PER_BIT * 1_000_000 // self.sm_freq) + 2

    # -- byte-level tx/rx ---------------------------------------------------
    def _ensure_sms(self):
        """(Re)bind the cached tx/rx state machines to the BKGD pin.

        Constructing a StateMachine costs a few hundred us, so this happens
        once per sync()/reset rather than once per bit — the old code did it
        per bit, which is where the 332 us/bit came from. Rebinding is only
        needed after something else has taken the pin's function select
        (a plain `Pin()`, or the SYNC/capture state machines).
        """
        if self.sm_freq is None:
            raise BdcError("call sync() before any tx/rx operation")
        if self._pio_bound and self._tx is not None:
            return
        self._tx = make_state_machine(
            bdc_tx_byte, self.bkgd_pin_num, self.sm_freq, SM_TX
        )
        self._rx = None            # there is no second SM any more; see _xfer
        self._pio_bound = True

    # ------------------------------------------------------------------
    # THE transaction primitive. Everything that reads goes through here.
    #
    # A 0xFF byte out of `bdc_tx_byte` is bit-for-bit a BDC read-bit
    # marker (3 cycles low, release, a 13-cycle window in which the target
    # may answer), and `bdc_tx_byte` samples BKGD at cycle 10 of every
    # bit. So `[command..., 0xFF, ...]` in one FIFO is the command AND its
    # read phase as ONE uninterrupted bit stream on ONE state machine.
    #
    # This is not a micro-optimisation, it is the difference between
    # reading data and reading nothing. Measured back to back on hardware
    # in state S (CONTEXT.md Finding 79): contiguous draws a real 13-cycle
    # target-driven low in the read window; the same waveform with a
    # TX-SM -> RX-SM handover in the middle draws 46 us of silence and
    # reads the pull-up's 0xFF at every address in the map.
    # ------------------------------------------------------------------
    # Longest burst that preloads entirely into the 4-word TX FIFO, one
    # byte per word. Four byte-times is 55 us at 9.25 MHz and covers every
    # BDC command this driver issues — memory reads fetch their data with
    # READ_LAST (E8/SS/RD, three bytes, no delay) rather than with
    # READ_BYTE's own delayed data phase, which is what used to need five.
    # Packing four bytes per word to lift this to 16 was tried and reverted
    # on hardware; see bdc_pio.bdc_tx_byte.
    XFER_MAX_BYTES = 4

    @staticmethod
    def _pack(seq):
        """Bytes -> FIFO words: one byte per word, left-justified, because
        `bdc_tx_byte` shifts MSB-first out of a 32-bit OSR and sends eight
        bits per `pull`."""
        return [(v & 0xFF) << 24 for v in seq]

    def _xfer(self, tx, nread=0):
        """ONE contiguous transaction: `tx` out, `nread` bytes read back.

        A 0xFF byte out of `bdc_tx_byte` is bit-for-bit a BDC read-bit
        marker (3 cycles low, release, a 13-cycle window in which the
        target may answer), and `bdc_tx_byte` samples BKGD at cycle 10 of
        every bit. So `[command..., 0xFF, ...]` is the command AND its read
        phase as one uninterrupted bit stream on one state machine.

        Returns `len(tx) + nread` sampled bytes, one per transmitted byte.
        The first `len(tx)` are the samples taken while we were driving, so
        they come back equal to `tx` itself (a '1' bit is released by cycle
        4 and samples high, a '0' bit is still driven low at cycle 10 and
        samples low). That is a free per-transaction check that the bit
        grid is aligned and that the SM clock matches the target.

        NOTHING runs in Python between the first bit and the last: the
        whole burst is in the FIFO before the state machine starts. That is
        the entire point — CONTEXT.md Finding 79 (a state-machine handover
        in the middle) and Finding 83 (a Python-paced FIFO top-up in the
        middle) are the same bug twice, and both read nothing but 0xFF.
        """
        tx = [v & 0xFF for v in tx]
        want = len(tx) + int(nread)
        if want < 1:
            raise BdcError("_xfer: nothing to send")
        if want > self.XFER_MAX_BYTES:
            raise BdcError(
                "_xfer: %d bytes; only %d fit in the TX FIFO, and anything "
                "Python has to feed mid-burst is no longer contiguous"
                % (want, self.XFER_MAX_BYTES)
            )
        seq = tx + [0xFF] * int(nread)

        self._ensure_sms()
        sm = self._tx
        sm.active(0)
        # restart() clears PC and both shift registers/counters but NOT the
        # FIFOs, so drain stale samples by hand or they arrive as the answer
        # to the *next* command.
        sm.restart()
        while sm.rx_fifo():
            sm.get()
        for w in self._pack(seq):
            sm.put(w)
        sm.active(1)
        out = []
        deadline = time.ticks_add(time.ticks_ms(), 100)
        while len(out) < want:
            if sm.rx_fifo():
                out.append(sm.get() & 0xFF)
            elif time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                break
        # The last sampled byte lands at cycle 10 of the last bit, with six
        # cycles of that bit still to run. Stopping the SM right there
        # freezes `pindirs` mid-bit — and for a final '0' bit that means
        # BKGD is left DRIVEN LOW, for however long Python takes to get to
        # the next transaction. Milliseconds of low on BKGD is not nothing:
        # it is longer than the 128 target cycles that define a SYNC
        # request, so the target would see a SYNC where a command should
        # be. Let the bit finish, then release explicitly.
        time.sleep_us(3)
        sm.active(0)
        sm.exec(SET_PINDIRS_IN)
        if len(out) < want:
            raise BdcError(
                "_xfer: state machine returned %d of %d sampled bytes"
                % (len(out), want)
            )
        return out

    def _xfer_long(self, tx, nread=0):
        """Gone. Topping the FIFO up from Python mid-burst is the bug, not
        a workaround for the FIFO being small — see CONTEXT.md Finding 83.
        Split the work into separate 4-byte transactions instead; READ_LAST
        exists precisely so a memory read can be two of them."""
        raise BdcError(
            "_xfer_long() no longer exists: a burst must fit in the FIFO "
            "before the state machine starts. Use two _xfer() calls."
        )

    def _xfer_read(self, tx, nread, delay=1):
        """`_xfer` reduced to the integer the caller wants.

        `delay` is the BDC command delay `d`: the number of idle byte-times
        inserted between the command and the data phase while the target
        performs its memory cycle. The BDC command table writes a memory
        read as `E0 / AAAA / d / RD`, and sending d = 0 does not fail
        loudly — it returns the PREVIOUS command's data, lagged by exactly
        one, which is how this was finally caught (CONTEXT.md Finding 83).
        Commands with no `d` in the table (READ_STATUS, the CPU register
        reads) pass delay=0.
        """
        words = self._xfer(tx, int(delay) + int(nread))
        echo = words[:len(tx)]
        if list(echo) != [v & 0xFF for v in tx]:
            raise BdcError(
                "transfer desynced: sent %s but sampled %s back while "
                "driving it"
                % ("".join("%02X" % v for v in tx),
                   "".join("%02X" % v for v in echo))
            )
        value = 0
        for b in words[len(tx) + int(delay):]:
            value = (value << 8) | b
        return value

    def _tx_bytes(self, *values):
        """Clock out whole bytes back-to-back, MSB first, no gaps.

        The TX FIFO is 4 words deep and one byte is one word, so up to 4
        bytes leave the Pico as one uninterrupted bit stream — which covers
        every BDC command this driver sends (the longest is WRITE_BYTE:
        opcode, address high, address low, data).
        """
        if len(values) > 4:
            raise BdcError("_tx_bytes: at most 4 bytes fit in the TX FIFO")
        self._xfer(values, 0)

    def _tx_bytes_unchecked(self, *values):
        """The old fixed-sleep transmit, kept only for the reset-window
        experiments that poke the PIO CTRL register directly."""
        self._ensure_sms()
        sm = self._tx
        # *** ORDER IS LOAD-BEARING: fill the FIFO *before* starting the SM.
        # It used to be active(1) first and then one put() per byte, which is
        # NOT contiguous: a MicroPython put() costs several microseconds, and
        # a byte is only 7.6 us of signal at 16.9 MHz, so the SM drained and
        # re-stalled between bytes. Measured on hardware with the scope SM,
        # tx=[0xE4, 0x00]: byte 1 at t=0-7.5 us, byte 2 not until t=13-16 us,
        # a 6-9 us hole that moved run to run. CONTEXT.md claimed multi-byte
        # commands left the Pico "as one uninterrupted bit stream"; they did
        # not. That matters enormously against this target, whose blank-FLASH
        # reset loop only leaves a ~30 us window between resets — a 3-byte
        # READ_BYTE was spending ~30 us on gaps alone.
        # restart() clears the PC/OSR/ISR (not the FIFOs) so a previous
        # deactivation mid-program cannot leave stale shift state behind.
        sm.restart()
        while sm.rx_fifo():
            sm.get()
        for w in self._pack(values):
            sm.put(w)
        sm.active(1)
        # put() only queues; the SM runs asynchronously. Wait out the whole
        # burst (8 bits x 16 cycles per byte) before stopping it, or the last
        # bit gets truncated mid-pulse.
        time.sleep_us(self._bit_window_us() * 8 * len(values) + 20)
        sm.active(0)

    def _rx_bits(self, nbits):
        """DELETED as a standalone operation — see CONTEXT.md Finding 79.

        A read phase issued *after* a command has already been sent is
        exactly the thing that does not work on this target. It is kept
        only as a loud error so no future code path quietly reintroduces
        it; reads go through `_xfer`/`_xfer_read` with the markers in the
        same FIFO burst as the command.
        """
        raise BdcError(
            "_rx_bits() no longer exists: a read phase must be part of the "
            "same contiguous burst as its command. Use _xfer_read(tx, n)."
        )

    # Thin alias kept for the single-opcode commands (BACKGROUND, GO,
    # TRACE1, TAGGO) that have no operands. Multi-byte commands must use
    # _tx_bytes directly so their bytes go out as one uninterrupted stream.
    def _write_byte_raw(self, value):
        self._tx_bytes(value)

    # -- raw / diagnostic primitives --------------------------------------
    def raw_xfer(self, tx, nbits=0):
        """Send an arbitrary byte sequence and optionally read `nbits` back.

        This exists because every higher-level command hard-codes its own
        byte layout, which makes it impossible to test a hypothesis about
        the wire protocol without editing firmware.

        The read phase is `nbits // 8` marker bytes appended to the SAME
        FIFO burst, so `gap_us` is now structurally zero — there is no
        turnaround to measure any more. It is still reported (as 0) so the
        existing host/diagnostic JSON shape does not change.

        Returns (value, tx_us, gap_us, rx_us).
        """
        n = len(tx)
        if nbits and nbits % 8:
            raise BdcError("raw_xfer: nbits must be a multiple of 8")
        nread = (nbits or 0) // 8
        if n + nread > 4:
            raise BdcError(
                "raw_xfer: %d command bytes + %d read bytes exceeds the "
                "4-word FIFO" % (n, nread)
            )
        t0 = time.ticks_us()
        words = self._xfer(tx, nread)
        t1 = time.ticks_us()
        if not nread:
            return (None, time.ticks_diff(t1, t0), 0, 0)
        value = 0
        for b in words[n:]:
            value = (value << 8) | b
        return (value, time.ticks_diff(t1, t0), 0, 0)

    def raw_xfer_full(self, tx, nbits=0):
        """`raw_xfer` but returning every sampled byte, echo included.

        The echo bytes (the samples taken while WE were driving) are the
        alignment check: they must equal `tx`. A mismatch means the bit
        grid slipped, which is a different failure from "the target said
        nothing", and telling the two apart by hand was costing whole
        sessions.
        """
        nread = (nbits or 0) // 8
        n = len(tx)
        words = self._xfer(tx, nread)
        value = None
        if nread:
            value = 0
            for b in words[n:]:
                value = (value << 8) | b
        return {
            "sampled": words,
            "echo": words[:n],
            "echo_ok": list(words[:n]) == [v & 0xFF for v in tx],
            "value": value,
        }

    def probe_pin(self, pin_num, ms=30, pull=None):
        """Measure activity on any GPIO with plain SIO reads: the fraction
        of samples that read low, and how many transitions occurred.

        Used to answer "is the target free-running / resetting?" without a
        logic analyzer — the target's RESET pin pulses while it loops on an
        illegal-address reset, and goes quiet when it halts.
        """
        if pull == "up":
            p = Pin(pin_num, Pin.IN, Pin.PULL_UP)
        elif pull == "down":
            p = Pin(pin_num, Pin.IN, Pin.PULL_DOWN)
        else:
            p = Pin(pin_num, Pin.IN, None)
        if pin_num == self.bkgd_pin_num:
            self._pio_bound = False
        low = 0
        total = 0
        trans = 0
        last = p.value()
        deadline = time.ticks_add(time.ticks_ms(), int(ms))
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            v = p.value()
            total += 1
            if not v:
                low += 1
            if v != last:
                trans += 1
                last = v
        return {
            "pin": pin_num,
            "pull": pull,
            "samples": total,
            "low_pct": (100.0 * low / total) if total else 0.0,
            "transitions": trans,
        }

    def rise_time(self, pin_num, hold_ms=1, trials=6):
        """Drive a pin low for `hold_ms`, release it to Hi-Z with NO internal
        pull, and time how long the external pull-up takes to bring it high.

        Sweeping `hold_ms` is a capacitance probe. A node that is only a
        pull-up plus a few tens of pF recovers in ~1 us no matter how long it
        was held down. A node that is charge-coupled to a big capacitor (e.g.
        the target's C1 through an ESD clamp diode, which is what parasitic
        powering looks like) takes far longer AND takes longer the longer it
        was held, because more charge had to be drained first.
        """
        p = Pin(pin_num, Pin.IN)
        if pin_num == self.bkgd_pin_num:
            self._pio_bound = False
        out = []
        for _ in range(int(trials)):
            p.init(Pin.OUT, value=0)
            time.sleep_ms(int(hold_ms))
            p.init(Pin.IN)
            t0 = time.ticks_us()
            limit = time.ticks_add(t0, 50_000)
            while p.value() == 0:
                if time.ticks_diff(limit, time.ticks_us()) <= 0:
                    out.append(-1)
                    break
            else:
                out.append(time.ticks_diff(time.ticks_us(), t0))
            time.sleep_ms(20)
        return {"pin": pin_num, "hold_ms": hold_ms, "rise_us": out}

    def drive_pin(self, pin_num, level):
        """Hold a GPIO low (level=0), drive it high (level=1), or release it
        to Hi-Z (level=None), and leave it that way until the next call.

        Deliberately sticky: several of the most informative experiments are
        "hold this line somewhere and then measure the other one", which is
        impossible if the pin snaps back when the command returns.
        """
        p = Pin(pin_num, Pin.IN)
        if pin_num == self.bkgd_pin_num:
            self._pio_bound = False
        if level is None:
            p.init(Pin.IN)
        else:
            p.init(Pin.OUT, value=1 if level else 0)
        time.sleep_us(200)
        return {"pin": pin_num, "driven": level}

    # -- self-diagnostic capture (no external logic analyzer needed) -------
    # Arms a passive PIO "scope" state machine (edge-triggered, so it
    # doesn't need to be precisely time-aligned with the operation it's
    # watching) and runs one of a few canned test operations while it
    # records. Returns raw samples + the time each sample represents, so
    # the host app can render/inspect the actual waveform.
    def _capture(self, sample_count, drive_fn, window_us=None):
        """Record BKGD while `drive_fn()` runs.

        `window_us`, if given, asks for a capture that spans roughly that
        much real time across `sample_count` samples — the sample rate is
        derived from it. Without it the rate is chosen for fine bit-level
        detail (~5 samples per target BDC cycle), which is the right choice
        for looking at a single bit and the wrong one for looking at a whole
        transaction. Being able to zoom out is what turned this from a
        decorative feature into the thing that actually found the bugs
        recorded in CONTEXT.md; captures at 500 ns, 1.5 us, 5 us and 15 us
        per sample each showed something the others could not.

        REAL-TIME CAVEAT: the RX FIFO is 4 words = 128 samples deep and the
        draining loop below is plain Python. Once the FIFO fills, the PIO
        program stalls on its autopush, so samples beyond the first 128 are
        contiguous with each other but NOT contiguous in time with the ones
        before them, unless the sample period is slow enough (>= ~2 us) for
        Python to keep up. Trust the first 128 samples of a fast capture.
        """
        sample_count = int(sample_count)
        if sample_count <= 0 or sample_count % 32 != 0:
            raise BdcError("sample_count must be a positive multiple of 32")

        if window_us:
            # samples * CYCLES_PER_SAMPLE / cap_freq = window
            cap_freq = int(sample_count * CYCLES_PER_SAMPLE * 1e6 / window_us)
        else:
            if self.sm_freq is None:
                # no SYNC yet -- use a conservative default so this still
                # works standalone, e.g. to sanity check SYNC itself.
                cap_target_hz = 1_000_000
            else:
                cap_target_hz = self.target_freq_hz
            # aim for ~5 samples per target BDC cycle
            cap_freq = int(cap_target_hz * 5 * CYCLES_PER_SAMPLE)
        cap_freq = int(min(max(cap_freq, SM_MIN_FREQ_HZ), SM_MAX_FREQ_HZ))

        cap_sm = make_capture_state_machine(self.bkgd_pin_num, cap_freq)
        # the capture SM's Pin()/StateMachine construction takes the BKGD
        # pin's function select away from the cached tx/rx SMs.
        self._pio_bound = False
        cap_sm.active(1)
        # NOTE: this put() is what used to hang the whole Pico. bdc_sample
        # was declared fifo_join=PIO.JOIN_RX, which gives up the TX FIFO
        # entirely, so this call spun forever inside MicroPython's blocking
        # C code and drive_fn() below was never reached. The join is gone;
        # do not re-add it. (See bdc_pio.bdc_sample's comment.)
        cap_sm.put(sample_count)  # arms the trigger-wait inside the program

        time.sleep_us(50)  # give the capture SM a moment to reach the 'wait'
        drive_fn()

        words = []
        deadline = time.ticks_add(time.ticks_ms(), 1000)
        needed_words = sample_count // 32
        while len(words) < needed_words:
            if cap_sm.rx_fifo():
                words.append(cap_sm.get())
            elif time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                break
        cap_sm.active(0)

        sample_period_ns = int(1e9 * CYCLES_PER_SAMPLE / cap_freq)
        bits = []
        for w in words:
            for i in range(31, -1, -1):
                bits.append((w >> i) & 1)
        return {"samples": bits, "sample_period_ns": sample_period_ns}

    def capture_sync(self, sample_count=256, window_us=600):
        """Capture a whole SYNC handshake.

        The window has to be comfortably LONGER than the host's own low
        pulse or all you capture is your own drive — 600 us against a
        200 us host low leaves room for the gap and the target's 128-cycle
        answer. Before the SYNC_ATTEMPTS fix this could never show anything
        at all: the host held BKGD low for 10 ms while the capture window
        was 6.4 us, so every capture was a flat line.
        """
        probe_freq, window_s, host_low_s = SYNC_ATTEMPTS[0]
        host_low_cycles = max(1, int(probe_freq * host_low_s))
        wait_iters = max(1, int(probe_freq * window_s / 2))

        def drive():
            sm = make_state_machine(
                bdc_sync, self.bkgd_pin_num, probe_freq, SM_SYNC
            )
            self._pio_bound = False
            sm.active(1)
            sm.put(host_low_cycles)
            sm.put(wait_iters)
            sm.put(wait_iters)
            sm.get()
            sm.active(0)

        return self._capture(sample_count, drive, window_us)

    def capture_write_bit(self, bit, sample_count=256, window_us=None):
        """Capture one transmitted BYTE (0x00 or 0xFF) so both bit shapes
        are visible. The bit-at-a-time PIO program this used to drive no
        longer exists; a byte of all-same bits shows the same thing and
        also shows the bit-to-bit spacing, which is what was actually
        wrong (CONTEXT.md Finding 6)."""
        if self.sm_freq is None:
            raise BdcError("call sync() before capture_write_bit()")
        value = 0xFF if bit else 0x00
        return self._capture(
            sample_count, lambda: self._tx_bytes(value), window_us
        )

    def capture_read_bit(self, sample_count=256, window_us=None):
        """Capture a bare 8-marker read phase with no command in front of
        it. This is the negative control for every read experiment: the
        markers are identical to a real read phase, so anything the target
        drives here would be spontaneous rather than an answer."""
        if self.sm_freq is None:
            raise BdcError("call sync() before capture_read_bit()")
        result = {}

        def drive():
            try:
                result["bit"] = self._xfer((), 1)[0]
            except BdcError:
                result["bit"] = None

        cap = self._capture(sample_count, drive, window_us)
        cap["bit_value"] = result.get("bit")
        return cap

    def capture_command(self, opcode, addr=None, sample_count=256,
                        window_us=None):
        """Capture an arbitrary command going out on the wire.

        This is the diagnostic that was missing: it shows the actual bit
        stream of a real BDC command and whatever the target does in
        response, instead of one isolated bit with no context.
        """
        if self.sm_freq is None:
            raise BdcError("call sync() before capture_command()")
        if addr is None:
            args = (opcode & 0xFF,)
        else:
            args = (opcode & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF)
        return self._capture(sample_count, lambda: self._tx_bytes(*args),
                             window_us)

    def _sync_drive(self, host_low_s=None):
        """Run one SYNC handshake (no measurement kept) — used as a capture
        stimulus."""
        probe_freq, window_s, default_low_s = SYNC_ATTEMPTS[0]
        if host_low_s is None:
            host_low_s = default_low_s
        host_low_cycles = max(1, int(probe_freq * host_low_s))
        wait_iters = max(1, int(probe_freq * window_s / 2))
        sm = make_state_machine(bdc_sync, self.bkgd_pin_num, probe_freq, SM_SYNC)
        self._pio_bound = False
        sm.active(1)
        sm.put(host_low_cycles)
        sm.put(wait_iters)
        sm.put(wait_iters)
        r = sm.get()
        sm.active(0)
        return r

    # -- phase-locked stimulus --------------------------------------------
    # The target free-runs in a blank-FLASH reset loop with a rock-steady
    # 40.0 us period (CONTEXT.md Finding 25), and both the SYNC response and
    # any command have to fit inside the ~30 us it spends out of reset. Every
    # earlier experiment fired at a RANDOM point in that cycle, which is why
    # the SYNC hit rate looked like unexplained ~20% noise. These two make
    # OUR reset release the time origin, so the target's cycle is locked to
    # us and `delay_us` sweeps the whole 40 us window deterministically.
    #
    # Jitter note: the delay is a MicroPython `ticks_us` busy-wait and the
    # `active(1)` that follows costs a few us. Both are CONSTANT offsets, not
    # jitter, so the *shape* of a sweep is trustworthy even though its
    # absolute zero is only good to a few us.
    def _prime_reset(self, assert_us=60):
        self._reset_assert()
        time.sleep_us(int(assert_us))
        self._reset_release()
        return time.ticks_us()

    def after_reset_sync(self, delay_us, host_low_us=20):
        """Release RESET, wait `delay_us`, then run one SYNC. Returns the
        raw bdc_sync result word (see bdc_pio.bdc_sync for the encoding)."""
        probe_freq, window_s, _ = SYNC_ATTEMPTS[0]
        host_low_cycles = max(1, int(probe_freq * host_low_us / 1e6))
        wait_iters = max(1, int(probe_freq * window_s / 2))
        sm = make_state_machine(bdc_sync, self.bkgd_pin_num, probe_freq,
                                SM_SYNC)
        self._pio_bound = False
        sm.active(0)
        sm.restart()
        # 3 words, FIFO is 4 deep — preloading means active(1) below is the
        # only thing standing between the delay expiring and BKGD going low.
        sm.put(host_low_cycles)
        sm.put(wait_iters)
        sm.put(wait_iters)
        self._prime_reset()
        if delay_us > 0:
            # time.sleep_us busy-waits in C and is accurate to ~1 us; the
            # ticks_us python loop this replaced overshot by ~40 us.
            time.sleep_us(int(delay_us))
        sm.active(1)
        r = sm.get() & 0xFFFFFFFF
        sm.active(0)
        return r

    def after_reset_xfer(self, delay_us, tx, sample_count=128,
                         window_us=20.0, nbits=0):
        """Release RESET, wait `delay_us`, then clock out `tx` as one
        uninterrupted burst while the scope SM records BKGD.

        Pair this with the `[opcode..., 0xFF]` filler trick (see
        CONTEXT.md Finding 28): the 0xFF byte's drive pattern IS the BDC
        read-bit marker, so the read phase costs no turnaround at all.
        """
        if self.sm_freq is None:
            raise BdcError("call sync() or set_bit_clock() first")
        if len(tx) > 4:
            raise BdcError("after_reset_xfer: at most 4 tx bytes")
        sample_count = int(sample_count)
        if sample_count <= 0 or sample_count % 32 != 0:
            raise BdcError("sample_count must be a positive multiple of 32")
        cap_freq = int(sample_count * CYCLES_PER_SAMPLE * 1e6 / window_us)
        cap_freq = int(min(max(cap_freq, SM_MIN_FREQ_HZ), SM_MAX_FREQ_HZ))

        cap_sm = make_capture_state_machine(self.bkgd_pin_num, cap_freq)
        self._pio_bound = False
        cap_sm.active(1)
        cap_sm.put(sample_count)

        self._ensure_sms()
        tx_sm = self._tx
        tx_sm.active(0)
        tx_sm.restart()
        while tx_sm.rx_fifo():
            tx_sm.get()
        # The read phase is `nbits // 8` marker bytes in the SAME FIFO, not
        # a second state machine: see _xfer and CONTEXT.md Finding 79.
        # `nbits` is kept as the argument name only so the existing host
        # JSON and the older scan scripts keep working.
        nread = 0
        if nbits:
            if nbits % 8:
                raise BdcError("after_reset_xfer: nbits must be a multiple of 8")
            nread = nbits // 8
        if len(tx) + nread > 4:
            raise BdcError(
                "after_reset_xfer: %d command + %d read bytes exceeds the "
                "4-word FIFO" % (len(tx), nread)
            )
        words = self._pack(list(tx) + [0xFF] * nread)
        for w in words:
            tx_sm.put(w)
        total = len(tx) + nread
        nwords = len(words)          # one sampled word per transmitted byte
        # Exact, not the old _bit_window_us() ceiling: at 16.9 MHz that
        # rounded 0.95 us/bit up to 2 us, so a one-byte command sat idle for
        # 16 us before the read phase could start — longer than the target's
        # live window.
        tx_us = int(1e6 * BDC_CYCLES_PER_BIT * 8 * nwords / self.sm_freq) + 1

        # The hot sequence below is deliberately flat — no method calls, no
        # attribute lookups after this point — because each one costs more
        # than a whole BDC bit.
        set_reg = PIO1_CTRL_SET
        sleep_us = time.sleep_us
        ticks = time.ticks_us
        t0 = self._prime_reset()
        if delay_us > 0:
            sleep_us(int(delay_us))
        t1 = ticks()
        mem32[set_reg] = SM_TX_MASK          # start the whole burst
        sleep_us(tx_us)
        t2 = ticks()
        sw = []
        deadline = time.ticks_add(time.ticks_ms(), 20)
        while len(sw) < nwords:
            if tx_sm.rx_fifo():
                sw.append(tx_sm.get())
            elif time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                break
        tx_sm.active(0)
        sampled = [w & 0xFF for w in sw][:total]
        value = None
        if nread and len(sampled) == total:
            value = 0
            for b in sampled[len(tx):]:
                value = (value << 8) | b

        words = []
        deadline = time.ticks_add(time.ticks_ms(), 200)
        needed = sample_count // 32
        while len(words) < needed:
            if cap_sm.rx_fifo():
                words.append(cap_sm.get())
            elif time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                break
        cap_sm.active(0)
        bits = []
        for w in words:
            for i in range(31, -1, -1):
                bits.append((w >> i) & 1)
        return {"samples": bits,
                "sample_period_ns": int(1e9 * CYCLES_PER_SAMPLE / cap_freq),
                "value": value,
                "sampled": sampled,
                "echo_ok": (list(sampled[:len(tx)])
                            == [v & 0xFF for v in tx]),
                "spin_us": time.ticks_diff(t2, t1),
                "gap_us": 0}

    def scan_xfer(self, tx, d_from=0, d_to=44, d_step=1, trials=2,
                  sample_count=128, window_us=20.0, skip_us=None):
        """Fire `tx` as ONE uninterrupted burst at every phase of the
        target's reset loop and report, per phase, the LONGEST low run seen
        on BKGD *after* our own transmission has finished and released the
        line.

        BKGD is never driven low by anything except us (CONTEXT.md Finding
        22: 0.00% low, 0 transitions, at idle) so any non-zero number here
        is the target answering — an ACK pulse, a read-phase bit, anything.
        Zero everywhere means the target emitted nothing at all.

        This lives on the Pico on purpose. A phase scan is thousands of
        trials and one serial round-trip is ~30 ms, so the host-side
        version of this loop costs minutes per opcode; here it is
        ~2 ms/trial. That is what makes an all-256-opcode x all-phases
        sweep feasible (CONTEXT.md Finding 38).

        Everything expensive is hoisted out of the loop: both state
        machines are built once, and the burst is started by a single
        store to PIO1's atomic-set CTRL alias, exactly as
        `after_reset_xfer` does.
        """
        if self.sm_freq is None:
            raise BdcError("call sync() or set_bit_clock() first")
        tx = [int(b) & 0xFF for b in tx]
        if not tx or len(tx) > 4:
            raise BdcError("scan_xfer: 1..4 tx bytes")
        sample_count = int(sample_count)
        if sample_count <= 0 or sample_count % 32 != 0:
            raise BdcError("sample_count must be a positive multiple of 32")

        cap_freq = int(sample_count * CYCLES_PER_SAMPLE * 1e6 / window_us)
        cap_freq = int(min(max(cap_freq, SM_MIN_FREQ_HZ), SM_MAX_FREQ_HZ))
        ns = 1e9 * CYCLES_PER_SAMPLE / cap_freq
        sample_us = ns / 1000.0

        # our own transmission, in us and in samples
        tx_us = 1e6 * BDC_CYCLES_PER_BIT * 8 * len(tx) / self.sm_freq
        if skip_us is None:
            skip_us = tx_us + 0.8      # 0.8 us of slop past our last bit
        skip = int(skip_us / sample_us) + 1
        if skip >= sample_count:
            raise BdcError("scan_xfer: window_us too short for this tx")

        cap_sm = make_capture_state_machine(self.bkgd_pin_num, cap_freq)
        self._pio_bound = False
        self._ensure_sms()            # rebinds tx/rx after the Pin() above
        tx_sm = self._tx
        needed = sample_count // 32
        sleep_tx = int(tx_us) + 1

        set_reg = PIO1_CTRL_SET
        sleep_us = time.sleep_us
        tx_mask = SM_TX_MASK
        prime = self._prime_reset

        out = []
        best_d = None
        best_run = 0
        best_at = 0
        for d in range(int(d_from), int(d_to), int(d_step)):
            run_max = 0
            at_max = 0
            for _ in range(int(trials)):
                while cap_sm.rx_fifo():
                    cap_sm.get()
                cap_sm.active(0)
                cap_sm.restart()
                cap_sm.active(1)
                cap_sm.put(sample_count)
                tx_sm.active(0)
                tx_sm.restart()
                # bdc_tx_byte now autopushes one sampled byte per
                # transmitted byte; leave those in the RX FIFO and it fills
                # after four bytes and stalls the SM mid-burst.
                while tx_sm.rx_fifo():
                    tx_sm.get()
                for w in self._pack(tx):
                    tx_sm.put(w)
                time.sleep_us(30)      # let the capture SM reach its 'wait'
                prime()
                if d > 0:
                    sleep_us(int(d))
                mem32[set_reg] = tx_mask
                sleep_us(sleep_tx)
                words = []
                deadline = time.ticks_add(time.ticks_ms(), 50)
                while len(words) < needed:
                    if cap_sm.rx_fifo():
                        words.append(cap_sm.get())
                    elif time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                        break
                tx_sm.active(0)
                if len(words) < needed:
                    continue
                # longest run of zeros at or after `skip`
                run = 0
                i = skip
                n = len(words) * 32
                while i < n:
                    if not ((words[i >> 5] >> (31 - (i & 31))) & 1):
                        run += 1
                        if run > run_max:
                            run_max = run
                            at_max = i - run + 1
                    else:
                        run = 0
                    i += 1
            out.append(run_max)
            if run_max > best_run:
                best_run = run_max
                best_d = d
                best_at = at_max
        cap_sm.active(0)
        return {"d_from": int(d_from), "d_step": int(d_step),
                "sample_period_ns": int(ns), "skip_sample": skip,
                "tx_us": round(tx_us, 2),
                "max_low_samples": out,
                "best_delay_us": best_d,
                "best_low_us": round(best_run * sample_us, 3),
                "best_at_us": round(best_at * sample_us, 3)}

    def scan_wave(self, words, n_cycles, d_from=0, d_to=44, d_step=1,
                  trials=4, sample_count=128, window_us=20.0, skip_us=0.0):
        """Like scan_xfer, but the stimulus is an ARBITRARY cycle-by-cycle
        waveform instead of `bdc_tx_byte`'s hard-coded 3/13/16 bit shape.

        `words` is up to 8 32-bit words of `pindirs` bits, MSB first, one bit
        per BDC clock cycle (1 = drive BKGD low, 0 = release to the pull-up).
        `n_cycles` is how many of those bits are real, and is only used to
        size the "wait for our own transmission to finish" sleep.

        The point is to make the ratio between the bit period and the '1'/'0'
        low widths sweepable from the host. Everything else — phase locking to
        the target's reset loop, the single-store burst start, the
        longest-low-after-skip detector — is identical to scan_xfer, so the
        two are directly comparable and scan_xfer's positive controls still
        apply.

        NOTE: this plays the waveform from PIO block 0 (SM_RAW), not block 1,
        so it takes the BKGD pin's function select away from the cached tx/rx
        state machines. `_pio_bound` is cleared accordingly.
        """
        if self.sm_freq is None:
            raise BdcError("call sync() or set_bit_clock() first")
        words = [int(w) & 0xFFFFFFFF for w in words]
        if not words or len(words) > 8:
            raise BdcError("scan_wave: 1..8 words (8 = 256 BDC cycles)")
        n_cycles = int(n_cycles)
        sample_count = int(sample_count)
        if sample_count <= 0 or sample_count % 32 != 0:
            raise BdcError("sample_count must be a positive multiple of 32")

        cap_freq = int(sample_count * CYCLES_PER_SAMPLE * 1e6 / window_us)
        cap_freq = int(min(max(cap_freq, SM_MIN_FREQ_HZ), SM_MAX_FREQ_HZ))
        ns = 1e9 * CYCLES_PER_SAMPLE / cap_freq
        sample_us = ns / 1000.0

        tx_us = 1e6 * n_cycles / self.sm_freq
        skip = int(float(skip_us) / sample_us) + 1
        if skip >= sample_count:
            raise BdcError("scan_wave: window_us too short for this skip_us")

        cap_sm = make_capture_state_machine(self.bkgd_pin_num, cap_freq)
        raw_sm = make_raw_state_machine(self.bkgd_pin_num, self.sm_freq)
        self._pio_bound = False       # PIO0 now owns the pin

        needed = sample_count // 32
        sleep_tx = int(tx_us) + 1
        set_reg = PIO0_CTRL_SET
        raw_mask = SM_RAW_MASK
        sleep_us = time.sleep_us
        prime = self._prime_reset

        out = []
        best_d = None
        best_run = 0
        best_at = 0
        for d in range(int(d_from), int(d_to), int(d_step)):
            run_max = 0
            at_max = 0
            for _ in range(int(trials)):
                while cap_sm.rx_fifo():
                    cap_sm.get()
                cap_sm.active(0)
                cap_sm.restart()
                cap_sm.active(1)
                cap_sm.put(sample_count)
                raw_sm.active(0)
                raw_sm.restart()
                for w in words:
                    raw_sm.put(w)
                time.sleep_us(30)      # let the capture SM reach its 'wait'
                prime()
                if d > 0:
                    sleep_us(int(d))
                mem32[set_reg] = raw_mask
                sleep_us(sleep_tx)
                words_in = []
                deadline = time.ticks_add(time.ticks_ms(), 50)
                while len(words_in) < needed:
                    if cap_sm.rx_fifo():
                        words_in.append(cap_sm.get())
                    elif time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                        break
                raw_sm.active(0)
                if len(words_in) < needed:
                    continue
                run = 0
                i = skip
                n = len(words_in) * 32
                while i < n:
                    if not ((words_in[i >> 5] >> (31 - (i & 31))) & 1):
                        run += 1
                        if run > run_max:
                            run_max = run
                            at_max = i - run + 1
                    else:
                        run = 0
                    i += 1
            out.append(run_max)
            if run_max > best_run:
                best_run = run_max
                best_d = d
                best_at = at_max
        cap_sm.active(0)
        raw_sm.active(0)
        return {"d_from": int(d_from), "d_step": int(d_step),
                "sample_period_ns": int(ns), "skip_sample": skip,
                "tx_us": round(tx_us, 2),
                "max_low_samples": out,
                "best_delay_us": best_d,
                "best_low_us": round(best_run * sample_us, 3),
                "best_at_us": round(best_at * sample_us, 3)}

    def capture_wave(self, words, n_cycles, sample_count=128, window_us=8.0):
        """One-shot capture of a scan_wave stimulus, no reset priming — for
        checking that a host-built waveform really looks the way it was
        meant to before trusting a scan built on it."""
        if self.sm_freq is None:
            raise BdcError("call sync() or set_bit_clock() first")
        words = [int(w) & 0xFFFFFFFF for w in words]
        sample_count = int(sample_count)
        cap_freq = int(sample_count * CYCLES_PER_SAMPLE * 1e6 / window_us)
        cap_freq = int(min(max(cap_freq, SM_MIN_FREQ_HZ), SM_MAX_FREQ_HZ))
        cap_sm = make_capture_state_machine(self.bkgd_pin_num, cap_freq)
        raw_sm = make_raw_state_machine(self.bkgd_pin_num, self.sm_freq)
        self._pio_bound = False
        cap_sm.active(1)
        cap_sm.put(sample_count)
        raw_sm.active(0)
        raw_sm.restart()
        for w in words:
            raw_sm.put(w)
        time.sleep_us(30)
        mem32[PIO0_CTRL_SET] = SM_RAW_MASK
        time.sleep_us(int(1e6 * int(n_cycles) / self.sm_freq) + 2)
        got = []
        needed = sample_count // 32
        deadline = time.ticks_add(time.ticks_ms(), 200)
        while len(got) < needed:
            if cap_sm.rx_fifo():
                got.append(cap_sm.get())
            elif time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                break
        cap_sm.active(0)
        raw_sm.active(0)
        bits = []
        for w in got:
            for i in range(31, -1, -1):
                bits.append((w >> i) & 1)
        return {"samples": bits,
                "sample_period_ns": int(1e9 * CYCLES_PER_SAMPLE / cap_freq)}

    def capture2_xfer(self, tx, nbits=0, sample_count=256, window_us=200,
                      drive="xfer", host_low_us=None, trigger="bkgd",
                      bdm_opts=None):
        """Two-pin capture (RESET on bit0, BKGD on bit1) across a whole
        transaction. See bdc_pio.bdc_sample2 for why this matters.

        `drive` selects the stimulus:
            "xfer"  raw_xfer(tx, nbits)              (default)
            "sync"  one SYNC handshake — the one stimulus whose host-side
                    behaviour is exactly known and long enough to see the
                    target's whole reaction
            "none"  no stimulus at all: pure passive observation. Use with
                    trigger="reset" to watch the target's own reset train
                    with the reset edge as the time origin.
            "reset" a plain RESET pulse
            "bdm"   hardware_reset_to_bdm(**bdm_opts)

        `trigger` selects the falling edge that starts the capture:
        "bkgd" (default) or "reset".

        *** DEPTH LIMIT — read this before believing a trace. ***
        This packs 16 samples per 32-bit word into a 4-word RX FIFO, so only
        the first **64 samples** are guaranteed contiguous. Beyond that the
        PIO stalls on autopush until the Python drain loop below starts,
        which (for a stimulus that blocks, e.g. a SYNC that times out after
        10 ms) can be milliseconds later. The stalled-then-resumed tail looks
        perfectly repeatable and is pure artifact. Either ask for 64 samples,
        or use a sample period slow enough (>= ~5 us, i.e. >= 80 us per word)
        that Python keeps up — which is only possible when the stimulus
        does not block, i.e. drive="none".
        """
        if self.sm_freq is None and drive in ("xfer",):
            raise BdcError("call sync() before capture2_xfer()")
        sample_count = int(sample_count)
        if sample_count <= 0 or sample_count % 16 != 0:
            raise BdcError("sample_count must be a positive multiple of 16")
        cap_freq = int(sample_count * CYCLES_PER_SAMPLE * 1e6 / window_us)
        cap_freq = int(min(max(cap_freq, SM_MIN_FREQ_HZ), SM_MAX_FREQ_HZ))

        # For the phase-locked stimulus the SYNC state machine has to exist
        # and be loaded BEFORE the reset — constructing a StateMachine costs
        # several hundred microseconds, which is many whole reset cycles, so
        # building it after the delay (as _sync_drive does) destroys the very
        # phase lock the mode exists to provide.
        pre_sm = None
        if drive == "areset_sync":
            o = bdm_opts or {}
            probe_freq, window_s, _ = SYNC_ATTEMPTS[0]
            pre_low = max(1, int(probe_freq * float(o.get("host_low_us", 20))
                                 / 1e6))
            pre_iters = max(1, int(probe_freq * window_s / 2))
            pre_sm = make_state_machine(bdc_sync, self.bkgd_pin_num,
                                        probe_freq, SM_SYNC)
            self._pio_bound = False
            pre_sm.active(0)
            pre_sm.restart()
            pre_sm.put(pre_low)
            pre_sm.put(pre_iters)
            pre_sm.put(pre_iters)

        base = min(self.bkgd_pin_num, self.reset_pin_num)
        trig = self.reset_pin_num if trigger == "reset" else self.bkgd_pin_num
        cap_sm = make_capture2_state_machine(base, cap_freq, trig)
        self._pio_bound = False
        cap_sm.active(1)
        cap_sm.put(sample_count)
        time.sleep_us(50)

        result = {}
        try:
            if drive == "sync":
                result["sync_raw"] = self._sync_drive(
                    None if host_low_us is None else host_low_us / 1e6
                )
            elif drive == "areset_sync":
                # Phase-locked SYNC, but with RESET watched at the same time,
                # so the target's own reset edges can be located relative to
                # a response we know is there (Finding 31). bdm_opts carries
                # {"delay_us": N, "host_low_us": M}.
                dly = int((bdm_opts or {}).get("delay_us", 64))
                self._prime_reset()
                if dly > 0:
                    time.sleep_us(dly)
                pre_sm.active(1)
                result["sync_raw"] = pre_sm.get() & 0xFFFFFFFF
                pre_sm.active(0)
            elif drive == "none":
                pass
            elif drive == "reset":
                self.reset_target()
            elif drive == "bdm":
                self.hardware_reset_to_bdm(**(bdm_opts or {}))
            else:
                result["xfer"] = self.raw_xfer(tx, nbits)
        except BdcError as e:
            result["error"] = str(e)

        words = []
        needed = sample_count // 16
        deadline = time.ticks_add(time.ticks_ms(), 1000)
        while len(words) < needed:
            if cap_sm.rx_fifo():
                words.append(cap_sm.get())
            elif time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                break
        cap_sm.active(0)

        reset_bits = []
        bkgd_bits = []
        for w in words:
            for i in range(15, -1, -1):
                pair = (w >> (2 * i)) & 3
                reset_bits.append(pair & 1)
                bkgd_bits.append((pair >> 1) & 1)
        out = {
            "reset": reset_bits,
            "bkgd": bkgd_bits,
            "sample_period_ns": int(1e9 * CYCLES_PER_SAMPLE / cap_freq),
        }
        if "xfer" in result:
            v, tx_us, gap_us, rx_us = result["xfer"]
            out["value"] = v
        if "sync_raw" in result:
            out["sync_raw"] = result["sync_raw"]
        if "error" in result:
            out["xfer_error"] = result["error"]
        return out

    def capture_xfer(self, tx, nbits=0, sample_count=256, window_us=None):
        """Capture a WHOLE transaction — command bytes and the read phase —
        rather than one isolated phase.

        capture_command() only ever showed the outgoing bytes and
        capture_read_bit() only ever showed a read phase with no command in
        front of it, so neither could show whether the target answers a real
        command. This one can.
        """
        if self.sm_freq is None:
            raise BdcError("call sync() before capture_xfer()")
        result = {}

        def drive():
            try:
                result["xfer"] = self.raw_xfer(tx, nbits)
            except BdcError as e:
                result["error"] = str(e)

        cap = self._capture(sample_count, drive, window_us)
        if "xfer" in result:
            v, tx_us, gap_us, rx_us = result["xfer"]
            cap["value"] = v
            cap["tx_us"] = tx_us
            cap["gap_us"] = gap_us
            cap["rx_us"] = rx_us
        if "error" in result:
            cap["xfer_error"] = result["error"]
        return cap

    # -- BDC commands ---------------------------------------------------
    def read_byte(self, addr):
        """Read one byte of target memory. Returns the byte.

        Two bursts, and the second one is the point.

        `READ_BYTE` is coded `E0/AAAA/d/RD` where, verbatim from Table 17-1's
        key, "d = delay 16 target BDC clock cycles" — the target's memory
        cycle. A host that skips `d` does NOT get an error; it gets whatever
        is still sitting in the BDC shift register, which is the PREVIOUS
        read's data. That is exactly what this driver was doing, and the
        lag-by-one it produced is documented in CONTEXT.md Finding 83.

        There is no clean way to put a 16-cycle *idle* gap inside a burst
        whose every bit slot is a bit — but the BDC has a command that does
        not need one. `READ_LAST` is coded `E8/SS/RD`: no `d`, because the
        8-bit-time status byte IS the delay. So: issue the read, then fetch
        it with READ_LAST. Both bursts preload whole into the FIFO, so both
        are gapless, and the status byte comes free.
        """
        self._xfer((CMD_READ_BYTE, (addr >> 8) & 0xFF, addr & 0xFF), 1)
        status, value = self.read_last()
        if status & SCR_DVF:
            raise BdcError(
                "read of $%04X: target reports DVF (data valid failure) "
                "in BDCSCR = 0x%02X" % (addr, status)
            )
        return value

    def read_last(self):
        """`READ_LAST` (E8/SS/RD): re-read the byte at the address just
        read, and report BDCSCR with it. Returns (status, value)."""
        words = self._xfer((CMD_READ_LAST,), 2)
        return words[1], words[2]

    def read_byte_raw(self, addr, delay=1):
        """READ_BYTE with the data phase taken straight out of the same
        burst, `delay` marker byte-times after the address.

        Kept because it is the experiment, not the product: it is the shape
        that shows the lag-by-one, and it is how a future session can check
        whether a real target ever answers in the first marker slot.
        """
        return self._xfer_read(
            (CMD_READ_BYTE, (addr >> 8) & 0xFF, addr & 0xFF), 1, delay=delay
        )

    def write_byte(self, addr, value):
        self._tx_bytes(
            CMD_WRITE_BYTE, (addr >> 8) & 0xFF, addr & 0xFF, value & 0xFF
        )

    def read_status(self):
        # E4/SS -- no `d` in Table 17-1, so no delay byte.
        return self._xfer_read((CMD_READ_STATUS,), 1, delay=0)

    def background(self):
        self._write_byte_raw(CMD_BACKGROUND)

    def go(self):
        self._write_byte_raw(CMD_GO)

    def trace1(self):
        """Single-step one target instruction, per the BDC TRACE1 command."""
        self._write_byte_raw(CMD_TRACE1)

    def tagged_go(self):
        """Resume execution via the TAGGO command.

        IMPORTANT — this is NOT "run until the breakpoint address is hit"
        on HCS08. Per HCS08RMv1 §7.3.4.16, HCS08 parts have no external
        tag-input pin, so TAGGO behaves identically to a plain GO. It is
        kept only because the opcode exists and costs nothing.

        What actually stops the target at an address is the BDC breakpoint:
        write the address with write_bkpt(), which also arms BKPTEN in
        BDCSCR (the breakpoint does nothing at all unless BKPTEN is 1, and
        BDCSCR resets with BKPTEN=0), then resume with go(). Whether the
        halt is force-mode or tag-mode is the FTS bit, also set by
        write_bkpt().
        """
        self._write_byte_raw(CMD_TAGGO)

    def write_control(self, value, preserve_clksw=True):
        """Write the BDC status/control register (BDCSCR). Same register
        read_status() reads.

        *** CLEARING CLKSW KILLS THE LINK. Measured, repeatedly. ***
        CLKSW selects the BDC clock source, and sync() calibrated this
        driver's bit rate against whichever source was selected at the
        time. Flip it and every subsequent transfer decodes to the
        pull-up's 0xFF -- no error, no warning, just a target that has
        apparently gone dead. It is recoverable (a power-on re-entry
        brings it straight back) but it cost this project most of a
        session to recognise. CONTEXT.md Finding 87.

        The resting BDCSCR of this part in state S is 0xC8, i.e. CLKSW is
        SET, so the natural-looking `write_control(SCR_ENBDM)` is exactly
        the call that breaks it.

        So by default the CURRENT CLKSW is read back and OR-ed into
        `value`. Pass preserve_clksw=False to write the byte verbatim --
        and re-run sync() immediately afterwards if you do.
        """
        value &= 0xFF
        if preserve_clksw:
            value = (value & ~SCR_CLKSW) | (self.read_status() & SCR_CLKSW)
        self._tx_bytes(CMD_WRITE_CONTROL, value)
        return value

    def update_control(self, set_bits=0, clear_bits=0):
        """Read-modify-write BDCSCR, touching only the host-writable control
        bits (ENBDM/BKPTEN/FTS/CLKSW) and preserving the rest.

        ENBDM is always forced on: dropping it would leave background mode
        mid-operation, and turning it on when it's already on costs nothing.

        CLKSW is deliberately PRESERVED rather than forced: sync() measured
        the response pulse produced by whatever BDC clock source is
        currently selected, so flipping CLKSW here would invalidate the bit
        timing calibration without anything noticing.
        """
        current = self.read_status()
        value = (current & SCR_WRITABLE_MASK & ~clear_bits) | set_bits | SCR_ENBDM
        self.write_control(value)
        return value

    def read_bkpt(self):
        # E2/RBKP -- no `d` in Table 17-1.
        return self._xfer_read((CMD_READ_BKPT,), 2, delay=0)

    def write_bkpt(self, addr, arm=True, tag_mode=False):
        """Set the BDC hardware breakpoint address.

        By default this ALSO arms it: BDCSCR.BKPTEN must be 1 for the BKPT
        register to have any effect, and BDCSCR resets with BKPTEN=0 — so
        before this fix, setting a breakpoint and resuming was a complete
        no-op on real silicon.

        tag_mode=False (the default) selects force mode (FTS=0): the target
        halts when the breakpoint address is reached. tag_mode=True selects
        tag mode (FTS=1), which halts when the tagged opcode actually
        executes. Force mode is the simpler, more predictable default.

        ENBDM and CLKSW are preserved by read-modify-write; a bare
        overwrite here would drop out of BDM and/or break the calibrated
        bit timing.
        """
        self._tx_bytes(CMD_WRITE_BKPT, (addr >> 8) & 0xFF, addr & 0xFF)
        if arm:
            if tag_mode:
                self.update_control(set_bits=SCR_BKPTEN | SCR_FTS)
            else:
                self.update_control(set_bits=SCR_BKPTEN, clear_bits=SCR_FTS)

    # -- CPU register access ---------------------------------------------
    # (opcode, is_word) per register name -- PC/HX/SP are 16-bit, A/CCR 8-bit
    _CPU_REGS = {
        "A":   (CMD_READ_A,  CMD_WRITE_A,  False),
        "CCR": (CMD_READ_CCR, CMD_WRITE_CCR, False),
        "PC":  (CMD_READ_PC, CMD_WRITE_PC, True),
        "HX":  (CMD_READ_HX, CMD_WRITE_HX, True),
        "SP":  (CMD_READ_SP, CMD_WRITE_SP, True),
    }

    def read_reg(self, name, delay=0, double=True):
        """Read a CPU register. *** THESE WORK. Finding 93 was wrong. ***

        Two things had to be right at once, and the ELEVENTH session found
        both (CONTEXT.md Finding 103):

        1. `delay=0`, not 1. Table 17-1 writes these as `68/d/RD`, but on
           this part the answer is in the FIRST marker slot, exactly like
           READ_STATUS. Sampling the second slot returns the pull-up, which
           is what made every register look dead: A/CCR/HX/PC/SP all read
           0xFF/0xFFFF and that was recorded as "active-BDM commands are
           silent on this part".

        2. Issue the read TWICE and keep the second answer. A and the H
           half of H:X are loaded into the BDC shift register as the
           command COMPLETES, so the first read clocks out the previous
           transaction's leftovers and the second clocks out the real
           value. Measured: write A=5A,A5,3C,00,F0 and the first read
           returns the previous write every time while the second returns
           the current one. CCR, PC, SP and the X half are already correct
           on the first read and are unaffected by reading again (they are
           pure reads with no side effects), so this is done unconditionally
           rather than per-register.

        Pass double=False / delay=1 to reproduce the old behaviour.
        """
        try:
            read_op, _, is_word = self._CPU_REGS[name]
        except KeyError:
            raise BdcError("unknown register: %r" % name)
        nread = 2 if is_word else 1
        if double:
            self._xfer_read((read_op,), nread, delay=delay)
        return self._xfer_read((read_op,), nread, delay=delay)

    def write_reg(self, name, value):
        try:
            _, write_op, is_word = self._CPU_REGS[name]
        except KeyError:
            raise BdcError("unknown register: %r" % name)
        if is_word:
            self._tx_bytes(write_op, (value >> 8) & 0xFF, value & 0xFF)
        else:
            self._tx_bytes(write_op, value & 0xFF)

    def read_block(self, addr, length):
        return bytes(self.read_byte(addr + i) for i in range(length))

    def write_block(self, addr, data):
        for i, b in enumerate(data):
            self.write_byte(addr + i, b)

    # -- FLASH programming, following the SG8/SG4 datasheet §4.5.3 flow ----
    def flash_clear_errors(self):
        """Write-1-to-clear FACCERR/FPVIOL in FSTAT.

        Datasheet §4.5.5: FACCERR must be cleared before any FLASH command
        can be processed, and FCDIV cannot be written while it is set. This
        used to never happen anywhere, so a single access error wedged the
        FLASH interface until the target was power-cycled.
        """
        self.write_byte(REG_FSTAT, FSTAT_ERR_CLEAR)

    def flash_unprotect(self):
        """Clear FLASH protection so erase/program can proceed.

        Per datasheet §4.5.6 the protection registers are writable through
        the BDC while the part is in active background mode; without this,
        erase/program on a protected part fails with FPVIOL. See the
        confidence note on FPROT_UNPROTECT_ALL.
        """
        self.write_byte(REG_FPROT, FPROT_UNPROTECT_ALL)

    def flash_init_clock(self, bus_freq_hz):
        """
        Set up FCDIV so the internal FLASH clock lands in the required
        150-200 kHz window (datasheet §4.7.1). Only needs doing once per
        power-up.

            fFCLK = fBus / (DIV+1)        if PRDIV8 = 0
            fFCLK = fBus / (8*(DIV+1))    if PRDIV8 = 1

        There are only 128 legal (PRDIV8, DIV) combinations, so rather than
        computing a divisor and hoping, this enumerates all of them, keeps
        the ones whose fFCLK lands inside the legal window, and picks the
        one closest to the MIDDLE of that window. That matters because the
        old code aimed at exactly 200 kHz -- the spec maximum, zero margin
        -- and used round(), which could round the divisor DOWN and push
        fFCLK over the limit.

        If nothing lands inside the window for this bus clock, this raises
        rather than silently clamping DIV: clamping can leave fFCLK further
        out of spec than failing would.
        """
        bus_freq_hz = int(bus_freq_hz)
        if bus_freq_hz <= 0:
            raise BdcError("bus_freq_hz must be positive")

        # FACCERR blocks the FCDIV write entirely -- clear it first.
        self.flash_clear_errors()

        best = None  # (error_from_target, prdiv8, div, fclk)
        closest = None  # best-effort, for the error message only
        for prdiv8 in (0, 1):
            prescale = 8 if prdiv8 else 1
            for div in range(64):
                fclk = bus_freq_hz / (prescale * (div + 1))
                err = abs(fclk - FCLK_TARGET_HZ)
                if closest is None or err < closest[0]:
                    closest = (err, prdiv8, div, fclk)
                if FCLK_MIN_HZ <= fclk <= FCLK_MAX_HZ:
                    if best is None or err < best[0]:
                        best = (err, prdiv8, div, fclk)

        if best is None:
            raise BdcError(
                "no FCDIV setting puts fFCLK in the legal %d-%d Hz window "
                "for a %d Hz bus clock (closest achievable: %.0f Hz) -- "
                "check the bus clock value you entered"
                % (FCLK_MIN_HZ, FCLK_MAX_HZ, bus_freq_hz, closest[3])
            )

        _, prdiv8, div, fclk = best
        fcdiv_val = (prdiv8 << 6) | div
        self.write_byte(REG_FCDIV, fcdiv_val)

        # sanity check: DIVLD bit (bit7 of FCDIV) should now read 1
        readback = self.read_byte(REG_FCDIV)
        if not (readback & 0x80):
            raise BdcError("FCDIV write didn't latch (DIVLD not set)")

    def _flash_wait_command(self, timeout_ms=200):
        start = time.ticks_ms()
        while True:
            stat = self.read_byte(REG_FSTAT)
            if stat & FSTAT_FACCERR:
                # Clear it on the way out, or the FLASH interface stays
                # wedged for every subsequent command too.
                self.flash_clear_errors()
                raise BdcError(
                    "FLASH access error (FACCERR set), FSTAT=0x%02X" % stat
                )
            if stat & FSTAT_FPVIOL:
                self.flash_clear_errors()
                raise BdcError(
                    "FLASH protection violation (FPVIOL set), FSTAT=0x%02X "
                    "-- try flash_unprotect() first" % stat
                )
            if stat & FSTAT_FCCF:
                return stat
            if time.ticks_diff(time.ticks_ms(), start) > timeout_ms:
                raise BdcError("timeout waiting for FCCF")

    def _flash_command(self, addr, data_byte, command):
        # Step 0: clear any latched FACCERR/FPVIOL. Per datasheet §4.5.5 a
        # set FACCERR blocks the whole command sequence, so without this a
        # single earlier error makes every later command fail forever.
        self.flash_clear_errors()
        # Step 1: write to the target flash address (latches addr+data)
        self.write_byte(addr, data_byte)
        # Step 2: write command code to FCMD
        self.write_byte(REG_FCMD, command)
        # Step 3: write 1 to FCBEF in FSTAT to launch the command
        self.write_byte(REG_FSTAT, FSTAT_FCBEF)
        self._flash_wait_command()

    def flash_erase_page(self, addr_in_page):
        self.flash_unprotect()
        self._flash_command(addr_in_page, 0xFF, FCMD_PAGE_ERASE)

    def flash_mass_erase(self):
        self.flash_unprotect()
        self._flash_command(0xFFFF, 0xFF, FCMD_MASS_ERASE)

    def flash_blank_check(self):
        """Run the FLASH module's own blank-check command and return True if
        the array reports erased (FSTAT.FBLANK set). Much faster than
        reading back and comparing every byte in Python."""
        self._flash_command(0xFFFF, 0xFF, FCMD_BLANK_CHECK)
        stat = self.read_byte(REG_FSTAT)
        return bool(stat & FSTAT_FBLANK)

    def flash_program_byte(self, addr, value):
        self._flash_command(addr, value, FCMD_BYTE_PROGRAM)

    def flash_write_region(self, start_addr, data, erase_pages=True):
        """
        Program `data` (bytes) starting at `start_addr`, then read it back
        and verify. Does NOT touch FCDIV -- call flash_init_clock() first.

        erase_pages=True erases every 512-byte page this region touches
        before programming.

        *** CALLER BEWARE (the C3 hazard) ***
        Erasing is per-REGION, and a page erase wipes the whole 512-byte
        page — including bytes belonging to a *different* region that were
        programmed earlier. Real toolchain output routinely puts two
        non-contiguous S-record runs inside one page. So when writing more
        than one region, the caller must compute the union of pages across
        ALL regions, erase those once up front, and then call this with
        erase_pages=False for every region. host/app.py's /api/flash_srec
        does exactly that; don't reintroduce per-region erasing there.
        """
        self.flash_unprotect()

        if erase_pages:
            first_page = start_addr - (start_addr % PAGE_SIZE)
            last_addr = start_addr + len(data) - 1
            last_page = last_addr - (last_addr % PAGE_SIZE)
            for page_addr in range(first_page, last_page + 1, PAGE_SIZE):
                self.flash_erase_page(page_addr)

        for i, b in enumerate(data):
            self.flash_program_byte(start_addr + i, b)

        # verify
        readback = self.read_block(start_addr, len(data))
        if bytes(readback) != bytes(data):
            raise BdcError("verification failed after flash_write_region")

        return len(data)

    # -- page-at-a-time programming, security, and state reporting ---------
    #
    # Everything below runs ENTIRELY ON THE PICO on purpose. A host-driven
    # byte over the JSON protocol costs ~17 ms of USB round trip (Finding
    # 90/94), so a 195-byte image driven from Python would be ~30 s of pure
    # latency and a 12-register status poll would take a fifth of a second.
    # On the Pico a BDC byte is ~3 ms, so these are one round trip each.

    def _security_from_fopt(self, fopt):
        sec = fopt & SEC_MASK
        return {
            "fopt": fopt,
            "sec": sec,
            "secured": sec != SEC_UNSECURED,
            "keyen": bool(fopt & 0x80),
            "fnored": bool(fopt & 0x40),
            # 0xFF is this project's signature for "the target said nothing
            # and we read the pull-up". It also decodes as SEC = 1:1, so a
            # dead link and a secured part look identical here unless the
            # caller is told to be suspicious.
            "suspect_link": fopt == 0xFF,
        }

    def security_state(self):
        """Current security state, from FOPT ($1821) with NVOPT ($FFBF) as
        a cross-check.

        FOPT is a peripheral register and answers even on a secured part.
        NVOPT is FLASH, and on a secured part every FLASH read returns 0x00
        (Finding 95) -- so `nvopt` is only meaningful when `secured` is
        False, and `nvopt_trustworthy` says so explicitly instead of
        handing back a 0x00 that looks like data.
        """
        st = self._security_from_fopt(self.read_byte(REG_FOPT))
        st["nvopt"] = self.read_byte(ADDR_NVOPT)
        st["nvopt_trustworthy"] = not st["secured"]
        return st

    def set_security(self, nvopt_value=NVOPT_SECURED, bus_freq_hz=None):
        """Program NVOPT ($FFBF) to lock the part at the next reset.

        Refuses any value that would need a 1 -> 0 -> 1 transition, because
        FLASH programming cannot set a bit: that would require erasing page
        $FE00, which is the Finding 95 hazard. From the usual unsecured
        0xFE, the reachable secured value is 0xFC (SEC = 0:0).

        Security is latched AT RESET, so this does not lock the current
        power cycle -- the caller still has the link until the next
        power-on.
        """
        if bus_freq_hz:
            self.flash_init_clock(bus_freq_hz)
        self.flash_unprotect()
        before = self.read_byte(ADDR_NVOPT)
        nvopt_value &= 0xFF
        if (before & nvopt_value) != nvopt_value:
            raise BdcError(
                "cannot program $FFBF from 0x%02X to 0x%02X: FLASH "
                "programming only clears bits (1->0). Erasing page $FE00 "
                "would be needed, which blanks NVOPT -- see Finding 95."
                % (before, nvopt_value)
            )
        self.flash_program_byte(ADDR_NVOPT, nvopt_value)
        after = self.read_byte(ADDR_NVOPT)
        st = self._security_from_fopt(self.read_byte(REG_FOPT))
        return {
            "before": before,
            "wrote": nvopt_value,
            "after": after,
            "written": after == nvopt_value,
            # FOPT still shows the state latched at the last reset; the new
            # NVOPT only takes effect at the next one.
            "fopt_now": st["fopt"],
            "secured_now": st["secured"],
            "takes_effect": "at the next reset / power cycle",
        }

    def flash_unsecure(self, bus_freq_hz=None, restore_nvopt=NVOPT_UNSECURED,
                       probe_addr=0xE000):
        """Full wipe + unsecure, the Finding 96 recipe, step by step.

        *** MASS ERASE ALONE DOES NOT UNSECURE AN HCS08. *** It is the
        blank check (erase-verify, FCMD 0x05) that releases security --
        measured, on a really-secured part, in the TENTH session. This runs
        both and reports every step separately so a failure is attributable
        rather than a black box.

        Returns a report dict; it does NOT raise, so the caller always gets
        the steps that did complete.
        """
        rep = {"steps": [], "secured_before": None, "secured_after": None,
               "complete": False, "error": None, "erased": False}

        def add(name, ok, **detail):
            entry = {"step": name, "ok": bool(ok)}
            entry.update(detail)
            rep["steps"].append(entry)
            return entry

        try:
            before = self.security_state()
            rep["secured_before"] = before["secured"]
            add("read_security", True, fopt=before["fopt"], sec=before["sec"],
                secured=before["secured"], suspect_link=before["suspect_link"])

            if bus_freq_hz:
                self.flash_init_clock(bus_freq_hz)
                add("flash_init_clock", True, bus_freq_hz=int(bus_freq_hz),
                    fcdiv=self.read_byte(REG_FCDIV))

            self.flash_mass_erase()
            rep["erased"] = True
            add("mass_erase", True, fstat=self.read_byte(REG_FSTAT),
                fopt=self.read_byte(REG_FOPT))

            blank = self.flash_blank_check()
            st = self.security_state()
            add("blank_check", blank, blank=blank,
                fstat=self.read_byte(REG_FSTAT), fopt=st["fopt"],
                secured=st["secured"])
            if not blank:
                raise BdcError(
                    "blank check reported NOT blank after a mass erase -- "
                    "the erase did not take"
                )

            probe = self.read_byte(probe_addr)
            add("flash_readable", probe == 0xFF, addr=probe_addr, value=probe,
                note="a secured part reads 0x00 everywhere in FLASH")
            if probe != 0xFF:
                raise BdcError(
                    "FLASH at $%04X reads 0x%02X after erase+blank check; "
                    "expected 0xFF on an erased, unsecured part"
                    % (probe_addr, probe)
                )

            st = self.security_state()
            add("security_released", not st["secured"], fopt=st["fopt"],
                sec=st["sec"], secured=st["secured"])

            if restore_nvopt is not None:
                # The part is unsecured RIGHT NOW but NVOPT is blank (0xFF =
                # SEC 1:1), so it would re-secure itself at the next reset.
                # Closing that window is part of the operation, not an
                # optional extra.
                self.flash_program_byte(ADDR_NVOPT, restore_nvopt)
                rb = self.read_byte(ADDR_NVOPT)
                add("restore_nvopt", rb == restore_nvopt,
                    wrote=restore_nvopt, read=rb,
                    note="without this the part re-secures at the next reset")
                if rb != restore_nvopt:
                    raise BdcError(
                        "NVOPT restore did not verify: wrote 0x%02X, read "
                        "0x%02X -- THE PART WILL RE-SECURE AT THE NEXT RESET"
                        % (restore_nvopt, rb)
                    )

            final = self.security_state()
            rep["secured_after"] = final["secured"]
            rep["complete"] = not final["secured"]
        except Exception as e:                     # report, don't propagate
            rep["error"] = "%s: %s" % (type(e).__name__, e)
            try:
                final = self.security_state()
                rep["secured_after"] = final["secured"]
            except Exception:
                pass
        return rep

    def flash_program_image(self, chunks, bus_freq_hz=None, erase=True,
                            nvopt=NVOPT_UNSECURED, verify=True):
        """Program a whole image PAGE AT A TIME: erase -> program -> verify
        one page before touching the next.

        `chunks` is a list of (addr, bytes). They may overlap pages and be
        non-contiguous; this builds the page map itself, so the "chunk B's
        erase wipes chunk A" hazard that /api/flash_srec works around by
        pre-computing a page union cannot arise here at all.

        Why page at a time: a failure (or a power loss) then leaves a known
        state -- "pages 0..N-1 are erased, programmed and verified, page N
        failed at address X" -- instead of a half-written array nobody can
        characterise. This is the achievable form of power-loss protection
        on this hardware; there is no VDD sense wire to interrupt on.

        Page $FE00 gets special handling: erasing it blanks NVOPT at $FFBF
        and the part re-secures at the next reset (Finding 95). So NVOPT is
        reprogrammed IMMEDIATELY after that page's erase, before any other
        byte of it, and `security_risk` stays True for exactly that window.
        If the report comes back with security_risk True, the part is one
        reset away from locking itself.

        Returns a report dict; does NOT raise.
        """
        rep = {"pages": [], "bytes_written": 0, "complete": False,
               "error": None, "security_risk": False, "nvopt": None,
               "pages_total": 0}

        # page -> {addr: byte}
        pages = {}
        for addr, data in chunks:
            for i in range(len(data)):
                a = addr + i
                page = a - (a % PAGE_SIZE)
                cells = pages.get(page)
                if cells is None:
                    cells = pages[page] = {}
                cells[a] = data[i]
        order = sorted(pages)
        rep["pages_total"] = len(order)

        # If the image itself carries NVOPT, that value wins; otherwise the
        # caller's (default: keep the part unsecured).
        nvopt_value = pages.get(NVOPT_PAGE, {}).get(ADDR_NVOPT, nvopt)
        rep["nvopt"] = nvopt_value

        try:
            if bus_freq_hz:
                self.flash_init_clock(bus_freq_hz)
            self.flash_unprotect()

            for page in order:
                cells = pages[page]
                entry = {"page": page, "bytes": len(cells), "erased": False,
                         "programmed": 0, "verified": False}
                rep["pages"].append(entry)

                if erase:
                    if page == NVOPT_PAGE:
                        rep["security_risk"] = True
                    self.flash_erase_page(page)
                    entry["erased"] = True
                    if page == NVOPT_PAGE:
                        self.flash_program_byte(ADDR_NVOPT, nvopt_value)
                        rb = self.read_byte(ADDR_NVOPT)
                        entry["nvopt_restored"] = rb
                        if rb != nvopt_value:
                            raise BdcError(
                                "NVOPT restore after erasing page $FE00 did "
                                "not verify (wrote 0x%02X, read 0x%02X)"
                                % (nvopt_value, rb)
                            )
                        rep["security_risk"] = False

                for a in sorted(cells):
                    if erase and page == NVOPT_PAGE and a == ADDR_NVOPT:
                        # already programmed, above, as part of closing the
                        # hazard window -- and it cannot be programmed twice
                        continue
                    self.flash_program_byte(a, cells[a])
                    entry["programmed"] += 1

                if verify:
                    bad = []
                    for a in sorted(cells):
                        v = self.read_byte(a)
                        if v != cells[a]:
                            bad.append([a, cells[a], v])
                            if len(bad) >= 8:
                                break
                    if bad:
                        entry["mismatches"] = bad
                        raise BdcError(
                            "page $%04X failed verify: $%04X wrote 0x%02X "
                            "read 0x%02X (%d mismatch(es) shown)"
                            % (page, bad[0][0], bad[0][1], bad[0][2], len(bad))
                        )
                    entry["verified"] = True

                rep["bytes_written"] += len(cells)
            rep["complete"] = True
        except Exception as e:
            rep["error"] = "%s: %s" % (type(e).__name__, e)
        return rep

    # SDID -> what part this is. SDIDH[7:4] is the mask revision and
    # SDIDH[3:0]:SDIDL is a 12-bit part ID, which identifies the FAMILY,
    # not the exact derivative: the SG8 and the SG4 share one datasheet and
    # (measured, this session) one ID. Telling them apart needs a probe, so
    # `identify()` does one instead of guessing -- see Finding 92, where
    # working RAM at $0240 was the first positive identification of which of
    # the two chips was in the socket.
    _PART_IDS = {
        0x014: {
            "family": "MC9S08SG8 / MC9S08SG4 (HCS08 SG family)",
            "variants": ["MC9S08SG8", "MC9S08SG4"],
        },
    }

    def _ram_cell_ok(self, addr, patterns=(0x5A, 0xA5)):
        """Non-destructive 'does this RAM cell exist and hold data' test.

        Two different patterns, because one pattern cannot tell a real cell
        from a bus that happens to float to that value -- and this die has
        cells that hold SOME bits (the $0060-$006F defect), which a single
        0xFF write would misreport. The original byte is put back.
        """
        orig = self.read_byte(addr)
        ok = True
        for p in patterns:
            self.write_byte(addr, p)
            if self.read_byte(addr) != p:
                ok = False
                break
        self.write_byte(addr, orig)
        return ok

    def identify(self, ram_probe=True, defect_scan=False):
        """Identify the MCU: decode SDID, and (optionally) probe for the
        facts that SDID cannot give.

        `ram_probe` writes and restores ONE RAM byte ($0240). That is safe
        on a halted target and is how SG8 (512 B RAM) is told from SG4
        (256 B, no $0240). It does perturb RAM for a heartbeat, so it is a
        parameter, not a default of every status read.

        `defect_scan` walks $0060-$006F and reports the per-address AND mask
        this particular die imposes (Finding 92). Also restores every byte.
        """
        sdidh = self.read_byte(REG_SDIDH)
        sdidl = self.read_byte(REG_SDIDL)
        part_id = ((sdidh & 0x0F) << 8) | sdidl
        ent = self._PART_IDS.get(part_id)
        out = {
            "sdidh": sdidh, "sdidl": sdidl,
            "part_id": part_id, "part_id_hex": "0x%03X" % part_id,
            "rev": (sdidh >> 4) & 0x0F,
            "family": ent["family"] if ent else "unknown (SDID 0x%03X)" % part_id,
            "variants": ent["variants"] if ent else [],
            "name": None,
            "confidence": "id-only",
            "evidence": None,
            "flash_kb": None, "ram_bytes": None, "flash_start": None,
        }
        if sdidh == 0xFF and sdidl == 0xFF:
            out["family"] = "no answer -- the link read the idle pull-up"
            out["confidence"] = "none"
            return out
        if ent and len(ent["variants"]) == 1:
            out["name"] = ent["variants"][0]

        if ram_probe and part_id == 0x014:
            big = self._ram_cell_ok(0x0240)
            out["name"] = "MC9S08SG8" if big else "MC9S08SG4"
            out["confidence"] = "probed"
            out["ram_bytes"] = 512 if big else 256
            out["flash_kb"] = 8 if big else 4
            out["flash_start"] = 0xE000 if big else 0xF000
            out["evidence"] = (
                "RAM at $0240 holds written data, so this part has the SG8's "
                "512 B array" if big else
                "RAM at $0240 does not hold written data, so this is the "
                "SG4's 256 B array"
            )

        if defect_scan:
            bad = []
            for a in range(0x0060, 0x0070):
                orig = self.read_byte(a)
                self.write_byte(a, 0xFF)
                mask = self.read_byte(a)
                self.write_byte(a, orig)
                if mask != 0xFF:
                    bad.append({"addr": a, "mask": mask})
            out["bad_ram_cells"] = bad
            out["bad_ram_note"] = (
                "cells that do not return 0xFF after 0xFF is written: a "
                "fixed per-address AND mask, localised to this die "
                "(CONTEXT.md Finding 92). Avoid this range."
            )
        return out

    def chip_info(self, bus_freq_hz=None, blank_check=False):
        """Everything cheap and already-proven-readable about the part in
        the socket, in ONE round trip. Meant to run on connect."""
        info = {}
        info["bdcscr"] = self.read_status()
        sdidh = self.read_byte(REG_SDIDH)
        sdidl = self.read_byte(REG_SDIDL)
        info["sdidh"] = sdidh
        info["sdidl"] = sdidl
        # SDIDH[3:0]:SDIDL = 12-bit part ID; SDIDH[7:4] = mask set revision.
        info["part_id"] = ((sdidh & 0x0F) << 8) | sdidl
        info["rev"] = (sdidh >> 4) & 0x0F
        info["id_ok"] = (sdidl == 0x14 and (sdidh & 0x0F) == 0x00)

        srs = self.read_byte(REG_SRS)
        info["srs"] = srs
        info["reset_source"] = {
            "por": bool(srs & 0x80), "pin": bool(srs & 0x40),
            "cop": bool(srs & 0x20), "ilop": bool(srs & 0x10),
            "ilad": bool(srs & 0x08), "lvd": bool(srs & 0x02),
        }
        info["sopt1"] = self.read_byte(REG_SOPT1)
        info["spmsc1"] = self.read_byte(REG_SPMSC1)
        info["fcdiv"] = self.read_byte(REG_FCDIV)
        info["fprot"] = self.read_byte(REG_FPROT)
        info["fstat"] = self.read_byte(REG_FSTAT)
        info["security"] = self.security_state()
        info["bdc_clock_hz"] = self.target_freq_hz
        # CLKSW = 1 selects the MCU bus clock as the BDC clock, so the rate
        # sync() measured IS fBus on this part (Finding 94).
        info["clksw"] = bool(info["bdcscr"] & SCR_CLKSW)
        info["bus_clock_hz"] = self.target_freq_hz if info["clksw"] else None

        if blank_check:
            try:
                if bus_freq_hz:
                    self.flash_init_clock(bus_freq_hz)
                info["blank"] = self.flash_blank_check()
            except Exception as e:
                info["blank"] = None
                info["blank_error"] = "%s: %s" % (type(e).__name__, e)
        return info

    def relink(self, validate_addr=REG_SDIDL, expect=0x14):
        """Re-establish the BDC link WITHOUT power-cycling the target.

        `/api/sync` enters BDM with a power-on reset, which is the only
        entry this part accepts (Finding 63) -- but it also resets the CPU,
        so it cannot be used to recover a link to a target that is RUNNING
        without destroying what it was doing. This does the two things that
        actually matter for a running target, from Findings 99 and 100:

          1. SYNC measures 2x high against a free-running target, so try
             the measured rate AND half of it, validating each by reading a
             register with a known value (SDIDL = 0x14).
          2. Memory reads are non-intrusive but still need BDM ENABLED:
             set ENBDM (preserving CLKSW, which must stay as sync()
             calibrated it) before believing a read.

        Nothing here halts the CPU. Returns what it settled on.
        """
        raw = self.sync()
        out = {"sync_raw_hz": raw, "bit_clock_hz": None, "validated": False,
               "status": None, "tried": []}
        for cand in (raw, raw / 2):
            try:
                self.set_bit_clock(cand)
                self.update_control(set_bits=SCR_ENBDM)
                self.set_bit_clock(cand)
                v = self.read_byte(validate_addr)
                st = self.read_status()
            except Exception as e:
                out["tried"].append({"hz": int(cand), "error": str(e)})
                continue
            out["tried"].append({"hz": int(cand), "value": v, "status": st})
            if v == expect:
                out["bit_clock_hz"] = int(cand)
                out["validated"] = True
                out["status"] = st
                return out
        return out

    def power_state(self):
        """What the programmer knows about target power — which is what it
        is COMMANDING, not what it is measuring.

        There is no voltage sense on this wiring and none can be added
        without a wire: target VDD is driven from GPIO12, and the RP2040's
        ADC only reaches GPIO26-29. So `can_sense_voltage` is False and says
        why, rather than the UI inventing a rail voltage. The honest in-band
        witness that VDD is actually present is that the target answers BDC
        commands at all — R1 (10k, BKGD -> VDD) idles BKGD high only while
        the target is powered.
        """
        return {
            "vdd_pin": self.power_pin_num,
            "vdd_driven_high": None if self.power is None else bool(self.power.value()),
            "bkgd_pin": self.bkgd_pin_num,
            "reset_pin": self.reset_pin_num,
            "can_sense_voltage": False,
            "reason": "target VDD is driven from GPIO%s, which is not an "
                      "ADC-capable pin (the RP2040 ADC reaches GPIO26-29 "
                      "only), and no sense wire exists"
                      % self.power_pin_num,
            "adc_capable_pins": [26, 27, 28, 29],
            "witness": "a target that answers BDC commands is powered; that "
                       "is the only in-band evidence available",
        }

    def live_state(self, cpu_regs=True, ports=True):
        """One-round-trip snapshot for a live-debug panel.

        Honesty rule for the CPU registers: they DO answer now (Finding
        103 -- delay=0 plus a double read), but the all-ones pattern is
        still exactly what a dead link returns, so a register reading
        0xFF/0xFFFF is flagged `ambiguous` with a reason rather than
        presented as a confident value. Anything else is real and is
        reported as such.
        """
        out = {"link_ok": True, "link_error": None}
        s = self.read_status()
        if s == 0xFF:
            # Every bit set is this project's signature for "nobody drove
            # the line". A running target can drift out of sync (Findings
            # 99/100), and a panel that then rendered 0xFF as register
            # contents would be inventing state. Say so and stop; relink()
            # is the non-destructive recovery.
            out["link_ok"] = False
            out["link_error"] = (
                "BDCSCR read back 0xFF -- the target is not answering at the "
                "current bit rate. If it is running, re-establish with "
                "relink() (SYNC can read 2x high against a free-running "
                "target, and ENBDM must be set); a halted target needs a "
                "power-on re-entry."
            )
            out["bdcscr"] = {"value": s}
            return out
        out["bdcscr"] = {
            "value": s,
            "enbdm": bool(s & SCR_ENBDM), "bdmact": bool(s & SCR_BDMACT),
            "bkpten": bool(s & SCR_BKPTEN), "fts": bool(s & SCR_FTS),
            "clksw": bool(s & SCR_CLKSW), "ws": bool(s & SCR_WS),
            "wsf": bool(s & SCR_WSF), "dvf": bool(s & SCR_DVF),
        }
        # BDMACT is the single fact a panel needs most: halted in background
        # mode (registers meaningful, FLASH commands allowed) vs executing
        # (memory reads still work, CPU registers do not).
        out["halted"] = bool(s & SCR_BDMACT)
        # From here on a read can legitimately fail mid-poll against a
        # RUNNING target (a DVF, or the rate drifting out from under us).
        # That is a link fact, not a crash: mark the snapshot and return
        # what was gathered, so a polling UI degrades instead of erroring.
        try:
            out["bkpt"] = {
                "addr": self.read_bkpt(),
                "enabled": bool(s & SCR_BKPTEN),
                "tag_mode": bool(s & SCR_FTS),
            }

            if ports:
                out["ports"] = {
                    "ptad": self.read_byte(REG_PTAD),
                    "ptadd": self.read_byte(REG_PTADD),
                    "ptbd": self.read_byte(REG_PTBD),
                    "ptbdd": self.read_byte(REG_PTBDD),
                    "ptcd": self.read_byte(REG_PTCD),
                    "ptcdd": self.read_byte(REG_PTCDD),
                }
        except Exception as e:
            out["link_ok"] = False
            out["link_error"] = "%s: %s" % (type(e).__name__, e)
            return out

        if cpu_regs:
            regs = {}
            halted = out["halted"]
            for name in ("A", "CCR", "PC", "HX", "SP"):
                is_word = self._CPU_REGS[name][2]
                idle = 0xFFFF if is_word else 0xFF
                if not halted:
                    # The CPU is EXECUTING. A register read still returns
                    # bytes -- measured against the running blinky, PC came
                    # back as a different meaningless value every poll
                    # (0x9C00, 0x4D00, 0xCB00 ...) -- but the register is
                    # changing under the read and there is no defined
                    # answer. Report that instead of animating noise.
                    regs[name] = {
                        "available": False, "value": None, "raw": None,
                        "ambiguous": False,
                        "reason": "the target is running (BDCSCR.BDMACT=0); "
                                  "CPU registers only have a defined value "
                                  "while it is halted in background mode",
                    }
                    continue
                try:
                    raw = self.read_reg(name)
                except Exception as e:
                    regs[name] = {"available": False, "value": None,
                                  "raw": None, "ambiguous": False,
                                  "reason": "%s: %s" % (type(e).__name__, e)}
                    continue
                regs[name] = {
                    "available": True, "value": raw, "raw": raw,
                    "ambiguous": raw == idle,
                    "reason": ("all-ones is also what a silent link returns, "
                               "so this particular value cannot be told apart "
                               "from 'no answer'") if raw == idle else None,
                }
            out["cpu_regs"] = regs
        return out
