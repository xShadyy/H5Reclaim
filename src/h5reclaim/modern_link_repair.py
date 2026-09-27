"""Bounded enumeration of possible children for a *single* broken checked link.

Scanning supplies candidates, never ownership. Callers must prove that replacing
only the parent pointer restores its original checksum, that the child has the
expected checked structure, and that the complete index remains consistent.
"""

from __future__ import annotations

from collections.abc import Iterator

from .format import UnsupportedFormat
from .modern_indexes import ModernH5File


MAX_REPAIR_SCAN_BYTES = 512 << 20
REPAIR_SCAN_BLOCK = 4 << 20


def candidate_addresses(reader: ModernH5File, signature: bytes) -> Iterator[int]:
    """Yield relative addresses of matching signatures within a fixed scan cap."""
    if len(signature) != 4:
        raise ValueError("candidate signature must be four bytes")
    if reader.size > MAX_REPAIR_SCAN_BYTES:
        raise UnsupportedFormat("modern index link reconstruction scan exceeds 512 MiB limit")
    previous = b""
    scan_limit = min(reader.size, reader.superblock.eof_address)
    for start in range(0, scan_limit, REPAIR_SCAN_BLOCK):
        data = reader._read_absolute(
            start, min(REPAIR_SCAN_BLOCK, scan_limit - start))
        window = previous + data
        base = start - len(previous)
        at = window.find(signature)
        while at >= 0:
            absolute = base + at
            address = absolute - reader.superblock.base_address
            if 0 <= address < 1 << (8 * reader.superblock.offset_size):
                yield address
            at = window.find(signature, at + 1)
        previous = window[-3:]
