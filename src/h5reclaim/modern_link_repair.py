"""Bounded enumeration of possible children for a *single* broken checked link.

Scanning supplies candidates, never ownership. Callers must prove that replacing
only the parent pointer restores its original checksum, that the child has the
expected checked structure, and that the complete index remains consistent.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator

from .format import UnsupportedFormat
from .modern_indexes import ModernH5File


MAX_REPAIR_SCAN_BYTES = 512 << 20
REPAIR_SCAN_BLOCK = 4 << 20
MAX_LARGE_SCAN_SOURCE_BYTES = 64 << 30
MAX_DENSE_FALLBACK_BYTES = 8 << 30
MAX_LARGE_SCAN_SECONDS = 180
MAX_LARGE_SIGNATURE_CANDIDATES = 8192


def _scan_extents(reader: ModernH5File) -> Iterator[tuple[int, int]]:
    """Cover all possible nonzero signatures or explicitly refuse the search.

    A hole can contain only zeros. The four-byte metadata signatures used by
    callers contain no zero bytes. SEEK_DATA/HOLE therefore skips no possible
    candidate. A Windows or unsupported-filesystem fallback reads the *whole*
    image under a separate eight GiB time/space cap.
    """
    limit = min(reader.size, reader.superblock.eof_address)
    if limit > MAX_LARGE_SCAN_SOURCE_BYTES:
        raise UnsupportedFormat("large checked-link scan exceeds 64 GiB source limit")
    if not hasattr(os, "SEEK_DATA") or not hasattr(os, "SEEK_HOLE"):
        if limit > MAX_DENSE_FALLBACK_BYTES:
            raise UnsupportedFormat("dense checked-link scan exceeds 8 GiB fallback limit")
        yield (0, limit)
        return
    cursor = 0
    used = 0
    # os.lseek on a buffered Python reader's descriptor would desynchronize
    # its internal buffer. Keep extent probes on an independent descriptor.
    with reader.path.open("rb", buffering=0) as extent_handle:
        while cursor < limit:
            try:
                start = os.lseek(extent_handle.fileno(), cursor, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    return
                if exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
                    if limit > MAX_DENSE_FALLBACK_BYTES:
                        raise UnsupportedFormat("sparse scan unavailable and dense fallback exceeds 8 GiB") from exc
                    yield (cursor, limit)
                    return
                raise UnsupportedFormat("sparse checked-link extent lookup failed") from exc
            if not cursor <= start <= limit:
                raise UnsupportedFormat("sparse scan returned an invalid data extent")
            if start == limit:
                return
            try:
                end = min(os.lseek(extent_handle.fileno(), start, os.SEEK_HOLE), limit)
            except OSError as exc:
                raise UnsupportedFormat("sparse checked-link hole lookup failed") from exc
            if not start < end <= limit:
                raise UnsupportedFormat("sparse scan returned an invalid hole extent")
            used += end - start
            if used > MAX_REPAIR_SCAN_BYTES:
                raise UnsupportedFormat("large checked-link scan exceeds 512 MiB allocated data budget")
            yield (start, end)
            cursor = end


def candidate_addresses(reader: ModernH5File, signature: bytes) -> Iterator[int]:
    """Yield relative addresses of matching signatures within a fixed scan cap."""
    if len(signature) != 4:
        raise ValueError("candidate signature must be four bytes")
    large_scan = bool(getattr(reader, "large_sparse_scan", False))
    if reader.size > MAX_REPAIR_SCAN_BYTES and not large_scan:
        raise UnsupportedFormat("modern index link reconstruction scan exceeds 512 MiB limit")
    deadline = time.monotonic() + MAX_LARGE_SCAN_SECONDS
    candidates = 0
    extents = (_scan_extents(reader) if large_scan else
               ((0, min(reader.size, reader.superblock.eof_address)),))
    for extent_start, extent_end in extents:
        previous = b""
        for start in range(extent_start, extent_end, REPAIR_SCAN_BLOCK):
            if time.monotonic() > deadline:
                raise UnsupportedFormat("checked-link signature scan exceeded 180 seconds")
            data = reader._read_absolute(start, min(REPAIR_SCAN_BLOCK, extent_end - start))
            window = previous + data
            base = start - len(previous)
            at = window.find(signature)
            while at >= 0:
                candidates += 1
                if large_scan and candidates > MAX_LARGE_SIGNATURE_CANDIDATES:
                    raise UnsupportedFormat("large checked-link scan exceeds signature candidate quota")
                if time.monotonic() > deadline:
                    raise UnsupportedFormat("checked-link signature scan exceeded 180 seconds")
                absolute = base + at
                address = absolute - reader.superblock.base_address
                if 0 <= address < 1 << (8 * reader.superblock.offset_size):
                    yield address
                at = window.find(signature, at + 1)
            previous = window[-3:]
