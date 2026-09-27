"""Read HDF5 extensible-array chunk indexes from an anchored layout.

The selected object's layout pointer, shape, maximum shape, chunk dimensions,
filter IDs, and object address must all come from the *same immutable snapshot*.
One EAHD-to-EAIB, EAIB-to-child, or EASB-to-EADB link may be reconstructed by
a bounded candidate scan only when replacing that pointer restores the
original parent checksum and the unique checked child has the expected owner
and row offset. It
checks the EAHD, EAIB, EASB, EADB, and initialized data-block page checksums
and their parent/back pointers before attributing bytes to a coordinate.

Format reference: https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
Appendix VII.D. Mapping and sizes are cross-checked against the HDF Group's
H5EA.c, H5EAhdr.c, H5EAiblock.c, H5EAcache.c, and H5Dearray.c source.
Paged data blocks use the secondary block's per-data-block page bitmap. An
uninitialized page never supplies a fill measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from math import prod

from .format import FormatError, UnsupportedFormat
from .modern_link_repair import candidate_addresses
from .modern_indexes import (
    MAX_CHUNK_BYTES, MAX_CHUNKS, MAX_READ_BYTES, ModernChunk,
    ModernH5File, ModernIndex, lookup3,
)

MAX_METADATA_BLOCKS = 512
MAX_METADATA_TOTAL_BYTES = 16 << 20
MAX_REPAIR_CANDIDATES = 8192


def _uint(raw: bytes) -> int:
    return int.from_bytes(raw, "little")


def _ceil_grid(shape: tuple[int, ...], chunks: tuple[int, ...]) -> tuple[int, ...]:
    return tuple((length + width - 1) // width for length, width in zip(shape, chunks))


def _bitmap_bit(bitmap: bytes, bit: int) -> bool:
    return bool(bitmap[bit // 8] & (0x80 >> (bit % 8)))


def _bitmap_any(bitmap: bytes, start: int, count: int) -> bool:
    """Check one block's bits without borrowing its neighbor's padding bits."""
    first, last = start // 8, (start + count - 1) // 8
    if first == last:
        return bool(bitmap[first] & (0xFF >> (start % 8))
                    & ((0xFF << (7 - ((start + count - 1) % 8))) & 0xFF))
    return (bool(bitmap[first] & (0xFF >> (start % 8)))
            or any(bitmap[first + 1:last])
            or bool(bitmap[last] & ((0xFF << (7 - ((start + count - 1) % 8))) & 0xFF)))


@dataclass(frozen=True)
class _SuperblockRow:
    start: int
    start_data_pointer: int
    data_blocks: int
    elements_per_block: int


class _Reader:
    def __init__(self, reader: ModernH5File, root: int,
                 repairs: dict[int, bytes] | None = None):
        self.reader = reader
        self.root = root
        self.repairs = repairs or {}
        self.seen: dict[int, tuple[bytes, int, str]] = {}
        self.bytes_read = 0

    def _checked(self, address: int, size: int, kind: str,
                 signature: bytes | None) -> bytes:
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
        observed = self.reader.read_at(address, size)
        raw = self.repairs.get(address, observed)
        if len(raw) != len(observed):
            raise FormatError("extensible-array repaired metadata length differs")
        if signature is not None and (raw[:4] != signature or raw[4] != 0):
            raise FormatError(f"{kind} signature or version is invalid")
        if lookup3(raw[:-4]) != _uint(raw[-4:]):
            raise FormatError(f"{kind} checksum mismatch")
        self.seen[address] = (raw, size, kind)
        self.bytes_read += size
        self.reader.metadata_ranges.append((absolute, absolute + size, kind))
        return raw

    def meta(self, address: int, size: int, signature: bytes, kind: str) -> bytes:
        return self._checked(address, size, kind, signature)

    def page(self, address: int, size: int) -> bytes:
        # EA pages have no signature or back-pointer; the checked EASB bitmap,
        # checked EADB, and contiguous allocation identify their coordinates.
        return self._checked(address, size, "extensible-array data block page", None)


def _reconstruct_header_index_link(reader: ModernH5File, header: bytes,
                                   root: int, pointer: int,
                                   index_size: int, filtered: bool) -> int:
    """Find the unique checked EAIB that restores the unmodified EAHD checksum."""
    sb = reader.superblock
    offsize = sb.offset_size
    wanted = _uint(header[-4:])
    if index_size > MAX_READ_BYTES:
        raise UnsupportedFormat("extensible-array index block exceeds bounded read")
    matches: list[int] = []
    for address in candidate_addresses(reader, b"EAIB"):
        absolute = sb.base_address + address
        if absolute + index_size > sb.eof_address or any(
            absolute < end and start < absolute + index_size
            for start, end, _ in reader.metadata_ranges
        ):
            continue
        repaired = bytearray(header)
        repaired[pointer:pointer+offsize] = address.to_bytes(offsize, "little")
        if lookup3(repaired[:-4]) != wanted:
            continue
        block = reader.read_at(address, index_size)
        if (block[:4] != b"EAIB" or block[4] != 0
                or block[5] != int(filtered)
                or _uint(block[6:6+offsize]) != root
                or lookup3(block[:-4]) != _uint(block[-4:])):
            continue
        matches.append(address)
        if len(matches) > 1:
            raise FormatError("extensible-array header index-link repair is ambiguous")
    if not matches:
        raise FormatError("extensible-array header checksum mismatch; no uniquely verified index-block pointer")
    return matches[0]


def _reconstruct_index_child_link(
    reader: ModernH5File, index: bytes, *, root: int,
    filtered: bool, entry_size: int, max_bits: int, page_bits: int,
    rows: list[_SuperblockRow], first_indirect: int,
    direct_start: int, indirect_start: int,
) -> tuple[bytes, int, int, str]:
    """Restore one checked EAIB child link to an owned, checked EADB/EASB."""
    sb = reader.superblock
    offsize = sb.offset_size
    block_prefix = 6 + offsize + (max_bits + 7) // 8
    page_elems = 1 << page_bits
    wanted = _uint(index[-4:])
    matches: list[tuple[bytes, int, int, str]] = []
    candidates_seen = 0
    for signature, candidate_kind in ((b"EADB", "extensible-array data block"),
                                      (b"EASB", "extensible-array secondary block")):
        for address in candidate_addresses(reader, signature):
            candidates_seen += 1
            if candidates_seen > MAX_REPAIR_CANDIDATES:
                raise UnsupportedFormat("extensible-array index link candidate budget exceeded")
            absolute = sb.base_address + address
            for row_index, row in enumerate(rows):
                if (signature == b"EADB" and row_index >= first_indirect
                        or signature == b"EASB" and row_index < first_indirect):
                    continue
                if signature == b"EASB":
                    pages_per_block = row.elements_per_block // page_elems if row.elements_per_block > page_elems else 0
                    bitmap_length = row.data_blocks * ((pages_per_block + 7) // 8) if pages_per_block else 0
                    length = block_prefix + bitmap_length + row.data_blocks * offsize + 4
                    position = indirect_start + (row_index - first_indirect) * offsize
                    expected_offset = row.start
                    choices = ((position, expected_offset),)
                else:
                    paged = row.elements_per_block > page_elems
                    length = block_prefix + (0 if paged else row.elements_per_block * entry_size) + 4
                    choices = tuple((direct_start + (row.start_data_pointer + db) * offsize,
                                     row.start + (row.start_data_pointer + db) * row.elements_per_block)
                                    for db in range(row.data_blocks))
                if length > MAX_READ_BYTES or absolute + length > sb.eof_address or any(
                    absolute < end and start < absolute + length
                    for start, end, _ in reader.metadata_ranges
                ):
                    continue
                try:
                    child = reader.read_at(address, length)
                except FormatError:
                    continue
                if (child[:4] != signature or child[4] != 0 or child[5] != int(filtered)
                        or _uint(child[6:6+offsize]) != root
                        or lookup3(child[:-4]) != _uint(child[-4:])):
                    continue
                observed_offset = _uint(child[6+offsize:block_prefix])
                for position, expected_offset in choices:
                    if observed_offset != expected_offset or _uint(index[position:position+offsize]) == address:
                        continue
                    repaired = bytearray(index)
                    repaired[position:position+offsize] = address.to_bytes(offsize, "little")
                    if lookup3(repaired[:-4]) == wanted:
                        matches.append((bytes(repaired), position, address, candidate_kind))
                        if len(matches) > 1:
                            raise FormatError("extensible-array child-link reconstruction is ambiguous")
    if not matches:
        raise FormatError("extensible-array index checksum mismatch; no checked child restores one pointer")
    return matches[0]


def _reconstruct_secondary_data_link(
    reader: ModernH5File, sblock: bytes, *,
    root: int, filtered: bool, row: _SuperblockRow,
    pointer_start: int, entry_size: int, max_bits: int, page_bits: int,
) -> tuple[bytes, int, int]:
    """Restore one EASB address using its original checksum and EADB owner/row."""
    sb = reader.superblock
    offsize = sb.offset_size
    page_elems = 1 << page_bits
    paged = row.elements_per_block > page_elems
    block_prefix = 6 + offsize + (max_bits + 7) // 8
    data_size = block_prefix + (0 if paged else row.elements_per_block * entry_size) + 4
    if data_size > MAX_READ_BYTES:
        raise UnsupportedFormat("extensible-array data block exceeds bounded repair read")
    wanted = _uint(sblock[-4:])
    matches: list[tuple[bytes, int, int]] = []
    candidates_seen = 0
    for candidate in candidate_addresses(reader, b"EADB"):
        candidates_seen += 1
        if candidates_seen > MAX_REPAIR_CANDIDATES:
            raise UnsupportedFormat("extensible-array secondary-link candidate budget exceeded")
        absolute = sb.base_address + candidate
        if absolute + data_size > sb.eof_address or any(
            absolute < end and start < absolute + data_size
            for start, end, _ in reader.metadata_ranges
        ):
            continue
        block = reader.read_at(candidate, data_size)
        if (block[:4] != b"EADB" or block[4] != 0
                or block[5] != int(filtered)
                or _uint(block[6:6+offsize]) != root
                or lookup3(block[:-4]) != _uint(block[-4:])):
            continue
        offset = _uint(block[6+offsize:block_prefix])
        for db in range(row.data_blocks):
            position = pointer_start + db * offsize
            if (offset != row.start + db * row.elements_per_block
                    or _uint(sblock[position:position+offsize]) == candidate):
                continue
            repaired = bytearray(sblock)
            repaired[position:position+offsize] = candidate.to_bytes(offsize, "little")
            if lookup3(repaired[:-4]) == wanted:
                matches.append((bytes(repaired), position, candidate))
                if len(matches) > 1:
                    raise FormatError("extensible-array secondary data-link repair is ambiguous")
    if not matches:
        raise FormatError("extensible-array secondary checksum mismatch; no checked data block restores pointer")
    return matches[0]


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
    selected_header = reader.absolute(object_address)
    if not any(start == selected_header and kind == "selected object header"
               for start, _, kind in reader.metadata_ranges):
        raise FormatError("extensible-array layout lacks a selected object-header anchor")
    if not any(start <= layout_pointer_offset and layout_pointer_offset + offsize <= end
               for start, end, kind in reader.metadata_ranges
               if kind in ("selected object header", "object header continuation")):
        raise FormatError("extensible-array layout pointer is not inside selected object metadata")
    if _uint(reader._read_absolute(layout_pointer_offset, offsize)) != root:
        raise FormatError("selected layout pointer does not own extensible-array header")
    filtered = bool(filters)
    chunk_size_width = (min(lensize, 8) if layout_version == 5 else
                        min(8, 1 + ((chunk_bytes.bit_length() - 1 + 8) // 8))) if filtered else 0
    entry_size = offsize + (chunk_size_width + 4 if filtered else 0)
    header_size = 16 + 6 * lensize + offsize
    header = reader.read_at(root, header_size)
    if header[:4] != b"EAHD" or header[4] != 0:
        raise FormatError("extensible-array header signature or version is invalid")
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
    n_direct = 2 * (min_ptrs - 1)
    n_indirect = rows_count - first_indirect
    index_size = 10 + offsize + index_elems * entry_size + (n_direct + n_indirect) * offsize
    header_checksum_valid = lookup3(header[:-4]) == _uint(header[-4:])
    repairs: dict[int, bytes] = {}
    reconstructed_links: tuple[dict[str, object], ...] = ()
    if not header_checksum_valid:
        index_address = _reconstruct_header_index_link(
            reader, header, root, 12 + 6 * lensize,
            index_size, filtered)
        repaired = bytearray(header)
        repaired[12+6*lensize:12+6*lensize+offsize] = index_address.to_bytes(offsize, "little")
        repairs[root] = bytes(repaired)
        reconstructed_links = ({
            "kind": "eahd_to_eaib", "source_address": root,
            "target_address": index_address,
            "pointer_offset": header_pointer_offset,
            "parent_checksum_offset": reader.absolute(root) + header_size - 4,
            "parent_kind": "extensible-array header",
            "child_kind": "extensible-array index block",
            "resolution": "original EAHD checksum restored by one pointer; unique checked EAIB with EAHD back-pointer",
        },)
    state = _Reader(reader, root, repairs)
    header = state.meta(root, header_size, b"EAHD", "extensible-array header")
    if index_address == undefined:
        if (not allow_sparse or max_index_set or n_elements or n_dblocks or n_sblocks
                or _sblock_bytes or _dblock_bytes):
            raise UnsupportedFormat("extensible-array index block is unallocated")
        return ModernIndex("extensible_array", layout_version, object_address, root,
                           layout_pointer_offset, chunks, element_size, (),
                           reconstructed_links=reconstructed_links)

    rows: list[_SuperblockRow] = []
    start = start_pointer = 0
    for row in range(rows_count):
        nblocks = 1 << (row // 2)
        nelmts = min_elems << ((row + 1) // 2)
        rows.append(_SuperblockRow(start, start_pointer, nblocks, nelmts))
        start += nblocks * nelmts
        start_pointer += nblocks
    index_raw = reader.read_at(index_address, index_size)
    if (index_raw[:4] != b"EAIB" or index_raw[4] != 0 or
            index_raw[5] != int(filtered) or _uint(index_raw[6:6+offsize]) != root):
        raise FormatError("extensible-array index block client or header back-pointer is invalid")
    direct_start = 6 + offsize + index_elems * entry_size
    indirect_start = direct_start + n_direct * offsize
    if lookup3(index_raw[:-4]) != _uint(index_raw[-4:]):
        if reconstructed_links:
            raise FormatError("more than one extensible-array metadata link is damaged")
        repaired, pointer_pos, candidate, child_kind = _reconstruct_index_child_link(
            reader, index_raw, root=root,
            filtered=filtered, entry_size=entry_size, max_bits=max_bits,
            page_bits=page_bits, rows=rows, first_indirect=first_indirect,
            direct_start=direct_start, indirect_start=indirect_start)
        # The state reader stores the original EAIB's physical range while
        # returning its single checksum-restoring hypothetical pointer view.
        state.repairs[index_address] = repaired
        reconstructed_links = ({
            "kind": "eaib_to_child", "source_address": index_address,
            "target_address": candidate,
            "pointer_offset": reader.absolute(index_address) + pointer_pos,
            "parent_checksum_offset": reader.absolute(index_address) + index_size - 4,
            "parent_kind": "extensible-array index block", "child_kind": child_kind,
            "resolution": "original EAIB checksum restored by one pointer; unique checked child owner and row offset",
        },)
    index = state.meta(index_address, index_size, b"EAIB", "extensible-array index block")
    page_elems = 1 << page_bits

    block_cache: dict[int, tuple[bytes, int, int, int]] = {}
    page_cache: dict[int, bytes] = {}
    paged_allocations: dict[int, tuple[int, int]] = {}
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
            paged = row.elements_per_block > page_elems
            pages_per_block = row.elements_per_block // page_elems if paged else 0
            bitmap_bytes_per_block = (pages_per_block + 7) // 8 if paged else 0
            page_bitmap = b""
            if row_index < first_indirect:
                if paged:
                    raise UnsupportedFormat("paged direct extensible-array blocks have no secondary bitmap")
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
                sblock_prefix = 6 + offsize + (max_bits + 7)//8
                bitmap_length = row.data_blocks * bitmap_bytes_per_block
                sblock_size = sblock_prefix + bitmap_length + row.data_blocks * offsize + 4
                if sblock_address not in state.seen:
                    sblock_raw = reader.read_at(sblock_address, sblock_size)
                    if (sblock_raw[:4] != b"EASB" or sblock_raw[4] != 0
                            or sblock_raw[5] != int(filtered)
                            or _uint(sblock_raw[6:6+offsize]) != root
                            or _uint(sblock_raw[6+offsize:6+offsize+(max_bits+7)//8]) != row.start):
                        raise FormatError("extensible-array secondary block owner or row offset disagrees")
                    if lookup3(sblock_raw[:-4]) != _uint(sblock_raw[-4:]):
                        if reconstructed_links:
                            raise FormatError("more than one extensible-array metadata link is damaged")
                        repaired, repair_position, candidate = _reconstruct_secondary_data_link(
                            reader, sblock_raw,
                            root=root, filtered=filtered, row=row,
                            pointer_start=sblock_prefix+bitmap_length,
                            entry_size=entry_size, max_bits=max_bits, page_bits=page_bits)
                        state.repairs[sblock_address] = repaired
                        reconstructed_links = ({
                            "kind": "easb_to_eadb", "source_address": sblock_address,
                            "target_address": candidate,
                            "pointer_offset": reader.absolute(sblock_address)+repair_position,
                            "parent_checksum_offset": reader.absolute(sblock_address)+sblock_size-4,
                            "parent_kind": "extensible-array secondary block",
                            "child_kind": "extensible-array data block",
                            "resolution": "original EASB checksum restored by one pointer; unique checked EADB owner and row offset",
                        },)
                sblock = state.meta(sblock_address, sblock_size, b"EASB", "extensible-array secondary block")
                if (sblock[5] != int(filtered) or _uint(sblock[6:6+offsize]) != root
                        or _uint(sblock[6+offsize:6+offsize+(max_bits+7)//8]) != row.start):
                    raise FormatError("extensible-array secondary block owner or row offset disagrees")
                sblock_absolute = reader.absolute(sblock_address)
                chain.append({"kind": "eaib_to_easb", "parent_address": index_address,
                              "target_address": sblock_address,
                              "pointer_offset": reader.absolute(index_address)+sblock_slot})
                if paged:
                    bitmap_start = sblock_prefix
                    page_bitmap = sblock[bitmap_start:bitmap_start+bitmap_length]
                    used_bits = row.data_blocks * pages_per_block
                    if (used_bits % 8 and page_bitmap[used_bits // 8]
                            & ((1 << (8 - used_bits % 8)) - 1)) or any(
                                page_bitmap[(used_bits+7)//8:]):
                        raise FormatError("extensible-array page bitmap sets a reserved bit")
                pointer_slot = sblock_prefix + bitmap_length + db_index * offsize
                address = _uint(sblock[pointer_slot:pointer_slot+offsize])
                pointer_parent = sblock_address
                expected_block_offset = row.start + db_index * row.elements_per_block
                parent_kind = "easb_to_eadb"
            if address == undefined:
                if paged and _bitmap_any(page_bitmap, db_index * pages_per_block,
                                         pages_per_block):
                    raise FormatError("extensible-array page bitmap claims an absent data block")
                if allow_sparse:
                    continue
                raise UnsupportedFormat("extensible-array data block is unallocated")
            if n_dblocks < 1:
                raise FormatError("extensible-array header denies observed data block")
            chain.append({"kind": parent_kind, "parent_address": pointer_parent,
                          "target_address": address,
                          "pointer_offset": reader.absolute(pointer_parent)+pointer_slot})
            block_prefix = 6 + offsize + (max_bits+7)//8
            block_size = block_prefix + (0 if paged else row.elements_per_block*entry_size) + 4
            if address in block_cache:
                _, _, capacity, prior_offset = block_cache[address]
                if (capacity, prior_offset) != (row.elements_per_block, expected_block_offset):
                    raise FormatError("extensible-array data block is claimed by two different rows")
            else:
                block = state.meta(address, block_size, b"EADB", "extensible-array data block")
                if (block[5] != int(filtered) or _uint(block[6:6+offsize]) != root
                        or _uint(block[6+offsize:6+offsize+(max_bits+7)//8]) != expected_block_offset):
                    raise FormatError("extensible-array data block owner or block offset disagrees")
                block_cache[address] = (block, 6+offsize+(max_bits+7)//8,
                                        row.elements_per_block, expected_block_offset)
            block, elements_start, _, _ = block_cache[address]
            if paged:
                page_size = page_elems * entry_size + 4
                block_span = block_size + pages_per_block * page_size
                if reader.absolute(address) + block_span > reader.superblock.eof_address:
                    raise FormatError("extensible-array data block pages cross declared file end")
                paged_allocations[address] = (
                    reader.absolute(address), reader.absolute(address) + block_span)
                page_index, page_slot = divmod(position % row.elements_per_block, page_elems)
                page_bit = db_index * pages_per_block + page_index
                if not _bitmap_bit(page_bitmap, page_bit):
                    if allow_sparse:
                        continue
                    raise UnsupportedFormat("extensible-array data block page is uninitialized")
                page_address = address + block_size + page_index * page_size
                if page_address not in page_cache:
                    page_cache[page_address] = state.page(page_address, page_size)
                slot_owner = page_cache[page_address]
                slot = page_slot * entry_size
                slot_address = page_address
                chain.append({"kind": "eadb_to_page", "parent_address": address,
                              "target_address": page_address,
                              "bitmap_offset": reader.absolute(sblock_address) + bitmap_start + page_bit // 8,
                              "bitmap_bit": page_bit,
                              "page_index": page_index,
                              "page_size": page_size,
                              "data_block_size": block_size})
            else:
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
             "reconstructed_links": list(reconstructed_links),
             "slot_owner_address": slot_address,
             "slot_owner_kind": ("extensible-array data block page" if paged else
                                 "extensible-array data block") if linear >= index_elems else
                                "extensible-array index block",
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
        if any(low < end and start < high for start, end in paged_allocations.values()):
            raise FormatError("extensible-array chunk overlaps a paged data block allocation")
    allocations = sorted(paged_allocations.values())
    if any(left[1] > right[0] for left, right in zip(allocations, allocations[1:])):
        raise FormatError("extensible-array paged data block allocations overlap")
    for low, high in allocations:
        if any(low < end and start < high
               and not (kind in ("extensible-array data block",
                                  "extensible-array data block page")
                        and low <= start and end <= high)
               for start, end, kind in reader.metadata_ranges):
            raise FormatError("extensible-array paged allocation overlaps other metadata")
    reader.metadata_ranges.extend((low, high, "extensible-array paged allocation")
                                  for low, high in allocations)
    return ModernIndex("extensible_array", layout_version, object_address, root,
                       layout_pointer_offset, chunks, element_size, tuple(records),
                       data_block_address=index_address,
                       data_block_pointer_offset=header_pointer_offset,
                       reconstructed_links=reconstructed_links)
