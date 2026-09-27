"""Bounded, read-only parsing of three modern HDF5 chunk index formats.

The selected object's address must come from a local hard-link lookup on the
same stable source snapshot. We validate the v2/v3 superblock, selected v2
object header, and its v4/v5 chunked layout message. Single-chunk, implicit,
and nonpaged, unfiltered fixed-array indexing have coordinate attribution. A signature
found by scanning unowned bytes is never treated as evidence of ownership.

Specification: https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
sections II.A, IV.A.1.b, IV.A.3.i, IV.A.3.q, VII.A, VII.B, and VII.C.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from math import prod
from pathlib import Path
from typing import BinaryIO

from .format import FormatError, SIGNATURE, UnsupportedFormat


MAX_HEADER_BYTES = 1 << 20
MAX_CONTINUATIONS = 4
MAX_CHUNKS = 8192
MAX_CHUNK_BYTES = 16 << 20
MAX_READ_BYTES = 16 << 20
_MASK = (1 << 32) - 1


def _rot(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (32 - bits))) & _MASK


def lookup3(data: bytes) -> int:
    """HDF5's little-endian Jenkins lookup3 metadata checksum, seed zero.

    This is kept independent of the installed HDF5 library so metadata
    corruption cannot be accepted merely because native lookup succeeded.
    """
    a = b = c = (0xDEADBEEF + len(data)) & _MASK
    pos = 0
    while len(data) - pos > 12:
        a = (a + int.from_bytes(data[pos : pos + 4], "little")) & _MASK
        b = (b + int.from_bytes(data[pos + 4 : pos + 8], "little")) & _MASK
        c = (c + int.from_bytes(data[pos + 8 : pos + 12], "little")) & _MASK
        a = (a - c) & _MASK; a ^= _rot(c, 4); c = (c + b) & _MASK
        b = (b - a) & _MASK; b ^= _rot(a, 6); a = (a + c) & _MASK
        c = (c - b) & _MASK; c ^= _rot(b, 8); b = (b + a) & _MASK
        a = (a - c) & _MASK; a ^= _rot(c, 16); c = (c + b) & _MASK
        b = (b - a) & _MASK; b ^= _rot(a, 19); a = (a + c) & _MASK
        c = (c - b) & _MASK; c ^= _rot(b, 4); b = (b + a) & _MASK
        pos += 12
    tail = data[pos:]
    if not tail:
        return c
    padded = tail.ljust(12, b"\x00")
    a = (a + int.from_bytes(padded[:4], "little")) & _MASK
    b = (b + int.from_bytes(padded[4:8], "little")) & _MASK
    c = (c + int.from_bytes(padded[8:12], "little")) & _MASK
    c ^= b; c = (c - _rot(b, 14)) & _MASK
    a ^= c; a = (a - _rot(c, 11)) & _MASK
    b ^= a; b = (b - _rot(a, 25)) & _MASK
    c ^= b; c = (c - _rot(b, 16)) & _MASK
    a ^= c; a = (a - _rot(c, 4)) & _MASK
    b ^= a; b = (b - _rot(a, 14)) & _MASK
    c ^= b; c = (c - _rot(b, 24)) & _MASK
    return c


def _uint(raw: bytes) -> int:
    return int.from_bytes(raw, "little")


@dataclass(frozen=True)
class ModernSuperblock:
    version: int
    signature_offset: int
    base_address: int
    offset_size: int
    length_size: int
    eof_address: int
    root_object_address: int

    @property
    def undefined_address(self) -> int:
        return (1 << (8 * self.offset_size)) - 1


@dataclass(frozen=True)
class ModernChunk:
    coordinate: tuple[int, ...]
    address: int
    size: int
    filter_mask: int
    evidence: dict[str, object]
    pointer_offset: int | None = None


@dataclass(frozen=True)
class ModernIndex:
    index_type: str
    layout_version: int
    object_address: int
    base_address: int
    layout_pointer_offset: int
    chunk_shape: tuple[int, ...]
    element_size: int
    chunks: tuple[ModernChunk, ...]
    data_block_address: int | None = None
    data_block_pointer_offset: int | None = None


class ModernH5File:
    """A read-only, file-size and declared-EOF bounded modern-format reader."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file: BinaryIO = self.path.open("rb")
        try:
            self.size = self._file.seek(0, 2)
            self.superblock = self._parse_superblock()
            sb = self.superblock
            self.metadata_ranges: list[tuple[int, int, str]] = [
                (sb.signature_offset, sb.signature_offset + 16 + 4 * sb.offset_size,
                 "modern superblock")
            ]
        except BaseException:
            self._file.close()
            raise

    def __enter__(self) -> ModernH5File:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._file.close()

    def _read_absolute(self, offset: int, length: int) -> bytes:
        if self._file.closed:
            raise ValueError("source file is closed")
        if length < 0 or length > MAX_READ_BYTES:
            raise UnsupportedFormat("modern metadata or raw single-read limit exceeded")
        if offset < 0 or offset > self.size or length > self.size - offset:
            raise FormatError("read outside source file")
        self._file.seek(offset)
        raw = self._file.read(length)
        if len(raw) != length:
            raise FormatError("short read from source")
        return raw

    def absolute(self, address: int) -> int:
        sb = self.superblock
        if not isinstance(address, int) or address < 0 or address == sb.undefined_address:
            raise FormatError("undefined or invalid relative address")
        offset = sb.base_address + address
        if offset >= sb.eof_address or offset >= self.size:
            raise FormatError("relative address is outside HDF5 data")
        return offset

    def read_at(self, address: int, length: int) -> bytes:
        absolute = self.absolute(address)
        if length < 0 or length > self.superblock.eof_address - absolute:
            raise FormatError("read crosses declared HDF5 end-of-file")
        return self._read_absolute(absolute, length)

    def _parse_superblock(self) -> ModernSuperblock:
        signature_offset = 0
        while signature_offset < self.size:
            if self.size - signature_offset >= 8 and self._read_absolute(signature_offset, 8) == SIGNATURE:
                break
            signature_offset = 512 if signature_offset == 0 else signature_offset * 2
        else:
            raise FormatError("HDF5 signature not found at a permitted offset")
        prefix = self._read_absolute(signature_offset, 12)
        version, offsize, lensize, flags = prefix[8:12]
        if version not in (2, 3):
            raise UnsupportedFormat(f"modern parser requires superblock version 2 or 3, found {version}")
        if offsize not in (2, 4, 8) or lensize not in (2, 4, 8):
            raise UnsupportedFormat("unsupported modern superblock offset or length size")
        if version == 3 and flags:
            raise UnsupportedFormat("superblock write-access/SWMR or unknown consistency flags are set")
        length = 16 + 4 * offsize
        raw = self._read_absolute(signature_offset, length)
        if lookup3(raw[:-4]) != _uint(raw[-4:]):
            raise FormatError("modern superblock checksum mismatch")
        base = _uint(raw[12 : 12 + offsize])
        extension = _uint(raw[12 + offsize : 12 + 2 * offsize])
        eof = _uint(raw[12 + 2 * offsize : 12 + 3 * offsize])
        root = _uint(raw[12 + 3 * offsize : 12 + 4 * offsize])
        undefined = (1 << (8 * offsize)) - 1
        if base != signature_offset:
            raise UnsupportedFormat("relocated modern superblock base is unsupported")
        if eof <= signature_offset + length or eof > self.size:
            raise FormatError("modern superblock end-of-file is inconsistent with source")
        if root == undefined or base + root >= eof:
            raise FormatError("modern superblock root group address is invalid")
        if extension != undefined and base + extension >= eof:
            raise FormatError("modern superblock extension address is invalid")
        return ModernSuperblock(version, signature_offset, base, offsize, lensize, eof, root)

    def _parse_object_layout(self, object_address: int) -> tuple[bytes, int]:
        start = self.absolute(object_address)
        prefix = self.read_at(object_address, 6)
        if prefix[:4] != b"OHDR" or prefix[4] != 2:
            raise UnsupportedFormat("selected object does not have a version-2 object header")
        flags = prefix[5]
        if flags & 0xC0:
            raise UnsupportedFormat("reserved version-2 object header flags set")
        extra = (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0)
        size_width = 1 << (flags & 0x03)
        size_raw = self.read_at(object_address + 6 + extra, size_width)
        data_length = _uint(size_raw)
        prefix_length = 6 + extra + size_width
        if data_length < 4 or data_length > MAX_HEADER_BYTES:
            raise UnsupportedFormat("selected object header chunk size is out of scope")
        chunk = self.read_at(object_address, prefix_length + data_length + 4)
        if lookup3(chunk[:-4]) != _uint(chunk[-4:]):
            raise FormatError("selected object header checksum mismatch")
        self.metadata_ranges.append((start, start + len(chunk), "selected object header"))
        queue: list[tuple[bytes, int]] = [
            (chunk[prefix_length:-4], start + prefix_length)
        ]
        seen_continuations: set[int] = set()
        layout: bytes | None = None
        layout_offset: int | None = None
        continuation_count = 0
        while queue:
            contents, contents_absolute = queue.pop(0)
            cursor = 0
            message_prefix = 6 if flags & 0x04 else 4
            while len(contents) - cursor >= message_prefix:
                kind = contents[cursor]
                size = _uint(contents[cursor + 1 : cursor + 3])
                msg_flags = contents[cursor + 3]
                cursor += message_prefix
                if size > len(contents) - cursor:
                    raise FormatError("version-2 object header message crosses chunk boundary")
                data_start = contents_absolute + cursor
                data = contents[cursor : cursor + size]
                cursor += size
                if kind == 0x08:
                    if layout is not None:
                        raise FormatError("selected object has duplicate layout messages")
                    if msg_flags & 0x22:
                        raise UnsupportedFormat("shared or marked-unknown layout message")
                    layout = data
                    layout_offset = data_start
                elif kind == 0x10:
                    if msg_flags & 0x22:
                        raise UnsupportedFormat("shared or marked-unknown object header continuation")
                    if size != self.superblock.offset_size + self.superblock.length_size:
                        raise FormatError("invalid object header continuation length")
                    continuation_count += 1
                    if continuation_count > MAX_CONTINUATIONS:
                        raise UnsupportedFormat("too many object header continuations")
                    offsize = self.superblock.offset_size
                    target = _uint(data[:offsize])
                    target_length = _uint(data[offsize:])
                    if target in seen_continuations or target == object_address:
                        raise FormatError("cyclic or repeated object header continuation")
                    seen_continuations.add(target)
                    if target_length < 8 or target_length > MAX_HEADER_BYTES:
                        raise UnsupportedFormat("continuation block size is out of scope")
                    absolute = self.absolute(target)
                    for meta_start, meta_end, _kind in self.metadata_ranges:
                        if absolute < meta_end and meta_start < absolute + target_length:
                            raise FormatError("object header continuation overlaps known metadata")
                    block = self.read_at(target, target_length)
                    if block[:4] != b"OCHK" or lookup3(block[:-4]) != _uint(block[-4:]):
                        raise FormatError("object header continuation signature or checksum invalid")
                    self.metadata_ranges.append((absolute, absolute + target_length,
                                                 "object header continuation"))
                    queue.append((block[4:-4], absolute + 4))
            if any(contents[cursor:]):
                raise FormatError("nonzero gap at end of object header chunk")
        if layout is None or layout_offset is None:
            raise FormatError("selected object has no layout message")
        return layout, layout_offset

    def _read_fixed_array(
        self, root: int, count: int, chunk_bytes: int,
        coordinates: tuple[tuple[int, ...], ...], layout_version: int,
        object_address: int, layout_pointer_offset: int,
        chunks: tuple[int, ...], element_size: int, layout_page_bits: int,
    ) -> ModernIndex:
        """Validate FAHD -> FADB -> each unfiltered, nonpaged chunk pointer."""
        sb = self.superblock
        header_length = 12 + sb.length_size + sb.offset_size
        header = self.read_at(root, header_length)
        if header[:4] != b"FAHD" or header[4] != 0:
            raise FormatError("fixed-array header signature or version is invalid")
        if header[5] != 0:
            raise UnsupportedFormat("filtered or unknown fixed-array client is unsupported")
        entry_size, page_bits = header[6], header[7]
        if entry_size != sb.offset_size or page_bits > 31:
            raise UnsupportedFormat("fixed-array entry size or page setting is unsupported")
        if page_bits != layout_page_bits:
            raise FormatError("fixed-array page bits disagree with selected layout")
        declared_count = _uint(header[8 : 8 + sb.length_size])
        if declared_count != count:
            raise FormatError("fixed-array capacity disagrees with dataset chunk grid")
        if count > (1 << page_bits):
            raise UnsupportedFormat("paged fixed arrays are not implemented")
        block_pointer = 8 + sb.length_size
        data_block = _uint(header[block_pointer : block_pointer + sb.offset_size])
        if data_block == sb.undefined_address:
            raise UnsupportedFormat("fixed-array data block is not allocated")
        if lookup3(header[:-4]) != _uint(header[-4:]):
            raise FormatError("fixed-array header checksum mismatch")
        header_absolute = self.absolute(root)
        block_absolute = self.absolute(data_block)
        # FADB: signature + version + client + back-pointer + entries + checksum.
        block_length = 10 + sb.offset_size + count * entry_size
        if block_length > MAX_READ_BYTES:
            raise UnsupportedFormat("fixed-array data block exceeds read limit")
        if header_absolute < block_absolute + block_length and block_absolute < header_absolute + header_length:
            raise FormatError("fixed-array header overlaps its data block")
        block = self.read_at(data_block, block_length)
        if block[:4] != b"FADB" or block[4:6] != b"\x00\x00":
            raise FormatError("fixed-array data block signature, version, or client is invalid")
        if _uint(block[6 : 6 + sb.offset_size]) != root:
            raise FormatError("fixed-array data block back-pointer disagrees with header")
        if lookup3(block[:-4]) != _uint(block[-4:]):
            raise FormatError("fixed-array data block checksum mismatch")
        self.metadata_ranges.extend((
            (header_absolute, header_absolute + header_length, "fixed-array header"),
            (block_absolute, block_absolute + block_length, "fixed-array data block"),
        ))
        records: list[ModernChunk] = []
        entries_start = 6 + sb.offset_size
        for linear, coordinate in enumerate(coordinates):
            entry = entries_start + linear * entry_size
            address = _uint(block[entry : entry + sb.offset_size])
            if address == sb.undefined_address:
                raise UnsupportedFormat("sparse fixed array has unallocated chunks")
            absolute = self.absolute(address)
            if chunk_bytes > sb.eof_address - absolute:
                raise FormatError("fixed-array payload crosses declared end-of-file")
            records.append(ModernChunk(
                coordinate, address, chunk_bytes, 0,
                {"rule": "validated fixed-array slot at row-major grid position",
                 "object_address": object_address, "layout_version": layout_version,
                 "index_type": "fixed_array", "linear_index": linear,
                 "fixed_array_header_address": root, "fixed_array_data_block_address": data_block},
                pointer_offset=block_absolute + entry,
            ))
        return ModernIndex(
            "fixed_array", layout_version, object_address, root,
            layout_pointer_offset, chunks, element_size, tuple(records),
            data_block_address=data_block,
            data_block_pointer_offset=header_absolute + block_pointer,
        )

    def read_index(
        self, object_address: int, shape: tuple[int, ...], chunks: tuple[int, ...],
        element_size: int, *, maxshape: tuple[int, ...] | None = None,
        filters: tuple[int, ...] = (), max_chunks: int = MAX_CHUNKS,
    ) -> ModernIndex:
        """Derive chunk addresses from anchored single, implicit, or fixed indexes.

        The caller must obtain object_address and all dataset properties from
        the same immutable source snapshot. Metadata from a different file is
        insufficient ownership evidence.
        """
        shape = tuple(shape); chunks = tuple(chunks)
        if maxshape is None:
            raise UnsupportedFormat("modern index requires observed maximum shape")
        if len(shape) not in (1, 2, 3, 4) or len(shape) != len(chunks) or len(maxshape) != len(shape):
            raise UnsupportedFormat("modern index requires rank 1 through 4 and matching dimensions")
        if not all(isinstance(x, int) and x > 0 for x in (*shape, *chunks, element_size)):
            raise FormatError("invalid dataset shape, chunk shape, or element size")
        if any(length % chunk for length, chunk in zip(shape, chunks)):
            raise UnsupportedFormat("partial edge chunks require a separate validation route")
        grid = tuple(length // chunk for length, chunk in zip(shape, chunks))
        count = prod(grid)
        if max_chunks < 1 or max_chunks > MAX_CHUNKS or count > max_chunks:
            raise UnsupportedFormat("modern index chunk count exceeds limit")
        chunk_bytes = prod(chunks) * element_size
        if chunk_bytes > MAX_CHUNK_BYTES:
            raise UnsupportedFormat("modern index chunk size exceeds limit")
        data, layout_offset = self._parse_object_layout(object_address)
        if len(data) < 6 or data[0] not in (4, 5) or data[1] != 2:
            raise UnsupportedFormat("selected dataset has no version-4/5 chunked layout")
        version, flags, ndim, width = data[0], data[2], data[3], data[4]
        if flags & ~0x03 or ndim != len(shape) + 1 or width not in (1, 2, 4, 8):
            raise UnsupportedFormat("unsupported modern chunked layout flags or dimensions")
        offset = 5
        end_dims = offset + ndim * width
        if len(data) < end_dims + 1 + self.superblock.offset_size:
            raise FormatError("truncated modern chunked layout message")
        raw_dims = tuple(_uint(data[i:i+width]) for i in range(offset, end_dims, width))
        if raw_dims != (*chunks, element_size):
            raise FormatError("modern raw chunk dimensions disagree with selected dataset")
        kind = data[end_dims]
        offset = end_dims + 1
        if kind == 1:
            if shape != chunks or tuple(maxshape) != shape or count != 1:
                raise FormatError("single chunk index disagrees with dataset extents")
            if flags & 0x02:
                if not filters or len(data) < offset + self.superblock.length_size + 4 + self.superblock.offset_size:
                    raise FormatError("single chunk filtered metadata contradicts pipeline or is truncated")
                size = _uint(data[offset : offset + self.superblock.length_size]); offset += self.superblock.length_size
                mask = _uint(data[offset : offset + 4]); offset += 4
                if mask >> len(filters):
                    raise FormatError("single chunk index mask exceeds filter pipeline")
            else:
                if filters:
                    raise FormatError("filtered single chunk has no stored size or mask")
                size, mask = chunk_bytes, 0
            name = "single_chunk"
        elif kind == 2:
            if flags & 0x02 or filters or tuple(maxshape) != shape:
                raise UnsupportedFormat("implicit index requires fixed extents and no filters")
            size, mask = chunk_bytes, 0
            name = "implicit"
        elif kind == 3:
            if flags & 0x02 or filters or tuple(maxshape) != shape:
                raise UnsupportedFormat("fixed array requires fixed extents and no filters")
            if len(data) < offset + 1 + self.superblock.offset_size:
                raise FormatError("fixed-array layout lacks page setting or address")
            layout_page_bits = data[offset]
            offset += 1
            size, mask = chunk_bytes, 0
            name = "fixed_array"
        else:
            raise UnsupportedFormat(f"modern chunk index type {kind} is not implemented")
        if len(data) != offset + self.superblock.offset_size:
            raise FormatError("modern chunked layout has trailing or missing bytes")
        base = _uint(data[offset:])
        if base == self.superblock.undefined_address:
            raise UnsupportedFormat("modern chunk storage has not been allocated")
        if size < 1 or size > MAX_CHUNK_BYTES:
            raise UnsupportedFormat("modern stored chunk size exceeds limit")
        coordinates = tuple(tuple(i * c for i, c in zip(index, chunks))
                            for index in product(*(range(length) for length in grid)))
        if kind == 3:
            fixed = self._read_fixed_array(
                base, count, chunk_bytes, coordinates, version,
                object_address, layout_offset + offset, chunks, element_size,
                layout_page_bits,
            )
            for record in fixed.chunks:
                start = self.absolute(record.address)
                for meta_start, meta_end, meta_kind in self.metadata_ranges:
                    if start < meta_end and meta_start < start + record.size:
                        raise FormatError(f"fixed-array chunk overlaps parsed {meta_kind}")
            return fixed
        physical = self.absolute(base)
        total = size if kind == 1 else size * count
        if total > self.superblock.eof_address - physical:
            raise FormatError("modern indexed chunks extend past HDF5 end-of-file")
        records: list[ModernChunk] = []
        for linear, coordinate in enumerate(coordinates):
            address = base + (linear * size if kind == 2 else 0)
            records.append(ModernChunk(
                coordinate, address, size, mask,
                {"rule": "layout direct chunk address" if kind == 1 else
                         "layout base plus row-major grid offset",
                 "object_address": object_address, "layout_version": version,
                 "index_type": name, "linear_index": linear},
            ))
        for record in records:
            start = self.absolute(record.address)
            for meta_start, meta_end, meta_kind in self.metadata_ranges:
                if start < meta_end and meta_start < start + record.size:
                    raise FormatError(f"modern chunk overlaps parsed {meta_kind}")
        return ModernIndex(name, version, object_address, base,
                           layout_offset + offset, chunks, element_size,
                           tuple(records))
