"""
bdc.py — higher-level Background Debug Controller driver for S08/RS08
targets, built on the PIO primitives in bdc_pio.py.

Runs on the Pico under MicroPython.

*** OPCODE DISCLAIMER ***
The BDC command opcode byte values below (SYNC excluded — it has no
opcode, it's a special pulse) are filled in from general familiarity with
the S08BDCV1/V2 command set, which is reused nearly verbatim across the
whole S08 product line's datasheets. They have NOT been re-verified
against your specific HCS08RMv1 copy in this session (the web-fetch of
that manual truncated before reaching chapter 7's command table). They
are marked below with a confidence note. **Before doing anything other
than reading, cross-check these against HCS08RMv1 §7.3.4 (or your part's
datasheet "BDC Commands" section) and fix any that don't match.**
"""

import time
from machine import Pin
from bdc_pio import (
    bdc_write_bit,
    bdc_read_bit,
    bdc_sync,
    make_state_machine,
    make_capture_state_machine,
    CYCLES_PER_SAMPLE,
)

# ---------------------------------------------------------------------------
# BDC command opcodes (S08). *** VERIFY against HCS08RMv1 before trusting ***
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
# FLASH module registers (confirmed against the S9S08SG8/SG4 datasheet
# high-page register map -- these ARE verified, unlike the opcodes above).
# ---------------------------------------------------------------------------
REG_FCDIV = 0x1820
REG_FOPT  = 0x1821
REG_FCNFG = 0x1823
REG_FPROT = 0x1824
REG_FSTAT = 0x1825
REG_FCMD  = 0x1826

FSTAT_FCBEF   = 0x80
FSTAT_FCCF    = 0x40
FSTAT_FPVIOL  = 0x20
FSTAT_FACCERR = 0x10
FSTAT_FBLANK  = 0x02

FCMD_BLANK_CHECK  = 0x05
FCMD_BYTE_PROGRAM = 0x20
FCMD_BURST_PROGRAM = 0x25
FCMD_PAGE_ERASE   = 0x40
FCMD_MASS_ERASE   = 0x41

PAGE_SIZE = 512  # bytes, per SG8/SG4 FLASH organization

# ---------------------------------------------------------------------------
# Timing constants, expressed as PIO cycles PER TARGET BDC CYCLE (see
# bdc_pio.py docstring). This is set after SYNC calibrates the target clock
# and picks a state-machine frequency.
# ---------------------------------------------------------------------------
PIO_CYCLES_PER_TARGET_CYCLE = 8  # tune based on SM max freq vs target freq


class BdcError(Exception):
    pass


