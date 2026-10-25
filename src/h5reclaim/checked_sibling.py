"""Rooted structural allocation inventory when native sibling info is damaged.

This establishes competing physical ownership only.  No raw value is inferred
from a sibling dataset, and a failed or ambiguous index leaves the enclosing
inventory incomplete.
"""

from __future__ import annotations

from math import prod
from pathlib import Path

import h5py

from .format import FormatError, UnsupportedFormat
from .modern_indexes import ModernH5File


def checked_chunk_ranges(snapshot: Path, item: h5py.Dataset, address: int,
                         *, max_allocations: int, max_seconds: float,
                         max_bytes: int) -> tuple[tuple[int, int], ...]:
    """Return every checked allocated chunk range under a rooted hard link."""
    from .repaired_native import _file_element_width

    if item.chunks is None or item.shape is None or len(item.shape) > 32:
        raise UnsupportedFormat("native sibling fallback requires a bounded chunked dataset")
    grid = prod((axis + width - 1) // width for axis, width in zip(item.shape, item.chunks))
    if not 1 <= grid <= max_allocations:
        raise UnsupportedFormat("native sibling fallback grid exceeds the allocation quota")
    creation = item.id.get_create_plist()
    if creation.get_external_count() or item.is_virtual:
        raise UnsupportedFormat("native sibling fallback does not follow dependent storage")
    filters = tuple(int(creation.get_filter(i)[0]) for i in range(creation.get_nfilters()))
    with ModernH5File(snapshot, large_sparse_scan=True, max_chunks=grid,
                      max_chunk_bytes=128 << 20,
                      repair_scan_bytes=min(512 << 20, max_bytes),
                      repair_scan_seconds=min(180, max_seconds)) as reader:
        width = _file_element_width(item.id.get_type(), reader.superblock.offset_size)
        index = reader.read_index(address, tuple(item.shape), tuple(item.chunks), width,
                                  maxshape=tuple(item.maxshape), filters=filters, max_chunks=grid)
        if not (index.reconstructed_links or index.reconstructed_data_block_pointer) or not index.chunks:
            raise UnsupportedFormat("native sibling fallback has no checked damaged link and allocated chunks")
        ranges = []
        coordinates = set()
        for record in index.chunks:
            if record.coordinate in coordinates:
                raise FormatError("native sibling fallback repeats a chunk coordinate")
            coordinates.add(record.coordinate)
            start = reader.absolute(record.address)
            end = start + record.size
            if (record.size <= 0 or end > reader.superblock.eof_address or end > reader.size
                    or record.filter_mask >> len(filters)):
                raise FormatError("native sibling fallback chunk range contradicts its source")
            ranges.append((start, end))
        ranges.sort()
        if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
            raise FormatError("native sibling fallback allocations overlap")
        if any(start < meta_end and meta_start < end
               for start, end in ranges for meta_start, meta_end, _ in reader.metadata_ranges):
            raise FormatError("native sibling fallback chunk overlaps checked metadata")
        return tuple(ranges)
