"""Bounded enumeration of candidate children for checked broken links.

Scanning supplies candidates, never ownership. Callers must prove that replacing
only the parent pointer restores its original checksum, that the child has the
expected checked structure, and that the complete index remains consistent.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from itertools import combinations, product

from .format import FormatError, UnsupportedFormat
from .modern_indexes import ModernH5File, lookup3


MAX_REPAIR_SCAN_BYTES = 512 << 20
REPAIR_SCAN_BLOCK = 4 << 20
MAX_LARGE_SCAN_SOURCE_BYTES = 64 << 30
MAX_DENSE_FALLBACK_BYTES = 8 << 30
MAX_LARGE_SCAN_SECONDS = 180
MAX_LARGE_SIGNATURE_CANDIDATES = 8192
MAX_PARENT_LINK_CHANGES = 2
MAX_PARENT_LINK_TRIALS = 200_000


def restore_parent_links(raw: bytes, *, checksum_at: int, offset_size: int,
                         options: dict[int, set[int]], label: str
                         ) -> tuple[bytes, tuple[tuple[int, int], ...]]:
    """Find one unique, checksum-restoring assignment of checked child links.

    The caller must independently validate every candidate's complete child
    structure, ownership, and slot geometry. This bounded search proves only
    the original parent checksum, and changes no nonpointer bytes.
    """
    if checksum_at + 4 > len(raw) or checksum_at < 0:
        raise FormatError(f"{label} checksum lies outside parent")
    wanted = int.from_bytes(raw[checksum_at:checksum_at + 4], "little")
    if lookup3(raw[:checksum_at]) == wanted:
        raise FormatError(f"{label} repair requires an invalid original parent checksum")
    slots = sorted((position, sorted(addresses)) for position, addresses in options.items()
                   if addresses)
    solutions: list[tuple[bytes, tuple[tuple[int, int], ...]]] = []
    tried = 0
    for changes in range(1, min(MAX_PARENT_LINK_CHANGES, len(slots)) + 1):
        for selected in combinations(slots, changes):
            for addresses in product(*(values for _, values in selected)):
                tried += 1
                if tried > MAX_PARENT_LINK_TRIALS:
                    raise UnsupportedFormat(f"{label} multi-link search exceeds bounded trial budget")
                if len(set(addresses)) != len(addresses):
                    continue
                patches = tuple((position, address) for (position, _), address
                                in zip(selected, addresses))
                candidate = bytearray(raw)
                for position, address in patches:
                    if not 0 <= position <= checksum_at - offset_size:
                        raise FormatError(f"{label} child pointer lies outside checksummed bytes")
                    candidate[position:position + offset_size] = address.to_bytes(offset_size, "little")
                if lookup3(candidate[:checksum_at]) == wanted:
                    solutions.append((bytes(candidate), patches))
                    if len(solutions) > 1:
                        raise FormatError(f"{label} child-link reconstruction is ambiguous")
    if not solutions:
        raise FormatError(f"{label} checksum mismatch; no checked child links restore parent")
    return solutions[0]


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
            scan_limit = getattr(reader, "repair_scan_bytes", MAX_REPAIR_SCAN_BYTES)
            if used > scan_limit:
                label = "512 MiB" if scan_limit == MAX_REPAIR_SCAN_BYTES else f"{scan_limit} bytes"
                raise UnsupportedFormat(f"large checked-link scan exceeds {label} allocated data budget")
            yield (start, end)
            cursor = end


def candidate_addresses(reader: ModernH5File, signature: bytes) -> Iterator[int]:
    """Yield candidate addresses under cumulative reader scan byte/time quotas."""
    if len(signature) != 4:
        raise ValueError("candidate signature must be four bytes")
    large_scan = bool(getattr(reader, "large_sparse_scan", False))
    if reader.size > MAX_REPAIR_SCAN_BYTES and not large_scan:
        raise UnsupportedFormat("modern index link reconstruction scan exceeds 512 MiB limit")
    deadline = getattr(reader, "_repair_scan_deadline", None)
    if deadline is None:
        deadline = time.monotonic() + getattr(reader, "repair_scan_seconds", MAX_LARGE_SCAN_SECONDS)
        reader._repair_scan_deadline = deadline
    byte_limit = getattr(reader, "repair_scan_bytes", MAX_REPAIR_SCAN_BYTES)
    candidates = 0
    extents = (_scan_extents(reader) if large_scan else
               ((0, min(reader.size, reader.superblock.eof_address)),))
    for extent_start, extent_end in extents:
        previous = b""
        for start in range(extent_start, extent_end, REPAIR_SCAN_BLOCK):
            if time.monotonic() > deadline:
                raise UnsupportedFormat("checked-link signature scan exceeded 180 seconds")
            length = min(REPAIR_SCAN_BLOCK, extent_end - start)
            spent = getattr(reader, "_repair_scan_cumulative_bytes", 0)
            if length > byte_limit - spent:
                raise UnsupportedFormat("checked-link scans exceed the cumulative byte budget")
            reader._repair_scan_cumulative_bytes = spent + length
            data = reader._read_absolute(start, length)
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
