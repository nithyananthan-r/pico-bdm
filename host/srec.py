"""
srec.py — minimal Motorola S-record (.s19/.s28/.s37) parser.
Returns a list of (address, bytes) chunks from S1/S2/S3 data records.
Good enough for typical CodeWarrior HCS08 output (S1 records, 16-bit addr).

Validation policy (tightened 2026-09-11): every realistic malformed input
raises ValueError with a line number, because that is the only exception
type host/app.py's /api/flash_srec route catches — anything else (the old
code could raise a bare IndexError on a truncated line) escaped as a Flask
500 HTML page, which then broke the browser's res.json() with a confusing
"Unexpected token" error instead of showing the real problem. Records with
a bad checksum are rejected outright rather than silently programmed.
"""

# Number of address bytes for each data record type.
_ADDR_LEN = {"1": 2, "2": 3, "3": 4}

# Record types that carry no data we care about: header, record count, and
# the three termination/start-address forms.
_SKIP_TYPES = ("0", "4", "5", "6", "7", "8", "9")


def _checksum(raw):
    """S-record checksum: one's complement of the low byte of the sum of
    the byte-count field and every byte after it (address + data)."""
    return (~sum(raw)) & 0xFF


def parse_srec(text):
    chunks = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        # Accept lowercase 's' too -- some tools emit it, and rejecting
        # those files silently (the old behaviour: the line was skipped as
        # if it were a comment) is worse than either accepting or erroring.
        if line[0] not in ("S", "s"):
            continue

        if len(line) < 4:
            raise ValueError("line %d: truncated S-record %r" % (lineno, line))

        rectype = line[1]
        if rectype in _SKIP_TYPES:
            continue
        if rectype not in _ADDR_LEN:
            raise ValueError(
                "line %d: unknown S-record type 'S%s'" % (lineno, rectype)
            )

        try:
            byte_count = int(line[2:4], 16)
        except ValueError:
            raise ValueError(
                "line %d: bad byte-count field %r" % (lineno, line[2:4])
            )

        body = line[4:]
        # The byte count covers address + data + checksum, i.e. everything
        # after the count field itself.
        expected_hex = byte_count * 2
        if len(body) != expected_hex:
            raise ValueError(
                "line %d: declared byte count %d means %d hex digits after "
                "the count field, but found %d"
                % (lineno, byte_count, expected_hex, len(body))
            )

        try:
            raw = bytes.fromhex(body)
        except ValueError:
            raise ValueError("line %d: non-hex characters in record" % lineno)

        addr_len = _ADDR_LEN[rectype]
        # addr_len address bytes + at least the checksum byte
        if byte_count < addr_len + 1:
            raise ValueError(
                "line %d: byte count %d too small for an S%s record "
                "(needs at least %d)" % (lineno, byte_count, rectype, addr_len + 1)
            )

        # Checksum is computed over the count field plus everything after
        # it except the checksum byte itself.
        if _checksum(bytes([byte_count]) + raw[:-1]) != raw[-1]:
            raise ValueError(
                "line %d: checksum mismatch (record says 0x%02X, computed "
                "0x%02X) -- the file is corrupt; refusing to program it"
                % (lineno, raw[-1], _checksum(bytes([byte_count]) + raw[:-1]))
            )

        addr = int.from_bytes(raw[:addr_len], "big")
        data = raw[addr_len:-1]  # drop trailing checksum byte
        if data:
            chunks.append((addr, data))
    return chunks


def merge_contiguous(chunks):
    """Merge adjacent (addr, data) chunks into fewer, larger writes."""
    if not chunks:
        return []
    chunks = sorted(chunks, key=lambda c: c[0])
    merged = [list(chunks[0])]
    for addr, data in chunks[1:]:
        last_addr, last_data = merged[-1]
        if addr == last_addr + len(last_data):
            merged[-1][1] = last_data + data
        else:
            merged.append([addr, data])
    return [(a, bytes(d)) for a, d in merged]
