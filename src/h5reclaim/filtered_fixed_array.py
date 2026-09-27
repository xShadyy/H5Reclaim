"""Validate filtered and paged HDF5 fixed-array chunk indexes.

The layout pointer must be owned by the selected checksum-validated object
header in the *same snapshot*. FAHD and FADB are checksum-validated; paged
FADB uses its initialization bitmap and the checksum of each used page.
Uninitialized pages and undefined chunk slots are unknown, never fill values.

Format reference: HDF5 File Format Specification 4.0, section VII.C:
https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
"""

from __future__ import annotations

from itertools import product
from math import prod

from .format import FormatError, UnsupportedFormat
from .modern_indexes import (
    MAX_CHUNK_BYTES, MAX_CHUNKS, MAX_READ_BYTES, ModernChunk, ModernH5File,
    ModernIndex, lookup3,
)


MAX_REPAIR_SCAN_BYTES = 512 << 20
REPAIR_SCAN_BLOCK = 4 << 20


def _uint(raw: bytes) -> int:
    return int.from_bytes(raw, "little")


def _reconstruct_data_block_pointer(
    reader: ModernH5File, header: bytes, root: int, pointer: int,
    count: int, entry_size: int, layout_page_bits: int, filtered: bool,
) -> int:
    """Bridge one damaged FAHD address only if its original checksum agrees.

    FADB carries the FAHD address as an independent back-pointer. A candidate
    must also have its own valid checksum, and substituting its *address* into
    FAHD must restore the on-disk FAHD checksum without changing any other
    byte. Search is bounded and two matching candidates make the repair
    ambiguous. A checksum field or a second header field damaged as well
    therefore cannot be repaired by this route.
    """
    sb = reader.superblock
    if reader.size > MAX_REPAIR_SCAN_BYTES:
        raise UnsupportedFormat("fixed-array pointer reconstruction scan exceeds 512 MiB limit")
    offsize = sb.offset_size
    page_capacity = 1 << layout_page_bits
    paged = count > page_capacity
    pages = (count + page_capacity - 1) // page_capacity if paged else 0
    bitmap_bytes = (pages + 7) // 8
    prefix_size = 10 + offsize + bitmap_bytes
    block_size = prefix_size + (count * entry_size + pages * 4 if paged else count * entry_size)
    if block_size > MAX_READ_BYTES:
        raise UnsupportedFormat("fixed-array data block exceeds bounded read limit")
    wanted_checksum = _uint(header[-4:])
    matches: list[int] = []
    previous = b""
    for start in range(0, min(reader.size, sb.eof_address), REPAIR_SCAN_BLOCK):
        data = reader._read_absolute(start, min(REPAIR_SCAN_BLOCK, sb.eof_address - start))
        window = previous + data
        base = start - len(previous)
        at = window.find(b"FADB")
        while at >= 0:
            absolute = base + at
            candidate = absolute - sb.base_address
            if (0 <= candidate < 1 << (8 * offsize)
                    and absolute + block_size <= sb.eof_address
                    and not any(absolute < end and begin < absolute + block_size
                                for begin, end, _kind in reader.metadata_ranges)):
                repaired = bytearray(header)
                repaired[pointer:pointer+offsize] = candidate.to_bytes(offsize, "little")
                if lookup3(repaired[:-4]) == wanted_checksum:
                    raw = reader._read_absolute(absolute, prefix_size if paged else block_size)
                    if (raw[:4] == b"FADB" and raw[4] == 0 and raw[5] == int(filtered)
                            and _uint(raw[6:6+offsize]) == root
                            and lookup3(raw[:-4]) == _uint(raw[-4:])):
                        matches.append(candidate)
                        if len(matches) > 1:
                            raise FormatError("fixed-array pointer reconstruction is ambiguous")
            at = window.find(b"FADB", at + 1)
        previous = window[-3:]
    if not matches:
        raise FormatError("fixed-array header checksum mismatch; no uniquely verified data-block pointer")
    return matches[0]


