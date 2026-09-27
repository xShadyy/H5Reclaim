"""Read intact HDF5 extensible-array chunk indexes from an anchored layout.

The selected object's layout pointer, shape, maximum shape, chunk dimensions,
filter IDs, and object address must all come from the *same immutable snapshot*.
This module never scans for orphan signatures or infers missing pointers. It
checks the EAHD, EAIB, EASB, and nonpaged EADB metadata checksums and their
parent/back pointers before attributing a raw byte range to a coordinate.

Format reference: https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
Appendix VII.D. Mapping and sizes are cross-checked against the HDF Group's
H5EA.c, H5EAhdr.c, H5EAiblock.c, H5EAcache.c, and H5Dearray.c source.
Paged data blocks are deliberately refused until their page initialization
bitmaps and each page checksum are validated independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from math import prod

from .format import FormatError, UnsupportedFormat
from .modern_indexes import (
    MAX_CHUNK_BYTES, MAX_CHUNKS, MAX_READ_BYTES, ModernChunk,
    ModernH5File, ModernIndex, lookup3,
)

MAX_METADATA_BLOCKS = 512
MAX_METADATA_TOTAL_BYTES = 16 << 20


def _uint(raw: bytes) -> int:
    return int.from_bytes(raw, "little")


def _ceil_grid(shape: tuple[int, ...], chunks: tuple[int, ...]) -> tuple[int, ...]:
    return tuple((length + width - 1) // width for length, width in zip(shape, chunks))


@dataclass(frozen=True)
class _SuperblockRow:
    start: int
    start_data_pointer: int
    data_blocks: int
    elements_per_block: int


class _Reader:
    def __init__(self, reader: ModernH5File, root: int):
        self.reader = reader
        self.root = root
        self.seen: dict[int, tuple[bytes, int, str]] = {}
        self.bytes_read = 0

    def meta(self, address: int, size: int, signature: bytes, kind: str) -> bytes:
        if address in self.seen:
            previous = self.seen[address]
            if previous[1:] != (size, kind):
                raise FormatError("extensible-array metadata address has conflicting owners")
            return previous[0]
        if size < 10 or size > MAX_READ_BYTES or len(self.seen) >= MAX_METADATA_BLOCKS:
            raise UnsupportedFormat("extensible-array metadata block limit exceeded")
        if self.bytes_read + size > MAX_METADATA_TOTAL_BYTES:
            raise UnsupportedFormat("extensible-array metadata read budget exceeded")
        absolute = self.reader.absolute(address)
        for start, end, _ in self.reader.metadata_ranges:
            if absolute < end and start < absolute + size:
                raise FormatError("extensible-array metadata overlaps previously validated metadata")
        raw = self.reader.read_at(address, size)
        if raw[:4] != signature or raw[4] != 0:
            raise FormatError(f"{kind} signature or version is invalid")
        if lookup3(raw[:-4]) != _uint(raw[-4:]):
            raise FormatError(f"{kind} checksum mismatch")
        self.seen[address] = (raw, size, kind)
        self.bytes_read += size
        self.reader.metadata_ranges.append((absolute, absolute + size, kind))
        return raw


def read_extensible_array(
    reader: ModernH5File,
    root: int,
    shape: tuple[int, ...],
    chunks: tuple[int, ...],
    element_size: int,
    *,
    maxshape: tuple[int | None, ...],
    layout_version: int,
    layout_pointer_offset: int,
    layout_params: tuple[int, int, int, int, int],
    object_address: int,
    filters: tuple[int, ...] = (),
    allow_sparse: bool = False,
    max_chunks: int = MAX_CHUNKS,
) -> ModernIndex:
    """Return validated, coordinate-attributed allocated EA chunks.

    ``layout_params`` are the five observed layout bytes in this order:
    (maximum index bits, index-block elements, minimum superblock pointers,
    minimum data-block elements, page bits). The one unlimited dimension is
    moved to the first position when computing the index, per HDF5's swizzle
    rule. If ``allow_sparse`` is true, missing slots are
    simply omitted and a caller must label those coordinates unknown. The
    missing slots are never passed off as recovered fill values.
    """
    if layout_version not in (4, 5) or len(layout_params) != 5:
        raise UnsupportedFormat("extensible array requires a version-4/5 layout and five index parameters")
    shape, chunks, maxshape, filters = tuple(shape), tuple(chunks), tuple(maxshape), tuple(filters)
    if not 1 <= len(shape) <= 4 or len(chunks) != len(shape) or len(maxshape) != len(shape):
        raise UnsupportedFormat("extensible array requires matching rank 1 through 4")
    unlimited_dimensions = [i for i, length in enumerate(maxshape) if length is None]
    if len(unlimited_dimensions) != 1:
        raise UnsupportedFormat("extensible array requires exactly one unlimited dimension")
    unlim = unlimited_dimensions[0]
    if any(not isinstance(v, int) or v <= 0 for v in (*shape, *chunks, element_size)):
        raise FormatError("invalid selected dataset shape or chunk size")
    if any(not isinstance(v, int) or v < current for i, (v, current) in
           enumerate(zip(maxshape, shape)) if i != unlim):
        raise FormatError("fixed maximum dimension is smaller than current dataset extent")
    if max_chunks < 1 or max_chunks > MAX_CHUNKS:
        raise UnsupportedFormat("extensible-array chunk budget is invalid")
    grid = _ceil_grid(shape, chunks)
    fixed_max_grid = tuple((maxshape[i] + chunks[i] - 1) // chunks[i]
                           for i in range(len(shape)) if i != unlim)
    count = prod(grid)
    if count > max_chunks:
        raise UnsupportedFormat("extensible-array chunk count exceeds bounded parser limit")
    chunk_bytes = prod(chunks) * element_size
    if chunk_bytes < 1 or chunk_bytes > MAX_CHUNK_BYTES:
        raise UnsupportedFormat("extensible-array unfiltered chunk size exceeds limit")
    max_bits, index_elems, min_ptrs, min_elems, page_bits = layout_params
    if (not 1 <= max_bits <= 48 or not 1 <= index_elems <= 255
            or min_ptrs < 2 or min_ptrs & (min_ptrs - 1)
            or min_elems < 1 or min_elems & (min_elems - 1)
            or not 1 <= page_bits <= 24):
        raise UnsupportedFormat("extensible-array layout parameters are outside checked profile")
    first_indirect = 2 * (min_ptrs.bit_length() - 1)
    rows_count = 1 + max_bits - (min_elems.bit_length() - 1)
    if rows_count <= first_indirect or rows_count > 64:
        raise UnsupportedFormat("extensible-array superblock schedule is unsupported")
    offsize = reader.superblock.offset_size
    lensize = reader.superblock.length_size
    undefined = reader.superblock.undefined_address
    filtered = bool(filters)
    chunk_size_width = (min(lensize, 8) if layout_version == 5 else
                        min(8, 1 + ((chunk_bytes.bit_length() - 1 + 8) // 8))) if filtered else 0
    entry_size = offsize + (chunk_size_width + 4 if filtered else 0)
    state = _Reader(reader, root)
    header_size = 16 + 6 * lensize + offsize
    header = state.meta(root, header_size, b"EAHD", "extensible-array header")
    raw_size = header[6]
    observed_params = (header[7], header[8], header[10], header[9], header[11])
    if header[5] != int(filtered):
        raise FormatError("extensible-array client disagrees with selected filter pipeline")
    if observed_params != layout_params:
        raise FormatError("extensible-array header parameters disagree with selected layout")
    if raw_size != entry_size:
        raise FormatError("extensible-array entry size contradicts dataset filters or layout version")
    stats = tuple(_uint(header[12+i*lensize:12+(i+1)*lensize]) for i in range(6))
    n_sblocks, _sblock_bytes, n_dblocks, _dblock_bytes, max_index_set, n_elements = stats
    if max_index_set > (1 << max_bits) or n_elements < (index_elems if max_index_set else 0):
        raise FormatError("extensible-array header statistics are inconsistent")
    header_pointer_offset = reader.absolute(root) + 12 + 6 * lensize
    index_address = _uint(header[12+6*lensize:12+6*lensize+offsize])
    if index_address == undefined:
        if not allow_sparse or max_index_set or n_dblocks or n_sblocks:
            raise UnsupportedFormat("extensible-array index block is unallocated")
        return ModernIndex("extensible_array", layout_version, object_address, root,
                           layout_pointer_offset, chunks, element_size, ())

    rows: list[_SuperblockRow] = []
    start = start_pointer = 0
    for row in range(rows_count):
        nblocks = 1 << (row // 2)
        nelmts = min_elems << ((row + 1) // 2)
        rows.append(_SuperblockRow(start, start_pointer, nblocks, nelmts))
        start += nblocks * nelmts
        start_pointer += nblocks
    n_direct = 2 * (min_ptrs - 1)
    n_indirect = rows_count - first_indirect
    index_size = 10 + offsize + index_elems * entry_size + (n_direct + n_indirect) * offsize
    index = state.meta(index_address, index_size, b"EAIB", "extensible-array index block")
    if index[5] != int(filtered) or _uint(index[6:6+offsize]) != root:
        raise FormatError("extensible-array index block client or header back-pointer is invalid")
    direct_start = 6 + offsize + index_elems * entry_size
    indirect_start = direct_start + n_direct * offsize
    page_elems = 1 << page_bits

    block_cache: dict[int, tuple[bytes, int, int, int]] = {}
    # Address -> raw block, first slot, capacity, expected row-specific offset.
    records: list[ModernChunk] = []
    for cell in product(*(range(d) for d in grid)):
        swizzled = (cell[unlim],) + cell[:unlim] + cell[unlim+1:]
        linear = sum(position * prod(fixed_max_grid[i:])
                     for i, position in enumerate(swizzled))
        coordinate = tuple(i * c for i, c in zip(cell, chunks))
        if linear >= (1 << max_bits):
            raise UnsupportedFormat("dataset grid exceeds extensible-array index address space")
        if linear >= max_index_set:
            if allow_sparse:
                continue
            raise UnsupportedFormat("extensible-array chunk slot has never been allocated")
        chain: list[dict[str, object]] = [
            {"kind": "layout_to_eahd", "parent_address": object_address,
             "target_address": root, "pointer_offset": layout_pointer_offset},
            {"kind": "eahd_to_eaib", "parent_address": root,
             "target_address": index_address, "pointer_offset": header_pointer_offset},
        ]
        if linear < index_elems:
            slot_owner, slot = index, 6 + offsize + linear * entry_size
            slot_address = index_address
        else:
            adjusted = linear - index_elems
            row_index = (adjusted // min_elems + 1).bit_length() - 1
            if row_index >= rows_count:
                raise UnsupportedFormat("extensible-array superblock selection exceeds header capacity")
            row = rows[row_index]
            position = adjusted - row.start
            if position < 0 or position >= row.data_blocks * row.elements_per_block:
                raise FormatError("extensible-array index mapping contradicts row bounds")
            db_index = position // row.elements_per_block
            if row.elements_per_block > page_elems:
                raise UnsupportedFormat("paged extensible-array data blocks require page validation")
            if row_index < first_indirect:
                pointer_slot = direct_start + (row.start_data_pointer + db_index) * offsize
                address = _uint(index[pointer_slot:pointer_slot+offsize])
                pointer_parent = index_address
                expected_block_offset = row.start + (row.start_data_pointer + db_index) * row.elements_per_block
                parent_kind = "eaib_to_eadb"
            else:
                sblock_slot = indirect_start + (row_index - first_indirect) * offsize
                sblock_address = _uint(index[sblock_slot:sblock_slot+offsize])
                if sblock_address == undefined:
                    if allow_sparse:
                        continue
                    raise UnsupportedFormat("extensible-array secondary block is unallocated")
                if n_sblocks < 1:
                    raise FormatError("extensible-array header denies observed secondary block")
                sblock_size = 10 + offsize + (max_bits + 7)//8 + row.data_blocks * offsize
                sblock = state.meta(sblock_address, sblock_size, b"EASB", "extensible-array secondary block")
                if (sblock[5] != int(filtered) or _uint(sblock[6:6+offsize]) != root
                        or _uint(sblock[6+offsize:6+offsize+(max_bits+7)//8]) != row.start):
                    raise FormatError("extensible-array secondary block owner or row offset disagrees")
                sblock_absolute = reader.absolute(sblock_address)
                chain.append({"kind": "eaib_to_easb", "parent_address": index_address,
                              "target_address": sblock_address,
                              "pointer_offset": reader.absolute(index_address)+sblock_slot})
                pointer_slot = 6 + offsize + (max_bits+7)//8 + db_index * offsize
                address = _uint(sblock[pointer_slot:pointer_slot+offsize])
                pointer_parent = sblock_address
                expected_block_offset = row.start + db_index * row.elements_per_block
                parent_kind = "easb_to_eadb"
            if address == undefined:
                if allow_sparse:
                    continue
                raise UnsupportedFormat("extensible-array data block is unallocated")
            if n_dblocks < 1:
                raise FormatError("extensible-array header denies observed data block")
            chain.append({"kind": parent_kind, "parent_address": pointer_parent,
                          "target_address": address,
                          "pointer_offset": reader.absolute(pointer_parent)+pointer_slot})
            if address in block_cache:
                _, _, capacity, prior_offset = block_cache[address]
                if (capacity, prior_offset) != (row.elements_per_block, expected_block_offset):
                    raise FormatError("extensible-array data block is claimed by two different rows")
            else:
                block_size = 10 + offsize + (max_bits+7)//8 + row.elements_per_block*entry_size
                block = state.meta(address, block_size, b"EADB", "extensible-array data block")
                if (block[5] != int(filtered) or _uint(block[6:6+offsize]) != root
                        or _uint(block[6+offsize:6+offsize+(max_bits+7)//8]) != expected_block_offset):
                    raise FormatError("extensible-array data block owner or block offset disagrees")
                block_cache[address] = (block, 6+offsize+(max_bits+7)//8,
                                        row.elements_per_block, expected_block_offset)
            block, elements_start, _, _ = block_cache[address]
            slot_owner = block
            slot = elements_start + (position % row.elements_per_block) * entry_size
            slot_address = address
        entry = slot_owner[slot:slot+entry_size]
        if len(entry) != entry_size:
            raise FormatError("extensible-array slot extends outside validated metadata")
        payload_address = _uint(entry[:offsize])
        if payload_address == undefined:
            if allow_sparse:
                continue
            raise UnsupportedFormat("extensible-array chunk slot is unallocated")
        size = _uint(entry[offsize:offsize+chunk_size_width]) if filtered else chunk_bytes
        mask = _uint(entry[-4:]) if filtered else 0
        if filtered and mask >> len(filters):
            raise FormatError("extensible-array chunk filter mask exceeds selected pipeline")
        if size < 1 or size > MAX_CHUNK_BYTES:
            raise UnsupportedFormat("extensible-array stored chunk size exceeds limit")
        absolute = reader.absolute(payload_address)
        if size > reader.superblock.eof_address - absolute:
            raise FormatError("extensible-array chunk crosses declared HDF5 end-of-file")
        slot_offset = reader.absolute(slot_address) + slot
        records.append(ModernChunk(
            coordinate, payload_address, size, mask,
            {"rule": "checksum-validated extensible-array slot and row mapping",
             "object_address": object_address, "layout_version": layout_version,
             "index_type": "extensible_array", "linear_index": linear,
             "extensible_array_header_address": root,
             "extensible_array_index_block_address": index_address,
             "slot_owner_address": slot_address,
             "index_chain": chain},
            pointer_offset=slot_offset,
        ))
    if len(block_cache) > n_dblocks or sum(kind == "extensible-array secondary block"
                                           for _, _, kind in state.seen.values()) > n_sblocks:
        raise FormatError("extensible-array observed blocks exceed header counts")
    ranges = sorted((reader.absolute(item.address), reader.absolute(item.address)+item.size)
                    for item in records)
    if any(right[0] < left[1] for left, right in zip(ranges, ranges[1:])):
        raise FormatError("extensible-array chunk payload ranges overlap")
    for low, high in ranges:
        if any(low < end and start < high for start, end, _ in reader.metadata_ranges):
            raise FormatError("extensible-array chunk overlaps validated metadata")
    return ModernIndex("extensible_array", layout_version, object_address, root,
                       layout_pointer_offset, chunks, element_size, tuple(records),
                       data_block_address=index_address,
                       data_block_pointer_offset=header_pointer_offset)