class Bdc:
    def __init__(self, bkgd_pin, reset_pin, sm_id=0):
        self.bkgd_pin_num = bkgd_pin
        self.reset = Pin(reset_pin, Pin.OUT, value=1)  # idle high (released)
        self.reset.init(Pin.IN)  # open-drain style: input = released
        self.sm_id = sm_id
        self.target_freq_hz = None
        self.sm_freq = None
        self._sm = None

    # -- low level pin control -------------------------------------------
    def _reset_assert(self):
        self.reset.init(Pin.OUT, value=0)

    def _reset_release(self):
        self.reset.init(Pin.IN)  # let external/internal pull-up bring it high

    def hardware_reset_to_bdm(self, settle_ms=5):
        """Hold BKGD low across a RESET pulse to force active background
        mode on power-up/reset, per AN3335 'hardware method'."""
        # NOTE: requires the SM to be stopped / pin under plain GPIO control
        # during this sequence -- see main.py orchestration.
        bkgd = Pin(self.bkgd_pin_num, Pin.OUT, value=0)
        self._reset_assert()
        time.sleep_ms(settle_ms)
        self._reset_release()
        time.sleep_ms(1)
        bkgd.init(Pin.IN)  # release BKGD, let pull-up bring it high
        time.sleep_ms(settle_ms)

    # -- SYNC calibration ---------------------------------------------------
    def sync(self, retries=4):
        """
        Perform the SYNC handshake to discover the target's BDC clock
        frequency, and configure the state machine's clock divider
        accordingly. Returns the measured target frequency in Hz.

        See bdc_pio.py for the exact algorithm and its caveats.
        """
        # Use a conservative, slow SM clock for the SYNC probe itself, since
        # we don't yet know the target speed. 1 MHz SM clock covers target
        # BDC clocks from a few kHz up to a few hundred kHz reliably; for
        # faster targets (which is the common case, internal ~8MHz or
        # bus-clock-derived), we still detect the pulse, just with coarser
        # resolution -- good enough to pick a reasonable working frequency
        # for the subsequent read/write, which self-corrects because those
        # operations use the SAME measured ratio.
        probe_freq = 1_000_000
        sm = make_state_machine(bdc_sync, self.bkgd_pin_num, probe_freq)
        sm.active(1)

        host_low_cycles = 4000     # >> 128 target cycles even for slow targets
        timeout_cycles = 60000

        for attempt in range(retries):
            sm.put(host_low_cycles)
            sm.put(timeout_cycles)
            measured = sm.get()  # cycles measured by bdc_sync program
            if measured not in (0, 0xFFFFFFFF):
                break
        else:
            sm.active(0)
            raise BdcError(
                "SYNC: no response from target -- check wiring (BKGD, "
                "RESET, GND) and that the target is powered."
            )
        sm.active(0)

        # measured is in PIO cycles at probe_freq; response pulse should be
        # ~128 target cycles wide (see bdc_pio.py). Solve for target freq:
        #   measured_pio_cycles / probe_freq = 128 / target_freq
        target_freq = 128.0 * probe_freq / measured
        self.target_freq_hz = target_freq

        # Now choose a real operating SM frequency: some multiple of the
        # target frequency, capped at what the RP2040 can do (<=~133MHz,
        # leave headroom -> cap at 120MHz).
        desired = target_freq * PIO_CYCLES_PER_TARGET_CYCLE
        self.sm_freq = int(min(desired, 120_000_000))
        return self.target_freq_hz

    # -- bit-level tx/rx, built on the PIO byte helpers ---------------------
    def _tx_sm(self):
        if self._sm is None:
            if self.sm_freq is None:
                raise BdcError("call sync() before any tx/rx operation")
            self._sm = make_state_machine(
                bdc_write_bit, self.bkgd_pin_num, self.sm_freq
            )
        return self._sm

    def _write_bit(self, bit):
        sm = make_state_machine(bdc_write_bit, self.bkgd_pin_num, self.sm_freq)
        sm.active(1)
        k = PIO_CYCLES_PER_TARGET_CYCLE
        low = k if bit else 13 * k
        total = 16 * k
        sm.put(low)
        sm.put(total - low)
        sm.active(0)

    def _read_bit(self):
        sm = make_state_machine(bdc_read_bit, self.bkgd_pin_num, self.sm_freq)
        sm.active(1)
        k = PIO_CYCLES_PER_TARGET_CYCLE
        start_low = k
        sample_delay = 9 * k   # sample around cycle 10 (1 start + 9 wait)
        total = 16 * k
        sm.put(start_low)
        sm.put(sample_delay)
        bit = sm.get()
        sm.put(total - start_low - sample_delay)
        sm.active(0)
        return bit & 1

    def _write_byte_raw(self, value):
        for i in range(7, -1, -1):
            self._write_bit((value >> i) & 1)

    def _read_byte_raw(self):
        v = 0
        for _ in range(8):
            v = (v << 1) | self._read_bit()
        return v

    def _write_word_raw(self, value):
        self._write_byte_raw((value >> 8) & 0xFF)
        self._write_byte_raw(value & 0xFF)

    # -- self-diagnostic capture (no external logic analyzer needed) -------
    # Arms a passive PIO "scope" state machine (edge-triggered, so it
    # doesn't need to be precisely time-aligned with the operation it's
    # watching) and runs one of a few canned test operations while it
    # records. Returns raw samples + the time each sample represents, so
    # the host app can render/inspect the actual waveform.
    def _capture(self, sample_count, drive_fn):
        if self.sm_freq is None:
            # no SYNC yet -- use a conservative default so this still works
            # standalone, e.g. to sanity check SYNC itself.
            cap_target_hz = 1_000_000
        else:
            cap_target_hz = self.target_freq_hz

        # aim for ~16 samples per target BDC cycle for decent resolution
        desired = cap_target_hz * 16 * CYCLES_PER_SAMPLE
        cap_freq = int(min(max(desired, 1_000_000), 120_000_000))

        cap_sm = make_capture_state_machine(self.bkgd_pin_num, cap_freq)
        cap_sm.active(1)
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

    def capture_sync(self, sample_count=256):
        host_low_cycles = 4000
        timeout_cycles = 60000

        def drive():
            sm = make_state_machine(bdc_sync, self.bkgd_pin_num, 1_000_000)
            sm.active(1)
            sm.put(host_low_cycles)
            sm.put(timeout_cycles)
            sm.get()
            sm.active(0)

        return self._capture(sample_count, drive)

    def capture_write_bit(self, bit, sample_count=256):
        if self.sm_freq is None:
            raise BdcError("call sync() before capture_write_bit()")
        return self._capture(sample_count, lambda: self._write_bit(bit))

    def capture_read_bit(self, sample_count=256):
        if self.sm_freq is None:
            raise BdcError("call sync() before capture_read_bit()")
        result = {}

        def drive():
            result["bit"] = self._read_bit()

        cap = self._capture(sample_count, drive)
        cap["bit_value"] = result.get("bit")
        return cap

    # -- BDC commands ---------------------------------------------------
    def read_byte(self, addr):
        self._write_byte_raw(CMD_READ_BYTE)
        self._write_word_raw(addr)
        return self._read_byte_raw()

    def write_byte(self, addr, value):
        self._write_byte_raw(CMD_WRITE_BYTE)
        self._write_word_raw(addr)
        self._write_byte_raw(value & 0xFF)

    def read_status(self):
        self._write_byte_raw(CMD_READ_STATUS)
        return self._read_byte_raw()

    def background(self):
        self._write_byte_raw(CMD_BACKGROUND)

    def go(self):
        self._write_byte_raw(CMD_GO)

    def read_block(self, addr, length):
        return bytes(self.read_byte(addr + i) for i in range(length))

    def write_block(self, addr, data):
        for i, b in enumerate(data):
            self.write_byte(addr + i, b)

    # -- FLASH programming, following the SG8/SG4 datasheet §4.5.3 flow ----
    def flash_init_clock(self, bus_freq_hz):
        """
        Set up FCDIV so the internal FLASH clock lands in the required
        150-200kHz window. Only needs doing once per power-up. See
        datasheet Table 4-7 for the standard bus_freq -> (PRDIV8, DIV)
        pairs; this computes it generically instead.
        """
        # fFCLK = fBus / (DIV+1)              if PRDIV8=0
        # fFCLK = fBus / (8*(DIV+1))          if PRDIV8=1
        target = 200_000  # aim for the top of the 150-200kHz window
        prdiv8 = 0
        div = round(bus_freq_hz / target) - 1
        if div > 63:
            prdiv8 = 1
            div = round(bus_freq_hz / (8 * target)) - 1
        div = max(0, min(63, div))
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
                raise BdcError("FLASH access error (FACCERR set), FSTAT=0x%02X" % stat)
            if stat & FSTAT_FPVIOL:
                raise BdcError("FLASH protection violation (FPVIOL set)")
            if stat & FSTAT_FCCF:
                return stat
            if time.ticks_diff(time.ticks_ms(), start) > timeout_ms:
                raise BdcError("timeout waiting for FCCF")

    def _flash_command(self, addr, data_byte, command):
        # Step 1: write to the target flash address (latches addr+data)
        self.write_byte(addr, data_byte)
        # Step 2: write command code to FCMD
        self.write_byte(REG_FCMD, command)
        # Step 3: write 1 to FCBEF in FSTAT to launch the command
        self.write_byte(REG_FSTAT, FSTAT_FCBEF)
        self._flash_wait_command()

    def flash_erase_page(self, addr_in_page):
        self._flash_command(addr_in_page, 0xFF, FCMD_PAGE_ERASE)

    def flash_mass_erase(self):
        self._flash_command(0xFFFF, 0xFF, FCMD_MASS_ERASE)

    def flash_program_byte(self, addr, value):
        self._flash_command(addr, value, FCMD_BYTE_PROGRAM)

    def flash_write_region(self, start_addr, data, erase_pages=True):
        """
        Program `data` (bytes) starting at `start_addr`. Erases whole
        512-byte pages that the region touches first if erase_pages=True.
        Does NOT touch FCDIV -- call flash_init_clock() first.
        """
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
