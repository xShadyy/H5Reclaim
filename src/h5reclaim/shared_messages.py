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
    if index_type != 0 or not 1 <= nrecords <= MAX_RECORDS:
        raise UnsupportedFormat("SOHM shared message requires a bounded record list")
    if index_address == sb.undefined_address or heap_address == sb.undefined_address:
        raise FormatError("SOHM index or heap pointer is undefined")
    list_size = 4 + 17 * nrecords + 4
    records = reader.read_at(index_address, list_size)
    if records[:4] != b"SMLI" or lookup3(records[:-4]) != _uint(records[-4:]):
        raise FormatError("SOHM record-list signature or checksum mismatch")
    list_start = reader.absolute(index_address)
    pending.append((list_start, list_start + list_size, "SOHM record list"))
    unique_ids: set[bytes] = set()
    matches = 0
    for i in range(nrecords):
        entry = records[4 + i * 17:4 + (i + 1) * 17]
        if entry[0] != 0:
            raise UnsupportedFormat("SOHM record list contains a non-heap record")
        if _uint(entry[5:9]) == 0:
            raise FormatError("SOHM heap record has no references")
        candidate = entry[9:17]
        if candidate in unique_ids:
            raise FormatError("SOHM record list repeats a heap ID")
        unique_ids.add(candidate)
        matches += candidate == identifier
    if matches != 1:
        raise FormatError("shared message heap ID has no unique record-list owner")
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
    # The index hash is preserved in the checksummed list. Its implementation
    # is not rederived here; the exact heap ID is independently matched.
    reader.metadata_ranges.extend((a, b, label.replace("dense", "SOHM"))
                                  for a, b, label in pending)
    return ResolvedMessage(block[relative:relative + length], physical,
                           "checksummed_sohm_list_managed_heap")


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
