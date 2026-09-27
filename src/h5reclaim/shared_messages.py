"""Bounded resolution of rooted shared dataset schema messages.

This accepts a committed datatype in a checksummed v2 object header, or a
shared dataspace/datatype/filter message in a checksummed SOHM master table,
record list and managed fractal heap. It never scans for plausible messages.

HDF5 File Format Specification v4, sections III.I, IV.A.2, IV.A.3.p:
https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .dense_group_links import _direct_block, _read_heap
from .format import FormatError, UnsupportedFormat
from .modern_indexes import ModernH5File, lookup3


MAX_INDICES = 8
MAX_RECORDS = 128
MAX_SHARED_BYTES = 1 << 20
MAX_BTREE_DEPTH = 1
MAX_BTREE_NODE_BYTES = 1 << 20
# The file carries H5O_SHMESG_* flags as defined in H5Opublic.h, using the
# object-header message type as the bit position. The prose table in one
# revision of the format specification lists compact ordinal bit positions;
# actual HDF5-written files use these public API values.
_MESSAGE_FLAG = {1: 1 << 1, 3: 1 << 3, 5: 1 << 5,
                 11: 1 << 11, 12: 1 << 12}
_VALID_FLAGS = sum(_MESSAGE_FLAG.values())


class Message(Protocol):
    kind: int
    flags: int
    data: bytes
    absolute_offset: int


@dataclass(frozen=True)
class ResolvedMessage:
    data: bytes
    absolute_offset: int
    route: str


def _uint(raw: bytes) -> int:
    return int.from_bytes(raw, "little")


def _width(value: int) -> int:
    return max(1, (value.bit_length() + 7) // 8)


def _reserve_index(reader: ModernH5File, pending: list[tuple[int, int, str]],
                   address: int, length: int, label: str) -> int:
    start = reader.absolute(address)
    end = start + length
    if length < 1 or end > reader.superblock.eof_address:
        raise FormatError("SOHM index allocation exceeds declared end-of-file")
    if sum(b-a for a, b, _ in pending) + length > 16 << 20:
        raise UnsupportedFormat("SOHM index metadata exceeds 16 MiB")
    if any(start < b and a < end for a, b, _ in (*reader.metadata_ranges, *pending)):
        raise FormatError("SOHM index overlaps parsed metadata")
    pending.append((start, end, label))
    return start


def _btree_records(reader: ModernH5File, address: int,
                   expected: int, pending: list[tuple[int, int, str]]) -> tuple[bytes, ...]:
    """Read a bounded type-7 v2 B-tree, including internal-record ownership."""
    osize, lsize = reader.superblock.offset_size, reader.superblock.length_size
    header_size = 22 + osize + lsize
    header = reader.read_at(address, header_size)
    if header[:4] != b"BTHD" or header[4:6] != b"\x00\x07":
        raise FormatError("SOHM B-tree header signature, version, or client invalid")
    if lookup3(header[:-4]) != _uint(header[-4:]):
        raise FormatError("SOHM B-tree header checksum mismatch")
    node_size = _uint(header[6:10]); record_size = _uint(header[10:12])
    depth = _uint(header[12:14]); split, merge = header[14:16]
    root = _uint(header[16:16+osize])
    root_count = _uint(header[16+osize:18+osize])
    total = _uint(header[18+osize:18+osize+lsize])
    if (not 64 <= node_size <= MAX_BTREE_NODE_BYTES or record_size != 17
        or depth > MAX_BTREE_DEPTH or not 0 < split <= 100
        or not 0 < merge <= 100 or not 1 <= total <= MAX_RECORDS):
        raise UnsupportedFormat("SOHM B-tree node size, depth, or record shape exceeds limits")
    if total != expected or root == reader.superblock.undefined_address:
        raise FormatError("SOHM B-tree root or total count contradicts master table")
    _reserve_index(reader, pending, address, header_size, "SOHM B-tree header")
    capacities = [(node_size - 10) // 17]
    subtrees = [capacities[0]]
    if capacities[0] < 1:
        raise UnsupportedFormat("SOHM B-tree leaf has no room for a record")
    for level in range(1, depth+1):
        pointer = osize + _width(capacities[level-1])
        if level > 1:
            pointer += _width(subtrees[level-1])
        capacity = (node_size - 10 - pointer) // (17 + pointer)
        if capacity < 1:
            raise UnsupportedFormat("SOHM B-tree internal node has no room for a record")
        capacities.append(capacity)
        subtrees.append(capacity + (capacity+1)*subtrees[level-1])
    if not 1 <= root_count <= capacities[depth]:
        raise FormatError("SOHM B-tree root count exceeds node capacity")
    visited: set[int] = set()

    def visit(node_address: int, level: int, count: int) -> tuple[list[bytes], int]:
        if (node_address in visited or len(visited) >= MAX_RECORDS
            or not 1 <= count <= capacities[level]):
            raise FormatError("SOHM B-tree node repeated or record count invalid")
        visited.add(node_address)
        _reserve_index(reader, pending, node_address, node_size, "SOHM B-tree node")
        block = reader.read_at(node_address, node_size)
        if block[:4] != (b"BTIN" if level else b"BTLF") or block[4:6] != b"\x00\x07":
            raise FormatError("SOHM B-tree node signature, version, or client invalid")
        cursor = 6 + count * 17
        own = [block[6+i*17:6+(i+1)*17] for i in range(count)]
        children = []
        if level:
            count_width = _width(capacities[level-1])
            total_width = _width(subtrees[level-1]) if level > 1 else 0
            for _ in range(count+1):
                if cursor + osize + count_width + total_width > node_size - 4:
                    raise FormatError("SOHM B-tree child pointer crosses node boundary")
                child = _uint(block[cursor:cursor+osize]); cursor += osize
                child_count = _uint(block[cursor:cursor+count_width]); cursor += count_width
                child_total = None
                if level > 1:
                    child_total = _uint(block[cursor:cursor+total_width]); cursor += total_width
                if child == reader.superblock.undefined_address:
                    raise FormatError("SOHM B-tree child pointer undefined")
                children.append((child, child_count, child_total))
        if cursor + 4 > node_size or lookup3(block[:cursor]) != _uint(block[cursor:cursor+4]):
            raise FormatError("SOHM B-tree node checksum mismatch")
        if not level:
            return own, count
        ordered: list[bytes] = []
        observed = count
        for i, (child, child_count, child_total) in enumerate(children):
            subset, actual = visit(child, level-1, child_count)
            if child_total is not None and child_total != actual:
                raise FormatError("SOHM B-tree subtree count contradicts parent")
            ordered.extend(subset)
            observed += actual
            if i < count:
                ordered.append(own[i])
        return ordered, observed

    records, observed = visit(root, depth, root_count)
    if observed != total or len(records) != total:
        raise FormatError("SOHM B-tree record count contradicts master table")
    hashes = [_uint(record[1:5]) for record in records]
    if any(a > b for a, b in zip(hashes, hashes[1:])):
        raise FormatError("SOHM B-tree record hashes violate key order")
    return tuple(records)


def _validated_extension(reader: ModernH5File, messages: Callable[[ModernH5File, int], tuple[Message, ...]]) -> Message:
    sb = reader.superblock
    pointer = sb.signature_offset + 12 + sb.offset_size
    address = _uint(reader._read_absolute(pointer, sb.offset_size))
    if address == sb.undefined_address:
        raise UnsupportedFormat("shared message has no superblock extension")
    reader.absolute(address)
    found = [m for m in messages(reader, address) if m.kind == 15]
    if len(found) != 1 or found[0].flags & 2:
        raise FormatError("superblock extension has no unique local SOHM table message")
    return found[0]


def _sohm_message(reader: ModernH5File, *, kind: int, encoded: bytes,
                  messages: Callable[[ModernH5File, int], tuple[Message, ...]]) -> ResolvedMessage:
    sb = reader.superblock
    if len(encoded) != 2 + 8 or encoded[:2] != b"\x03\x01":
        raise UnsupportedFormat("shared message is not a version-three SOHM heap reference")
    identifier = encoded[2:]
    extension = _validated_extension(reader, messages)
    if (len(extension.data) != 2 + sb.offset_size or extension.data[0] != 0
        or not 1 <= extension.data[-1] <= MAX_INDICES):
        raise UnsupportedFormat("SOHM extension table version or index count is unsupported")
    count = extension.data[-1]
    table_address = _uint(extension.data[1:1 + sb.offset_size])
    entry_size = 14 + 2 * sb.offset_size
    size = 4 + count * entry_size + 4
    table = reader.read_at(table_address, size)
    if table[:4] != b"SMTB" or lookup3(table[:-4]) != _uint(table[-4:]):
        raise FormatError("SOHM master-table signature or checksum mismatch")
    table_start = reader.absolute(table_address)
    pending: list[tuple[int, int, str]] = [(table_start, table_start + size, "SOHM master table")]
    occupied_flags = 0
    selected = None
    for i in range(count):
        entry = table[4 + i * entry_size:4 + (i + 1) * entry_size]
        version, index_type = entry[:2]
        flags = _uint(entry[2:4])
        minimum = _uint(entry[4:8])
        list_cutoff, btree_cutoff, nrecords = (_uint(entry[pos:pos+2]) for pos in (8, 10, 12))
        index_address = _uint(entry[14:14 + sb.offset_size])
        heap_address = _uint(entry[14 + sb.offset_size:])
        if (version != 0 or index_type not in (0, 1) or flags == 0 or flags & ~_VALID_FLAGS
            or flags & occupied_flags or not 0 < btree_cutoff <= list_cutoff
            or not 1 <= minimum <= MAX_SHARED_BYTES):
            raise UnsupportedFormat("SOHM master-table index header is unsupported or contradictory")
        occupied_flags |= flags
        if flags & _MESSAGE_FLAG[kind]:
            selected = (index_type, flags, minimum, nrecords, index_address, heap_address)
    if selected is None:
        raise FormatError("no SOHM index tracks the referenced message type")
    index_type, flags, minimum, nrecords, index_address, heap_address = selected
    if not flags & _MESSAGE_FLAG[kind]:
        raise FormatError("SOHM index does not track the referenced message type")
    if not 1 <= nrecords <= MAX_RECORDS:
        raise UnsupportedFormat("SOHM index record count exceeds bound")
    if index_address == sb.undefined_address or heap_address == sb.undefined_address:
        raise FormatError("SOHM index or heap pointer is undefined")
    if index_type == 0:
        list_size = 4 + 17 * nrecords + 4
        block = reader.read_at(index_address, list_size)
        if block[:4] != b"SMLI" or lookup3(block[:-4]) != _uint(block[-4:]):
            raise FormatError("SOHM record-list signature or checksum mismatch")
        _reserve_index(reader, pending, index_address, list_size, "SOHM record list")
        records = tuple(block[4+i*17:4+(i+1)*17] for i in range(nrecords))
        index_route = "list"
    else:
        records = _btree_records(reader, index_address, nrecords, pending)
        index_route = "btree"
    unique_ids: set[bytes] = set()
    matches = 0
    for entry in records:
        if entry[0] != 0:
            raise UnsupportedFormat("SOHM index contains a non-heap record")
        if _uint(entry[5:9]) == 0:
            raise FormatError("SOHM heap record has no references")
        candidate = entry[9:17]
        if candidate in unique_ids:
            raise FormatError("SOHM index repeats a heap ID")
        unique_ids.add(candidate)
        matches += candidate == identifier
    if matches != 1:
        raise FormatError("shared message heap ID has no unique index owner")
    heap = _read_heap(reader, heap_address, pending)
    if heap.id_length != 8 or heap.managed_count != nrecords or identifier[0] != 0:
        raise UnsupportedFormat("SOHM heap needs matching managed ID and record count")
    offset = _uint(identifier[1:1 + heap.offset_width])
    length = _uint(identifier[1 + heap.offset_width:])
    if length < minimum or length > MAX_SHARED_BYTES or offset + length > heap.managed_space:
        raise FormatError("SOHM heap ID has invalid offset or message length")
    cache: dict[int, tuple[int, int, bytes]] = {}
    indirect_cache: dict[int, tuple[int, int, bytes]] = {}
    block_offset, block_size, block = _direct_block(reader, heap, offset, pending, cache,
                                                    indirect_cache)
    data_start = 5 + sb.offset_size + heap.offset_width + 4
    relative = offset - block_offset
    if relative < data_start or relative + length > block_size:
        raise FormatError("SOHM heap ID intersects direct-block header or boundary")
    direct_address = next(address for address, (start, size, _) in cache.items()
                          if (start, size) == (block_offset, block_size))
    physical = reader.absolute(direct_address) + relative
    # The index hash is preserved in the checksummed list or B-tree. Its implementation
    # is not rederived here; the exact heap ID is independently matched.
    reader.metadata_ranges.extend((a, b, label.replace("dense", "SOHM"))
                                  for a, b, label in pending)
    return ResolvedMessage(block[relative:relative + length], physical,
                           f"checksummed_sohm_{index_route}_managed_heap")


def resolve_shared_schema(reader: ModernH5File, *, selected_address: int,
                          kind: int, encoded: bytes,
                          messages: Callable[[ModernH5File, int], tuple[Message, ...]]) -> ResolvedMessage:
    """Resolve only a shared message reached through the checked selected OHDR."""
    if kind not in (1, 3, 11):
        raise UnsupportedFormat("unsupported shared dataset metadata type")
    sb = reader.superblock
    if len(encoded) == 2 + sb.offset_size and encoded[0] in (2, 3) and encoded[1] in (0, 2):
        if kind != 3:
            raise UnsupportedFormat("committed shared reference is not a datatype")
        target = _uint(encoded[2:])
        if target == selected_address:
            raise FormatError("committed datatype points back to selected dataset")
        reader.absolute(target)
        target_messages = messages(reader, target)
        if any(m.kind in (1, 2, 6, 8, 10, 11, 17) for m in target_messages):
            raise FormatError("committed datatype target is a group or dataset object")
        found = [m for m in target_messages if m.kind == kind]
        if len(found) != 1 or found[0].flags & 2:
            raise FormatError("committed datatype target lacks a unique local datatype message")
        return ResolvedMessage(found[0].data, found[0].absolute_offset,
                               "checksummed_committed_datatype")
    return _sohm_message(reader, kind=kind, encoded=encoded, messages=messages)
