"""
srec.py — minimal Motorola S-record (.s19/.s28/.s37) parser.
Returns a list of (address, bytes) chunks from S1/S2/S3 data records.
Good enough for typical CodeWarrior HCS08 output (S1 records, 16-bit addr).
"""


def parse_srec(text):
    chunks = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or not line.startswith("S"):
            continue
        rectype = line[1]
        if rectype in ("0", "5", "7", "8", "9"):
            continue  # header / count / termination records, skip
        if rectype not in ("1", "2", "3"):
            continue

        byte_count = int(line[2:4], 16)
        payload = line[4 : 4 + (byte_count * 2)]
        raw = bytes.fromhex(payload)

        if rectype == "1":
            addr_len = 2
        elif rectype == "2":
            addr_len = 3
        else:  # "3"
            addr_len = 4

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