def read_fixed_array_variants(
    reader: ModernH5File,
    root: int,
    count: int,
    chunk_bytes: int,
    coordinates: tuple[tuple[int, ...], ...],
    layout_version: int,
    object_address: int,
    layout_pointer_offset: int,
    chunks: tuple[int, ...],
    element_size: int,
    layout_page_bits: int,
    *,
    shape: tuple[int, ...],
    filters: tuple[int, ...],
    allow_sparse: bool = False,
    max_chunks: int = MAX_CHUNKS,
) -> ModernIndex:
    """Return only verified, allocated chunks with literal FA slot pointers.

    The caller obtains ``root``, dimensions, and filters from the selected
    dataset's metadata on ``reader``. This reader independently checks that
    the layout pointer lies in that object's validated header and still names
    the FAHD. Page addresses are computed from the contiguous FADB allocation,
    not invented as literal links. ``allow_sparse=False`` refuses missing
    slots/pages; with it, absent slots are omitted from ``ModernIndex.chunks``.
    """
    sb = reader.superblock
    offsize, lensize = sb.offset_size, sb.length_size
    filters, shape, chunks = tuple(filters), tuple(shape), tuple(chunks)
    if layout_version not in (4, 5) or not 1 <= len(chunks) <= 4:
        raise UnsupportedFormat("fixed array requires a version-4/5 layout of rank one through four")
    if (not isinstance(count, int) or count < 1 or count > min(MAX_CHUNKS, max_chunks)
            or count != len(coordinates)):
        raise UnsupportedFormat("fixed-array chunk capacity exceeds parser limit or coordinate grid")
    if (not isinstance(chunk_bytes, int) or chunk_bytes != prod(chunks) * element_size
            or chunk_bytes < 1 or chunk_bytes > MAX_CHUNK_BYTES):
        raise FormatError("fixed-array chunk byte size contradicts observed shape and datatype")
    if len(shape) != len(chunks) or any(not isinstance(value, int) or value <= 0
                                        for value in (*shape, *chunks, element_size)):
        raise FormatError("invalid fixed-array dataset dimensions")
    grid = tuple((extent + width - 1) // width for extent, width in zip(shape, chunks))
    if count != prod(grid):
        raise FormatError("fixed-array capacity disagrees with selected row-major grid")
    expected = tuple(tuple(i * width for i, width in zip(cell, chunks))
                     for cell in product(*(range(extent) for extent in grid)))
    if coordinates != expected:
        raise FormatError("fixed-array coordinates disagree with selected row-major grid")
    if not isinstance(layout_page_bits, int) or not 0 <= layout_page_bits <= 31:
        raise UnsupportedFormat("fixed-array page bits exceed parser limit")
    if len(filters) > 32:
        raise UnsupportedFormat("fixed-array filter mask supports at most 32 filters")
    filtered = bool(filters)
    size_width = (min(8, 1 + (chunk_bytes.bit_length() + 7) // 8)
                  if layout_version == 4 else offsize) if filtered else 0
    entry_size = offsize + (size_width + 4 if filtered else 0)
    if entry_size > 255:
        raise UnsupportedFormat("fixed-array entry width exceeds on-disk representation")

    selected_header = reader.absolute(object_address)
    if not any(start == selected_header and kind == "selected object header"
               for start, _end, kind in reader.metadata_ranges):
        raise FormatError("fixed-array selected object header has not been validated")
    if not any(start <= layout_pointer_offset and layout_pointer_offset + offsize <= end
               and kind in ("selected object header", "object header continuation")
               for start, end, kind in reader.metadata_ranges):
        raise FormatError("fixed-array layout pointer is outside validated selected metadata")
    if _uint(reader._read_absolute(layout_pointer_offset, offsize)) != root:
        raise FormatError("fixed-array layout pointer disagrees with selected index")

    seen_ranges: list[tuple[int, int, str]] = []

    def reserve(address: int, length: int, label: str) -> int:
        start = reader.absolute(address)
        end = start + length
        if length < 1 or length > MAX_READ_BYTES or end > sb.eof_address:
            raise UnsupportedFormat("fixed-array metadata size exceeds bounded source")
        for before, after, kind in (*reader.metadata_ranges, *seen_ranges):
            if start < after and before < end:
                raise FormatError(f"fixed-array metadata overlaps {kind}")
        seen_ranges.append((start, end, label))
        return start

    header_size = 12 + lensize + offsize
    header_absolute = reserve(root, header_size, "fixed-array header")
    header = reader.read_at(root, header_size)
    if header[:4] != b"FAHD" or header[4] != 0:
        raise FormatError("fixed-array header signature or version is invalid")
    if header[5] != int(filtered):
        raise FormatError("fixed-array header client contradicts selected filters")
    if header[6] != entry_size or header[7] != layout_page_bits:
        raise FormatError("fixed-array header entry width or page bits contradict selected layout")
    if _uint(header[8:8+lensize]) != count:
        raise FormatError("fixed-array header capacity contradicts selected chunk grid")
    pointer = 8 + lensize
    header_checksum_valid = lookup3(header[:-4]) == _uint(header[-4:])
    data_block = (_uint(header[pointer:pointer+offsize]) if header_checksum_valid else
                  _reconstruct_data_block_pointer(reader, header, root, pointer, count,
                                                  entry_size, layout_page_bits, filtered))
    if data_block == sb.undefined_address:
        if not allow_sparse:
            raise UnsupportedFormat("fixed-array data block is unallocated")
        reader.metadata_ranges.extend(seen_ranges)
        return ModernIndex("fixed_array", layout_version, object_address, root,
                           layout_pointer_offset, chunks, element_size, ())

    page_capacity = 1 << layout_page_bits
    paged = count > page_capacity
    pages = (count + page_capacity - 1) // page_capacity if paged else 0
    bitmap_bytes = (pages + 7) // 8
    prefix_size = 10 + offsize + bitmap_bytes if paged else 10 + offsize
    block_size = prefix_size + (count * entry_size + pages * 4 if paged else count * entry_size)
    if block_size > MAX_READ_BYTES:
        raise UnsupportedFormat("fixed-array data block exceeds bounded read limit")
    block_absolute = reserve(data_block, prefix_size if paged else block_size,
                             "fixed-array data block")
    block = reader.read_at(data_block, prefix_size if paged else block_size)
    if block[:4] != b"FADB" or block[4] != 0 or block[5] != int(filtered):
        raise FormatError("fixed-array data block signature, version, or client is invalid")
    if _uint(block[6:6+offsize]) != root:
        raise FormatError("fixed-array data block back-pointer contradicts header")
    if lookup3(block[:-4]) != _uint(block[-4:]):
        raise FormatError("fixed-array data block checksum mismatch")
    if block_absolute + block_size > sb.eof_address:
        raise FormatError("fixed-array pages exceed declared file end")
    bitmap = block[6+offsize:6+offsize+bitmap_bytes] if paged else b""
    if paged and pages % 8 and bitmap[-1] & ((1 << (8 - pages % 8)) - 1):
        raise FormatError("fixed-array initialization bitmap sets out-of-range pages")

    records: list[ModernChunk] = []
    payload_ranges: list[tuple[int, int]] = []
    for page in range(pages if paged else 1):
        first_index = page * page_capacity if paged else 0
        entries = min(page_capacity, count - first_index) if paged else count
        page_address = data_block + prefix_size + first_index * entry_size + page * 4
        initialized = not paged or bool(bitmap[page // 8] & (0x80 >> (page % 8)))
        if paged:
            kind = "fixed-array data block page" if initialized else "fixed-array uninitialized page"
            page_absolute = reserve(page_address, entries * entry_size + 4, kind)
        else:
            page_absolute = block_absolute
        if not initialized:
            if not allow_sparse:
                raise UnsupportedFormat("fixed-array page has not been initialized")
            continue
        page_raw = reader.read_at(page_address, entries * entry_size + 4) if paged else block
        if paged and lookup3(page_raw[:-4]) != _uint(page_raw[-4:]):
            raise FormatError("fixed-array data block page checksum mismatch")
        elements_offset = 0 if paged else 6 + offsize
        for index in range(entries):
            linear = first_index + index
            slot = elements_offset + index * entry_size
            address = _uint(page_raw[slot:slot+offsize])
            if address == sb.undefined_address:
                if filtered and any(page_raw[slot+offsize:slot+entry_size]):
                    raise FormatError("unallocated fixed-array slot contains size or filter bits")
                if not allow_sparse:
                    raise UnsupportedFormat("sparse fixed array has unallocated chunks")
                continue
            size = _uint(page_raw[slot+offsize:slot+offsize+size_width]) if filtered else chunk_bytes
            mask = _uint(page_raw[slot+offsize+size_width:slot+entry_size]) if filtered else 0
            if size < 1 or size > MAX_CHUNK_BYTES or mask >> len(filters):
                raise FormatError("fixed-array stored size or filter mask is invalid")
            absolute = reader.absolute(address)
            if size > sb.eof_address - absolute:
                raise FormatError("fixed-array payload exceeds declared file end")
            payload_ranges.append((absolute, absolute + size))
            link_path: list[dict[str, object]] = [
                {"kind": "layout_to_fahd", "parent_address": object_address,
                 "target_address": root, "pointer_offset": layout_pointer_offset},
                {"kind": "fahd_to_fadb", "parent_address": root,
                 "target_address": data_block, "pointer_offset": header_absolute + pointer},
                {"kind": "fixed_array_slot_to_chunk",
                 "parent_address": page_address if paged else data_block,
                 "target_address": address, "pointer_offset": page_absolute + slot},
            ]
            evidence: dict[str, object] = {
                "rule": "checksum-validated fixed-array slot at row-major grid position",
                "object_address": object_address,
                "layout_version": layout_version,
                "index_type": "fixed_array",
                "linear_index": linear,
                "fixed_array_header_address": root,
                "fixed_array_data_block_address": data_block,
                "slot_owner_address": page_address if paged else data_block,
                "slot_owner_kind": "fixed-array data block page" if paged else "fixed-array data block",
                "page_index": page if paged else None,
                "page_address_rule": "FADB contiguous allocation plus preceding page sizes"
                                     if paged else None,
                "computed_page": {
                    "address": page_address, "page_index": page,
                    "offset_from_data_block": page_address - data_block,
                    "bitmap_initialized": True,
                    "rule": "FADB prefix + preceding entries and 4-byte page checksums",
                } if paged else None,
                "metadata_checksums": {
                    "fahd": _uint(header[-4:]), "fadb": _uint(block[-4:]),
                    "page": _uint(page_raw[-4:]) if paged else None,
                    "validated": header_checksum_valid,
                    "fahd_original_checksum_valid": header_checksum_valid,
                    "fahd_checksum_restored_by_pointer_substitution": not header_checksum_valid,
                    "fahd_pointer_reconstructed": not header_checksum_valid,
                },
                "link_path": link_path,
            }
            records.append(ModernChunk(coordinates[linear], address, size, mask, evidence,
                                       pointer_offset=page_absolute + slot))
    payload_ranges.sort()
    if any(first[1] > second[0] for first, second in zip(payload_ranges, payload_ranges[1:])):
        raise FormatError("fixed-array chunk payloads overlap")
    for start, end in payload_ranges:
        for meta_start, meta_end, kind in (*reader.metadata_ranges, *seen_ranges):
            if start < meta_end and meta_start < end:
                raise FormatError(f"fixed-array chunk overlaps validated {kind}")
    reader.metadata_ranges.extend(seen_ranges)
    return ModernIndex("fixed_array", layout_version, object_address, root,
                       layout_pointer_offset, chunks, element_size, tuple(records),
                       data_block_address=data_block,
                       data_block_pointer_offset=header_absolute + pointer,
                       reconstructed_data_block_pointer=not header_checksum_valid)
