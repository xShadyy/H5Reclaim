"""Read-only, rooted modern metadata fallback for a selected local dataset.

This module is deliberately narrower than HDF5's object graph. It follows
compact, local hard links from a checksummed v2/v3 superblock root through
checksummed v2 object headers. It parses the selected dataset's own dataspace,
datatype, layout, and filter metadata before returning a DatasetSpec. A user
hint or an unreferenced OHDR signature cannot establish ownership.

Format: https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
sections II.A, IV.A.1.b, IV.A.3.b-d, IV.A.3.g, IV.A.3.i/l/q.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from hashlib import sha256
from math import prod
from pathlib import Path

from .format import FormatError, H5File, UnsupportedFormat
from .metadata import DatasetSpec
from .modern_indexes import ModernH5File, lookup3
from .schema_codec import FilterDescriptor


MAX_PATH_BYTES = 2048
MAX_DEPTH = 32
MAX_HEADER_BYTES = 1 << 20
MAX_CONTINUATIONS = 8
MAX_MESSAGES = 4096
MAX_FILTERS = 8
MAX_SOURCE_BYTES = 4 * (1 << 30)


def _uint(value: bytes) -> int:
    return int.from_bytes(value, "little")


@dataclass(frozen=True)
class HeaderMessage:
    kind: int
    flags: int
    data: bytes
    absolute_offset: int


@dataclass(frozen=True)
class LinkStep:
    group_address: int
    name: str
    object_address: int
    link_message_offset: int
    cached_group: tuple[int, int] | None = None


@dataclass(frozen=True)
class FallbackMetadata:
    spec: DatasetSpec
    source_sha256: str
    superblock_root: int
    link_chain: tuple[LinkStep, ...]
    metadata_ranges: tuple[tuple[int, int, str], ...]
    omitted_auxiliary_metadata: tuple[str, ...] = ()
    route: str = "checksummed_modern_compact_hard_links"


@dataclass(frozen=True)
class _OldHeap:
    segment: bytes
    free_ranges: tuple[tuple[int, int], ...]

    def name(self, offset: int, *, allow_empty: bool = False) -> str:
        if not 0 <= offset < len(self.segment):
            raise FormatError("symbol name offset outside local heap")
        end = self.segment.find(b"\x00", offset, min(len(self.segment), offset + MAX_PATH_BYTES + 1))
        if end < 0 or (not allow_empty and end == offset):
            raise FormatError("unterminated or empty symbol name")
        if any(offset < b and a < end + 1 for a, b in self.free_ranges):
            raise FormatError("symbol name overlaps local-heap free block")
        raw = self.segment[offset:end]
        try:
            name = raw.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise UnsupportedFormat("unsupported older group name encoding") from exc
        if "/" in name or name in (".", ".."):
            raise FormatError("invalid older group link name")
        return name


def _old_messages(reader: H5File, address: int) -> tuple[HeaderMessage, ...]:
    prefix = reader.read_at(address, 16)
    if prefix[0] != 1 or prefix[1] or prefix[12:16] != b"\x00" * 4:
        raise FormatError("rooted older object header prefix is invalid")
    count, size = _uint(prefix[2:4]), _uint(prefix[8:12])
    if not 1 <= count <= MAX_MESSAGES or not 8 <= size <= MAX_HEADER_BYTES:
        raise UnsupportedFormat("older object-header count or size exceeds limits")
    initial = reader.absolute(address)
    ranges = [(initial, initial + 16 + size, "rooted older object header")]
    queue = [(address + 16, size)]
    seen = {address}
    messages: list[HeaderMessage] = []
    while queue:
        block_address, length = queue.pop(0)
        content = reader.read_at(block_address, length)
        pos = 0
        while pos < length:
            if length - pos < 8:
                raise FormatError("truncated older object-header message")
            kind = _uint(content[pos:pos + 2])
            size = _uint(content[pos + 2:pos + 4])
            flags = content[pos + 4]
            if content[pos + 5:pos + 8] != b"\x00" * 3 or size % 8 or size > length - pos - 8:
                raise FormatError("invalid older object-header message length or reserved bytes")
            if flags & 0xA0:
                raise UnsupportedFormat("older message marked unknown or mandatory-unknown")
            start = pos + 8
            messages.append(HeaderMessage(kind, flags, content[start:start + size],
                                          reader.absolute(block_address) + start))
            if len(messages) > count:
                raise FormatError("older object header contains extra messages")
            pos = start + size
            if kind == 0x10:
                if flags & 2:
                    raise UnsupportedFormat("shared older continuation")
                osize, lsize = reader.superblock.offset_size, reader.superblock.length_size
                if size != osize + lsize or len(seen) > MAX_CONTINUATIONS:
                    raise UnsupportedFormat("unsupported older continuation structure")
                target = _uint(messages[-1].data[:osize]); target_size = _uint(messages[-1].data[osize:])
                if target in seen or not 8 <= target_size <= MAX_HEADER_BYTES or target_size % 8:
                    raise FormatError("invalid or repeated older continuation")
                seen.add(target)
                absolute = reader.absolute(target)
                if any(absolute < b and a < absolute + target_size for a, b, _ in ranges):
                    raise FormatError("overlapping older continuation")
                ranges.append((absolute, absolute + target_size, "rooted older continuation"))
                queue.append((target, target_size))
    if len(messages) != count:
        raise FormatError("older object-header message count mismatch")
    reader.metadata_ranges.extend(ranges)
    return tuple(messages)


def _old_root_address(reader: H5File) -> tuple[int, tuple[int, int] | None]:
    sb = reader.superblock
    prefix_size = (28 if sb.version == 1 else 24) + 4 * sb.offset_size
    osize, lsize = sb.offset_size, sb.length_size
    raw = reader._read_absolute(sb.signature_offset + prefix_size, lsize + osize + 24)
    nameoff = _uint(raw[:lsize]); root = _uint(raw[lsize:lsize + osize])
    cache_type = _uint(raw[lsize + osize:lsize + osize + 4])
    if nameoff != 0 or raw[lsize + osize + 4:lsize + osize + 8] != b"\x00" * 4:
        raise FormatError("invalid older root group symbol-table entry")
    if cache_type not in (0, 1):
        raise UnsupportedFormat("older root is not a local group")
    reader.absolute(root)
    cached = None
    if cache_type == 1:
        scratch = raw[lsize + osize + 8:]
        if osize > 8:
            raise UnsupportedFormat("older group scratch address exceeds limits")
        cached = (_uint(scratch[:osize]), _uint(scratch[osize:2*osize]))
        if any(scratch[2*osize:]):
            raise FormatError("older root group scratch padding is not zero")
    reader.metadata_ranges.append((sb.signature_offset + prefix_size,
                                   sb.signature_offset + prefix_size + len(raw),
                                   "older superblock root entry"))
    return root, cached


def _old_heap(reader: H5File, address: int) -> _OldHeap:
    osize, lsize = reader.superblock.offset_size, reader.superblock.length_size
    size = 8 + 2*lsize + osize
    raw = reader.read_at(address, size)
    if raw[:4] != b"HEAP" or raw[4] != 0 or any(raw[5:8]):
        raise FormatError("older local-heap signature or version invalid")
    n = _uint(raw[8:8+lsize]); head = _uint(raw[8+lsize:8+2*lsize]); data = _uint(raw[8+2*lsize:])
    if n < 8 or n > MAX_HEADER_BYTES:
        raise UnsupportedFormat("older local heap exceeds 1 MiB limit")
    segment = reader.read_at(data, n)
    reader.metadata_ranges.extend(((reader.absolute(address), reader.absolute(address)+size,
                                    "older local-heap header"),
                                   (reader.absolute(data), reader.absolute(data)+n,
                                    "older local-heap segment")))
    free: list[tuple[int, int]] = []
    seen: set[int] = set()
    undefined = (1 << (8*lsize)) - 1
    while head != undefined and head != 1:
        if head in seen or head + 2*lsize > len(segment):
            raise FormatError("cyclic or invalid older local-heap free list")
        seen.add(head)
        next_free = _uint(segment[head:head+lsize]); free_size = _uint(segment[head+lsize:head+2*lsize])
        if free_size < 2*lsize or head+free_size > len(segment):
            raise FormatError("invalid older local-heap free block length")
        if any(head < b and a < head + free_size for a, b in free):
            raise FormatError("overlapping older local-heap free blocks")
        free.append((head, head+free_size))
        head = next_free
        if len(free) > 4096:
            raise UnsupportedFormat("older local-heap free list exceeds limit")
    return _OldHeap(segment, tuple(free))


def _old_group(reader: H5File, address: int, cached: tuple[int, int] | None) -> dict[str, LinkStep | None]:
    messages = _old_messages(reader, address)
    stab = _unique(messages, 0x11)
    if stab is None:
        raise UnsupportedFormat("rooted older object is not a symbol-table group")
    osize = reader.superblock.offset_size
    if len(stab.data) != 2*osize:
        raise FormatError("invalid older group symbol-table message length")
    tree, heapaddr = _uint(stab.data[:osize]), _uint(stab.data[osize:])
    if cached is not None and cached != (tree, heapaddr):
        raise FormatError("older cached group addresses contradict object header")
    heap = _old_heap(reader, heapaddr)
    sb = reader.superblock
    leaf_k = _uint(reader._read_absolute(sb.signature_offset + 16, 2))
    int_k = _uint(reader._read_absolute(sb.signature_offset + 18, 2))
    if not 0 < int_k <= 32767 or not 0 < leaf_k <= 32767:
        raise UnsupportedFormat("older group B-tree K exceeds limits")
    osize, lsize = sb.offset_size, sb.length_size
    links: dict[str, LinkStep | None] = {}
    seen: set[int] = set()
    pending = [(tree, None, None, None)]
    while pending:
        node_address, parent_level, low, high = pending.pop()
        if node_address in seen or len(seen) >= 4096:
            raise FormatError("cyclic or excessive older group B-tree")
        seen.add(node_address)
        prefix = reader.read_at(node_address, 8 + 2*osize)
        if prefix[:4] != b"TREE" or prefix[4] != 0:
            raise FormatError("rooted older group B-tree signature or type invalid")
        level, used = prefix[5], _uint(prefix[6:8])
        if level > MAX_DEPTH or not 1 <= used <= 2*int_k or (
            parent_level is not None and level != parent_level - 1
        ):
            raise FormatError("older group tree level or entry count invalid")
        size = 8 + 2*osize + 2*int_k*(lsize+osize) + lsize
        if size > MAX_HEADER_BYTES:
            raise UnsupportedFormat("older group B-tree node exceeds 1 MiB")
        reader.read_at(node_address, size)
        reader.metadata_ranges.append((reader.absolute(node_address), reader.absolute(node_address)+size,
                                       "older group B-tree node"))
        content = reader.read_at(node_address + 8 + 2*osize, used*(lsize+osize)+lsize)
        keys = []
        targets = []
        for i in range(used+1):
            start = i*(lsize+osize)
            key = heap.name(_uint(content[start:start+lsize]), allow_empty=True)
            if keys and key <= keys[-1]:
                raise FormatError("older group B-tree keys are not ordered")
            keys.append(key)
            if i < used:
                child = _uint(content[start+lsize:start+lsize+osize]); reader.absolute(child)
                targets.append(child)
        if low is not None and keys[0] < low or high is not None and keys[-1] > high:
            raise FormatError("older group B-tree child exceeds parent name interval")
        for i, child in enumerate(targets):
            if level:
                pending.append((child, level, keys[i], keys[i+1]))
                continue
            first = reader.read_at(child, 8)
            if first[:4] != b"SNOD" or first[4] != 1 or first[5] != 0:
                raise FormatError("older symbol-node signature or version invalid")
            count = _uint(first[6:8])
            if not 1 <= count <= 2*leaf_k:
                raise FormatError("older symbol-node count invalid")
            entry_size = lsize + osize + 24
            allocated = 8 + 2*leaf_k*entry_size
            if allocated > MAX_HEADER_BYTES:
                raise UnsupportedFormat("older symbol node exceeds 1 MiB")
            reader.read_at(child, allocated)
            reader.metadata_ranges.append((reader.absolute(child), reader.absolute(child)+allocated,
                                           "older group symbol node"))
            raw_entries = reader.read_at(child + 8, count*entry_size)
            previous = None
            for j in range(count):
                entry = raw_entries[j*entry_size:(j+1)*entry_size]
                name = heap.name(_uint(entry[:lsize]))
                if (not keys[i] < name or name > keys[i+1]
                    or previous is not None and name <= previous):
                    raise FormatError("older group symbol name contradicts tree interval")
                previous = name
                target = _uint(entry[lsize:lsize+osize])
                cache_type = _uint(entry[lsize+osize:lsize+osize+4])
                if any(entry[lsize+osize+4:lsize+osize+8]) or cache_type not in (0, 1, 2):
                    raise FormatError("older symbol entry cache type or reserved bytes invalid")
                if name in links:
                    raise FormatError("duplicate older symbol-table link name")
                if cache_type == 2:
                    links[name] = None
                else:
                    reader.absolute(target)
                    cached_child = None
                    if cache_type == 1:
                        scratch = entry[lsize+osize+8:]
                        cached_child = (_uint(scratch[:osize]), _uint(scratch[osize:2*osize]))
                        if any(scratch[2*osize:]):
                            raise FormatError("older cached group scratch padding invalid")
                    links[name] = LinkStep(address, name, target,
                                           reader.absolute(child)+8+j*entry_size,
                                           cached_child)
    counts = Counter(step.object_address for step in links.values() if step is not None)
    for target, count in counts.items():
        if count < 2:
            continue
        prefix = reader.read_at(target, 16)
        if prefix[0] != 1 or _uint(prefix[4:8]) < count:
            raise FormatError("older local hard-link count contradicts target object header")
    return links


def _messages(reader: ModernH5File, address: int) -> tuple[HeaderMessage, ...]:
    """Validate each v2 object-header block before interpreting messages."""
    prefix = reader.read_at(address, 6)
    if prefix[:4] != b"OHDR" or prefix[4] != 2:
        raise UnsupportedFormat("rooted object does not have a v2 object header")
    flags = prefix[5]
    if flags & 0xC0:
        raise FormatError("reserved object-header flags")
    extra = (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0)
    width = 1 << (flags & 3)
    size = _uint(reader.read_at(address + 6 + extra, width))
    if size < 4 or size > MAX_HEADER_BYTES:
        raise UnsupportedFormat("object-header size exceeds parser limit")
    head_size = 6 + extra + width
    raw = reader.read_at(address, head_size + size + 4)
    if lookup3(raw[:-4]) != _uint(raw[-4:]):
        raise FormatError("object-header checksum mismatch")
    first = reader.absolute(address)
    ranges = [(first, first + len(raw), "rooted object header")]
    queue = [(raw[head_size:-4], first + head_size)]
    seen = {address}
    messages: list[HeaderMessage] = []
    while queue:
        content, base = queue.pop(0)
        pos = 0
        prefix_size = 6 if flags & 4 else 4
        while len(content) - pos >= prefix_size:
            kind = content[pos]
            length = _uint(content[pos + 1:pos + 3])
            msg_flags = content[pos + 3]
            if length > len(content) - pos - prefix_size:
                raise FormatError("object-header message crosses block boundary")
            if msg_flags & 0xA0:
                raise UnsupportedFormat("message marked unknown or mandatory-unknown")
            start = pos + prefix_size
            data = content[start:start + length]
            absolute = base + start
            messages.append(HeaderMessage(kind, msg_flags, data, absolute))
            if len(messages) > MAX_MESSAGES:
                raise UnsupportedFormat("too many object-header messages")
            pos = start + length
            if kind == 0x10:
                if msg_flags & 0x02:
                    raise UnsupportedFormat("shared object-header continuation")
                osize, lsize = reader.superblock.offset_size, reader.superblock.length_size
                if length != osize + lsize or len(seen) > MAX_CONTINUATIONS:
                    raise UnsupportedFormat("invalid or excessive object-header continuations")
                target = _uint(data[:osize]); count = _uint(data[osize:])
                if target in seen or not 8 <= count <= MAX_HEADER_BYTES:
                    raise FormatError("cyclic or invalid object-header continuation")
                seen.add(target)
                physical = reader.absolute(target)
                if any(physical < b and a < physical + count for a, b, _ in ranges):
                    raise FormatError("overlapping object-header continuation")
                block = reader.read_at(target, count)
                if block[:4] != b"OCHK" or lookup3(block[:-4]) != _uint(block[-4:]):
                    raise FormatError("object-header continuation signature or checksum mismatch")
                ranges.append((physical, physical + count, "rooted object header continuation"))
                queue.append((block[4:-4], physical + 4))
        if any(content[pos:]):
            raise FormatError("nonzero object-header gap")
    reader.metadata_ranges.extend(ranges)
    return tuple(messages)


def _unique(messages: tuple[HeaderMessage, ...], kind: int) -> HeaderMessage | None:
    selected = [m for m in messages if m.kind == kind]
    if len(selected) > 1:
        raise FormatError(f"duplicate object-header message type {kind}")
    if selected and selected[0].flags & 2:
        raise UnsupportedFormat(f"shared object-header message type {kind}")
    return selected[0] if selected else None


def _compact_links(reader: ModernH5File, address: int) -> dict[str, LinkStep | None]:
    messages = _messages(reader, address)
    info = _unique(messages, 2)
    _group = _unique(messages, 10)
    if info is None or _group is None:
        raise UnsupportedFormat("rooted object is not a compact modern group")
    raw = info.data
    if len(raw) < 2 or raw[0] != 0 or raw[1] & ~3:
        raise FormatError("invalid group link-info message")
    pos = 2 + (8 if raw[1] & 1 else 0)
    osize = reader.superblock.offset_size
    expected = pos + (3 if raw[1] & 2 else 2) * osize
    if len(raw) != expected:
        raise FormatError("invalid group link-info length")
    addresses = [_uint(raw[pos + i * osize:pos + (i + 1) * osize])
                 for i in range(3 if raw[1] & 2 else 2)]
    if any(a != reader.superblock.undefined_address for a in addresses):
        raise UnsupportedFormat("dense group link index needs fractal-heap validation")
    links: dict[str, LinkStep | None] = {}
    for message in messages:
        if message.kind != 6:
            continue
        if message.flags & 2:
            raise UnsupportedFormat("shared link message")
        data = message.data
        if len(data) < 3 or data[0] != 1 or data[1] & 0xE0:
            raise FormatError("invalid compact link message")
        flags = data[1]
        pos = 2
        if flags & 8:
            if pos >= len(data):
                raise FormatError("truncated link type")
            link_type = data[pos]
            pos += 1
            if link_type not in (1, 64):
                raise UnsupportedFormat("unknown user-defined compact link type")
        else:
            link_type = 0
        if flags & 4:
            pos += 8
        if flags & 16:
            if pos >= len(data) or data[pos] != 1:
                raise UnsupportedFormat("unsupported link name character set")
            pos += 1
        width = 1 << (flags & 3)
        if pos + width > len(data):
            raise FormatError("truncated compact link name length")
        length = _uint(data[pos:pos + width]); pos += width
        if not 1 <= length <= MAX_PATH_BYTES or pos + length > len(data):
            raise FormatError("compact link field lengths disagree")
        name_bytes = data[pos:pos + length]
        try:
            name = name_bytes.decode("utf-8" if flags & 16 else "ascii", "strict")
        except UnicodeDecodeError as exc:
            raise FormatError("invalid compact link name encoding") from exc
        if "\x00" in name or "/" in name or name in (".", ".."):
            raise FormatError("invalid compact link component")
        if name in links:
            raise FormatError("duplicate compact group link name")
        pos += length
        if link_type:
            if len(data) - pos < 2 or len(data) != pos + 2 + _uint(data[pos:pos + 2]):
                raise FormatError("soft or external link target length disagrees")
            links[name] = None
        else:
            if len(data) - pos != osize:
                raise FormatError("compact hard-link address length disagrees")
            target = _uint(data[pos:])
            reader.absolute(target)
            links[name] = LinkStep(address, name, target, message.absolute_offset)
    counts = Counter(step.object_address for step in links.values() if step is not None)
    for target, count in counts.items():
        if count < 2:
            continue
        declared = _unique(_messages(reader, target), 22)
        if declared is None or len(declared.data) != 5 or declared.data[0] != 0 or (
            _uint(declared.data[1:]) < count
        ):
            raise FormatError("modern local hard-link count contradicts target object header")
    return links


def _dataspace(raw: bytes, lsize: int, *, older_padding: bool = False,
               allow_growing: bool = False) -> tuple[tuple[int, ...], tuple[int | None, ...]]:
    if len(raw) < 4:
        raise FormatError("truncated dataspace")
    version, rank, flags, space_type = raw[:4]
    if rank not in (1, 2) or flags & ~1:
        raise UnsupportedFormat("fallback requires rank one or two, simple dataspace")
    if version == 1:
        if len(raw) < 8 or any(raw[3:8]):
            raise FormatError("invalid version-one dataspace prefix")
        pos = 8
    elif version == 2:
        if space_type != 1:
            raise UnsupportedFormat("fallback requires simple dataspace")
        pos = 4
    else:
        raise UnsupportedFormat("unsupported dataspace version")
    expected = pos + rank * lsize * (2 if flags & 1 else 1)
    if older_padding and len(raw) >= expected and len(raw) == (expected + 7) // 8 * 8 and not any(raw[expected:]):
        raw = raw[:expected]
    if len(raw) != expected:
        raise FormatError("dataspace length disagrees with rank and flags")
    shape = tuple(_uint(raw[pos + i * lsize:pos + (i + 1) * lsize]) for i in range(rank))
    pos += rank * lsize
    maximum_raw = (tuple(_uint(raw[pos + i * lsize:pos + (i + 1) * lsize]) for i in range(rank))
               if flags & 1 else shape)
    unlimited = (1 << (8 * lsize)) - 1
    maximum: tuple[int | None, ...] = tuple(None if x == unlimited else x for x in maximum_raw)
    if (not all(x > 0 for x in shape) or prod(shape) > 1_048_576
        or any(x is not None and x < length for x, length in zip(maximum, shape))):
        raise UnsupportedFormat("fallback needs positive, bounded, coherent dimensions")
    if not allow_growing and maximum != shape:
        raise UnsupportedFormat("older fallback requires fixed extents")
    return shape, maximum


def _dtype(raw: bytes, rank: int) -> str:
    if rank == 2:
        if (len(raw) != 12 or raw[0] >> 4 not in (1, 2, 3, 4)
            or raw[0] & 15 != 0 or raw[1:4] != b"\x00\x00\x00"
            or raw[4:8] != b"\x04\x00\x00\x00"
            or raw[8:12] != b"\x00\x00\x20\x00"):
            raise UnsupportedFormat("fallback datatype is not canonical little-endian uint32")
        return "<u4"
    if (len(raw) != 20 or raw[0] >> 4 not in (1, 2, 3, 4)
        or raw[0] & 15 != 1 or raw[1:4] != b"\x20\x3f\x00"
        or raw[4:8] != b"\x08\x00\x00\x00"
        or raw[8:20] != b"\x00\x00\x40\x00\x34\x0b\x00\x34\xff\x03\x00\x00"):
        raise UnsupportedFormat("fallback datatype is not canonical little-endian IEEE float64")
    return "<f8"


def _numeric_dtype(raw: bytes) -> str:
    """Decode only exact primitive, full-precision on-disk numeric types."""
    if len(raw) not in (12, 20) or raw[0] >> 4 not in (1, 2, 3, 4, 5):
        raise UnsupportedFormat("fallback numeric datatype version or length unsupported")
    kind = raw[0] & 15
    size = _uint(raw[4:8])
    order = raw[1] & 1
    prefix = ">" if order else "<"
    if kind == 0:
        if (len(raw) != 12 or size not in (1, 2, 4, 8)
            or raw[1] & ~9 or any(raw[2:4])
            or _uint(raw[8:10]) != 0 or _uint(raw[10:12]) != size * 8):
            raise UnsupportedFormat("noncanonical fixed-point datatype")
        signed = bool(raw[1] & 8)
        return f"{prefix}{'i' if signed else 'u'}{size}"
    if kind == 1:
        if (len(raw) != 20 or size not in (4, 8)
            or raw[1] != (0x20 | order)
            or raw[2] != size * 8 - 1 or raw[3] != 0):
            raise UnsupportedFormat("noncanonical IEEE floating-point datatype")
        expected = (
            b"\x00\x00\x20\x00\x17\x08\x00\x17\x7f\x00\x00\x00" if size == 4 else
            b"\x00\x00\x40\x00\x34\x0b\x00\x34\xff\x03\x00\x00"
        )
        if raw[8:] != expected:
            raise UnsupportedFormat("floating-point bit positions or padding are noncanonical")
        return f"{prefix}f{size}"
    raise UnsupportedFormat("fallback cannot interpret compound, VLEN, reference, or other datatype")


def _filters(raw: bytes | None, element_size: int) -> tuple[FilterDescriptor, ...]:
    if raw is None:
        return ()
    if len(raw) < 2 or raw[0] != 2 or not 1 <= raw[1] <= MAX_FILTERS:
        raise UnsupportedFormat("fallback supports only bounded filter-pipeline version two")
    count = raw[1]
    pos = 2
    result: list[FilterDescriptor] = []
    for _ in range(count):
        if len(raw) - pos < 6:
            raise FormatError("truncated filter entry")
        identifier = _uint(raw[pos:pos + 2]); pos += 2
        if identifier >= 256:
            if len(raw)-pos < 2:
                raise FormatError("truncated custom-filter name length")
            name_length = _uint(raw[pos:pos+2]); pos += 2
            if name_length > 128:
                raise UnsupportedFormat("custom-filter name exceeds limit")
            if len(raw)-pos < 4:
                raise FormatError("truncated custom-filter flags or values")
        else:
            name_length = 0
        flags, values = _uint(raw[pos:pos + 2]), _uint(raw[pos + 2:pos + 4]); pos += 4
        if flags & ~1 or values > 16 or len(raw) - pos < name_length + 4 * values:
            raise FormatError("invalid filter flags or client data length")
        name = raw[pos:pos+name_length]; pos += name_length
        if name and (b"\x00" not in name or any(name[name.index(0):])):
            raise FormatError("custom-filter name is not terminated")
        params = tuple(_uint(raw[pos + 4*i:pos + 4*(i+1)]) for i in range(values))
        pos += 4 * values
        if identifier == 3 and (flags, params) != (0, ()):
            raise UnsupportedFormat("unsupported Fletcher32 settings")
        if identifier == 1 and not (len(params) == 1 and 0 <= params[0] <= 9):
            raise UnsupportedFormat("unsupported DEFLATE settings")
        if identifier == 2 and params != (element_size,):
            raise UnsupportedFormat("shuffle element size contradicts datatype")
        if any(item.id == identifier for item in result):
            raise FormatError("duplicate filter in selected pipeline")
        result.append(FilterDescriptor(identifier, flags, params))
    if pos != len(raw):
        raise FormatError("filter-pipeline trailing bytes")
    return tuple(result)


def _old_filters(raw: bytes | None) -> tuple[FilterDescriptor, ...]:
    if raw is None:
        return ()
    if len(raw) < 8 or raw[0] != 1 or not 1 <= raw[1] <= MAX_FILTERS or any(raw[2:8]):
        raise UnsupportedFormat("invalid older filter-pipeline prefix")
    pos = 8
    filters: list[FilterDescriptor] = []
    for _ in range(raw[1]):
        if len(raw) - pos < 8:
            raise FormatError("truncated older filter entry")
        identifier, name_length = _uint(raw[pos:pos+2]), _uint(raw[pos+2:pos+4])
        flags, count = _uint(raw[pos+4:pos+6]), _uint(raw[pos+6:pos+8])
        pos += 8
        if identifier not in (1, 3) or name_length > 128 or name_length % 8 or count > 16:
            raise UnsupportedFormat("unsupported older filter metadata")
        if flags & ~1 or len(raw) - pos < name_length + 4*count:
            raise FormatError("invalid older filter flags, name, or values")
        name = raw[pos:pos+name_length]; pos += name_length
        if name_length and (b"\x00" not in name or any(name[name.index(0):])):
            raise FormatError("invalid older filter name")
        values = tuple(_uint(raw[pos+4*i:pos+4*(i+1)]) for i in range(count))
        pos += 4*count
        if count % 2:
            if len(raw)-pos < 4 or any(raw[pos:pos+4]):
                raise FormatError("invalid older filter client-data padding")
            pos += 4
        if identifier == 3 and (flags, values) != (0, ()):
            raise UnsupportedFormat("unsupported older Fletcher32 parameters")
        if identifier == 1 and not (flags == 1 and len(values) == 1 and 0 <= values[0] <= 9):
            raise UnsupportedFormat("unsupported older DEFLATE parameters")
        filters.append(FilterDescriptor(identifier, flags, values))
    if any(raw[pos:]):
        raise FormatError("older filter-pipeline trailing bytes")
    return tuple(filters)


def _chunk_shape(raw: bytes, shape: tuple[int, ...], itemsize: int) -> tuple[int, ...]:
    if len(raw) < 6 or raw[0] not in (4, 5) or raw[1] != 2:
        raise UnsupportedFormat("fallback requires a modern chunked layout")
    flags, ndim, width = raw[2:5]
    if flags & ~3 or ndim != len(shape) + 1 or width not in (1, 2, 4, 8):
        raise UnsupportedFormat("unsupported modern chunk-dimension encoding")
    if len(raw) < 5 + ndim * width + 1:
        raise FormatError("truncated modern chunk dimensions")
    dims = tuple(_uint(raw[i:i + width]) for i in range(5, 5 + ndim * width, width))
    chunks = dims[:-1]
    if dims[-1] != itemsize or any(c <= 0 for c in chunks):
        raise UnsupportedFormat("datatype or zero chunk dimensions outside fallback route")
    if prod(chunks) * itemsize > 1_048_576:
        raise UnsupportedFormat("fallback chunk exceeds 1 MiB")
    return chunks


def _validate_metadata_ranges(ranges: list[tuple[int, int, str]]) -> None:
    ordered = sorted(set((a, b) for a, b, _kind in ranges))
    if any(a >= b for a, b in ordered):
        raise FormatError("invalid rooted metadata extent")
    for previous, current in zip(ordered, ordered[1:]):
        if previous[1] > current[0]:
            raise FormatError("rooted metadata allocations overlap")


def _hash_bounded_source(path: Path) -> str:
    digest = sha256()
    total = 0
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            total += len(block)
            if total > MAX_SOURCE_BYTES:
                raise UnsupportedFormat("fallback source exceeds 4 GiB read limit")
            digest.update(block)
    return digest.hexdigest()


def _old_fallback(snapshot: Path, dataset_path: str, parts: list[str], source_hash: str) -> FallbackMetadata:
    """Follow old symbol-table entries; this format has no metadata checksum."""
    with H5File(snapshot) as reader:
        root, cached = _old_root_address(reader)
        address = root
        chain: list[LinkStep] = []
        for part in parts:
            links = _old_group(reader, address, cached)
            if part not in links:
                raise UnsupportedFormat(f"selected component {part!r} has no rooted symbol-table entry")
            step = links[part]
            if step is None:
                raise UnsupportedFormat(f"selected component {part!r} is not a local hard link")
            chain.append(step)
            address = step.object_address
            cached = step.cached_group
        selected = _old_messages(reader, address)
        if _unique(selected, 7) is not None:
            raise UnsupportedFormat("selected older dataset uses external raw storage")
        space, dtype, layout = (_unique(selected, k) for k in (1, 3, 8))
        if space is None or dtype is None or layout is None:
            raise UnsupportedFormat("selected older object lacks required dataset metadata")
        shape, maximum = _dataspace(space.data, reader.superblock.length_size, older_padding=True)
        itemsize = 4 if len(shape) == 2 else 8
        dt_len = 12 if itemsize == 4 else 20
        if len(dtype.data) != (dt_len+7)//8*8 or any(dtype.data[dt_len:]):
            raise FormatError("older dataset datatype message padding invalid")
        dtype_name = _dtype(dtype.data[:dt_len], len(shape))
        pipeline_message = _unique(selected, 11)
        pipeline = _old_filters(pipeline_message.data if pipeline_message else None)
        filters = tuple(item.id for item in pipeline)
        if len(shape) == 2 and pipeline:
            raise UnsupportedFormat("rank-two older fallback requires unfiltered chunks")
        if len(shape) == 1 and filters != (3, 1):
            raise UnsupportedFormat("rank-one older fallback requires Fletcher32 then DEFLATE")
        dataset_layout = reader.read_dataset_layout(address, rank=len(shape))
        if dataset_layout.chunk_shape == () or any(s % c for s, c in zip(shape, dataset_layout.chunk_shape)):
            raise UnsupportedFormat("older fallback requires fully aligned chunk extents")
        if dataset_layout.element_size != itemsize or prod(dataset_layout.chunk_shape)*itemsize > 1_048_576:
            raise FormatError("older layout contradicts datatype or chunk limit")
        omitted_attributes = (("all attribute metadata (raw fallback cannot safely decode it)",)
                              if any(message.kind in (12, 21) for message in selected) else ())
        spec = DatasetSpec(dataset_path, address, shape, dataset_layout.chunk_shape, dtype_name,
                           filters, omitted_attributes=omitted_attributes,
                           filter_pipeline=pipeline, maxshape=maximum)
        omitted = tuple(
            f"auxiliary message type {message.kind} at byte {message.absolute_offset}: "
            "preserved only in the damaged source, not interpreted by fallback"
            for message in selected if message.kind in (5, 12, 13, 17, 21)
        )
        _validate_metadata_ranges(reader.metadata_ranges)
        return FallbackMetadata(spec, source_hash, root, tuple(chain),
                                tuple(reader.metadata_ranges), omitted,
                                route="unchecksummed_older_symbol_table_hard_links")


def _superblock_version(snapshot: Path) -> int:
    size = snapshot.stat().st_size
    with snapshot.open("rb") as stream:
        pos = 0
        while pos + 9 <= size:
            stream.seek(pos)
            if stream.read(8) == b"\x89HDF\r\n\x1a\n":
                return stream.read(1)[0]
            pos = 512 if pos == 0 else pos * 2
    raise FormatError("HDF5 signature not found at a permitted offset")


def read_dataset_spec_fallback(
    snapshot: Path, dataset_path: str, *, expected_sha256: str | None = None,
) -> FallbackMetadata:
    """Resolve selected metadata from the same immutable snapshot used by recovery.

    This is not a recovery of lost links. It helps only if the rooted compact
    path and all required dataset messages survive even though a native open
    fails for another reason. The caller still validates the chunk index and
    payload separately before publishing any measurement.
    """
    if (not isinstance(dataset_path, str) or not dataset_path.startswith("/")
        or dataset_path in ("/", "/_h5reclaim")
        or dataset_path.startswith("/_h5reclaim/")):
        raise UnsupportedFormat("fallback requires an absolute selected dataset path")
    path_bytes = dataset_path.encode("utf-8")
    parts = dataset_path.split("/")[1:]
    if (len(path_bytes) > MAX_PATH_BYTES or len(parts) > MAX_DEPTH
        or any(not p or p in (".", "..") or "\x00" in p for p in parts)):
        raise UnsupportedFormat("fallback path is not canonical or exceeds limits")
    if expected_sha256 is not None and (len(expected_sha256) != 64 or any(
        c not in "0123456789abcdef" for c in expected_sha256)):
        raise FormatError("expected source SHA-256 must be lowercase hex")
    snapshot = Path(snapshot)
    before = snapshot.stat()
    if before.st_size > MAX_SOURCE_BYTES:
        raise UnsupportedFormat("fallback source exceeds 4 GiB read limit")
    # This bounded streaming hash binds parser observations to a snapshot;
    # it does not prove the scientific data were unmodified before damage.
    source_hash = _hash_bounded_source(snapshot)
    if expected_sha256 is not None and expected_sha256 != source_hash:
        raise FormatError("independently supplied source SHA-256 disagrees")
    version = _superblock_version(snapshot)
    if version in (0, 1):
        result = _old_fallback(snapshot, dataset_path, parts, source_hash)
        after = snapshot.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ) or _hash_bounded_source(snapshot) != source_hash:
            raise FormatError("source changed during raw metadata fallback")
        return result
    if version not in (2, 3):
        raise UnsupportedFormat(f"superblock version {version} is unsupported by fallback")
    with ModernH5File(snapshot) as reader:
        root = reader.superblock.root_object_address
        address = root
        chain: list[LinkStep] = []
        for part in parts:
            links = _compact_links(reader, address)
            if part not in links:
                raise UnsupportedFormat(f"selected component {part!r} has no verified compact hard link")
            step = links[part]
            if step is None:
                raise UnsupportedFormat(f"selected component {part!r} is not a local hard link")
            chain.append(step)
            address = step.object_address
        selected = _messages(reader, address)
        if _unique(selected, 7) is not None:
            raise UnsupportedFormat("selected dataset uses external raw storage")
        space, dtype, layout = (_unique(selected, k) for k in (1, 3, 8))
        if space is None or dtype is None or layout is None:
            raise UnsupportedFormat("selected object lacks required dataset metadata")
        shape, maximum = _dataspace(space.data, reader.superblock.length_size,
                                    allow_growing=True)
        dtype_name = _numeric_dtype(dtype.data)
        itemsize = int(dtype_name[-1])
        pipeline_message = _unique(selected, 11)
        pipeline = _filters(pipeline_message.data if pipeline_message else None, itemsize)
        filters = tuple(item.id for item in pipeline)
        chunks = _chunk_shape(layout.data, shape, itemsize)
        if layout.data[5 + (len(shape) + 1) * layout.data[4]] not in (1, 2, 3, 4, 5):
            raise UnsupportedFormat("unsupported modern index family in fallback")
        omitted_attributes = (("all attribute metadata (raw fallback cannot safely decode it)",)
                              if any(message.kind in (12, 21) for message in selected) else ())
        spec = DatasetSpec(dataset_path, address, shape, chunks, dtype_name, filters,
                           omitted_attributes=omitted_attributes,
                           filter_pipeline=pipeline, maxshape=maximum)
        # The index reader separately checks its own layout interpretation.
        # It also verifies declared data ranges and registered metadata ranges.
        reader.read_index(address, shape, chunks, itemsize,
                          maxshape=maximum, filters=filters)
        omitted = tuple(
            f"auxiliary message type {message.kind} at byte {message.absolute_offset}: "
            "preserved only in the damaged source, not interpreted by fallback"
            for message in selected if message.kind in (5, 12, 13, 17, 21)
        )
        _validate_metadata_ranges(reader.metadata_ranges)
        result = FallbackMetadata(spec, source_hash, root, tuple(chain),
                                  tuple(reader.metadata_ranges), omitted)
    after = snapshot.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ) or _hash_bounded_source(snapshot) != source_hash:
        raise FormatError("source changed during raw metadata fallback")
    return result
