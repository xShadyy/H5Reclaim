"""Inspect or explicitly detach an HDF5 metadata cache image on a new copy.

The cache is optional acceleration metadata. Detachment retains its bytes and
the file's extent, and requires the ordinary rooted metadata to pass preflight
without the cache. This never treats cache removal as measurement recovery.
"""

import time
from pathlib import Path

from .format import FormatError, UnsupportedFormat
from .large_streaming import LargeBudget, _deadline
from .metadata_fallback import _messages, _unique
from .modern_indexes import ModernH5File, lookup3


def _declaration(reader):
    sb = reader.superblock
    width = sb.offset_size
    raw = reader._read_absolute(sb.signature_offset, 16 + 4 * width)
    extension = int.from_bytes(raw[12 + width : 12 + 2 * width], "little")
    if extension == sb.undefined_address:
        return None
    message = _unique(_messages(reader, extension), 0x18)
    if message is None or len(message.data) != 1 + width + sb.length_size:
        return None  # Historical file-space message 0x18 has another length.
    if message.data[0] != 0 or message.flags & ~0x84:
        raise UnsupportedFormat("unsupported metadata cache-image declaration")
    address = int.from_bytes(message.data[1 : 1 + width], "little")
    size = int.from_bytes(message.data[1 + width :], "little")
    start = reader.absolute(address)
    if size < 14 + sb.length_size or size > min(reader.size, sb.eof_address) - start:
        raise FormatError("metadata cache-image declaration is outside the file")
    return extension, message, start, size


def _inspect(path, budget):
    budget = budget or LargeBudget()
    deadline = time.monotonic() + budget.max_seconds
    with ModernH5File(Path(path), large_sparse_scan=True) as reader:
        declaration = _declaration(reader)
        if declaration is None:
            return {"present": False, "metadata_ranges": []}, []
        _, _, start, size = declaration
        if size > min(32 << 20, budget.max_metadata_bytes):
            raise UnsupportedFormat(
                "cache-image checksum inspection exceeds its metadata byte budget"
            )
        raw = reader._read_absolute(start, size)
        width = reader.superblock.length_size
        if raw[:5] != b"MDCI\0" or raw[5] != 0:
            raise FormatError("metadata cache-image signature, version or flags are invalid")
        if int.from_bytes(raw[6 : 6 + width], "little") != size:
            raise FormatError("metadata cache-image lengths disagree")
        entries = int.from_bytes(raw[6 + width : 10 + width], "little")
        if not 0 < entries <= budget.max_objects:
            raise UnsupportedFormat("metadata cache-image entry count exceeds its object budget")
        _deadline(deadline)
        checksum = lookup3(raw[:-4])
        _deadline(deadline)
        if checksum != int.from_bytes(raw[-4:], "little"):
            raise FormatError("metadata cache-image checksum mismatch")
        offset_size = reader.superblock.offset_size
        cursor, records, ranges = 10 + width, [], []
        prefix = 14 + offset_size + width
        for _ in range(entries):
            _deadline(deadline)
            if len(raw) - 4 - cursor < prefix:
                raise FormatError("metadata cache-image entry header is truncated")
            flags = raw[cursor + 1]
            parents = int.from_bytes(raw[cursor + 8 : cursor + 10], "little")
            children = int.from_bytes(raw[cursor + 4 : cursor + 6], "little")
            dirty_children = int.from_bytes(raw[cursor + 6 : cursor + 8], "little")
            address = int.from_bytes(raw[cursor + 14 : cursor + 14 + offset_size], "little")
            length = int.from_bytes(raw[cursor + 14 + offset_size : cursor + prefix], "little")
            if flags & ~15 or dirty_children > children or parents > budget.max_objects:
                raise FormatError("metadata cache-image entry flags or dependencies are invalid")
            physical = reader.absolute(address)
            cursor += prefix + parents * offset_size
            if (
                not 0 < length <= min(1 << 20, budget.max_metadata_bytes)
                or length > len(raw) - 4 - cursor
                or physical + length > min(reader.size, reader.superblock.eof_address)
                or physical < start + size
                and start < physical + length
            ):
                raise FormatError("metadata cache-image entry has an invalid or overlapping range")
            records.append(
                {
                    "offset": physical,
                    "data": raw[cursor : cursor + length],
                    "dirty": bool(flags & 1),
                }
            )
            ranges.append((physical, physical + length))
            cursor += length
        if cursor != len(raw) - 4:
            raise FormatError("metadata cache-image entries do not cover the declared image")
        ranges.sort()
        if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
            raise FormatError("metadata cache-image entries claim overlapping destinations")
        return {
            "present": True,
            "offset": start,
            "bytes": size,
            "entries": entries,
            "checksum_valid": True,
            "metadata_ranges": [[start, start + size, "metadata cache image"]],
            "scope": "declaration, entry framing and checksum; ordinary metadata is checked separately",
        }, records


def inspect_cache_image(path, *, budget=None):
    result, _ = _inspect(path, budget or LargeBudget())
    return result


def discard_cache_image(path, touches, *, budget=None):
    from .repair_plan import _object_header, _write

    budget = budget or LargeBudget()
    try:
        _, entries = _inspect(path, budget)
    except FormatError as exc:
        if isinstance(exc, UnsupportedFormat):
            raise
        # An invalid cache supplies no replacement bytes. Detachment can
        # succeed only if the ordinary on-disk metadata passes postflight.
        entries = []
    with ModernH5File(Path(path), large_sparse_scan=True) as reader:
        declaration = _declaration(reader)
        if declaration is None:
            return False
        extension, message, _, _ = declaration
        start, raw = _object_header(reader, extension)
        if lookup3(raw[:-4]) != int.from_bytes(raw[-4:], "little"):
            raise FormatError("cache-image detachment needs a checked extension header")
        prefix = 6 if raw[5] & 4 else 4
        at = message.absolute_offset - start - prefix
        if at < 6 or at + prefix + len(message.data) > len(raw) - 4 or raw[at] != 0x18:
            raise UnsupportedFormat(
                "cache-image declaration in a continuation is not detachable here"
            )
        proposal = bytearray(raw)
        proposal[at] = 0  # Same-size NIL message retains all enclosing framing.
        length = len(message.data)
        tail = raw[at + prefix + length : -4]
        if len(tail) < prefix and not any(tail):
            # HDF5 requires a terminal gap to be absorbed by a NIL message.
            length += len(tail)
        proposal[at + 1 : at + 3] = length.to_bytes(2, "little")
        proposal[at + 3 : at + prefix + length] = bytes(prefix - 3 + length)
        proposal[-4:] = lookup3(proposal[:-4]).to_bytes(4, "little")
        updates = []
        for entry in entries:
            before = reader._read_absolute(entry["offset"], len(entry["data"]))
            if before != entry["data"]:
                if entry["offset"] < start + len(raw) and start < entry["offset"] + len(
                    entry["data"]
                ):
                    raise FormatError("cached metadata competes with the cache declaration header")
                updates.append(entry)
    for entry in updates:
        _write(Path(path), entry["offset"], entry["data"])
        touches.append(
            (
                entry["offset"],
                entry["offset"] + len(entry["data"]),
                "restore_cached_metadata",
                "complete original cache-image checksum; replayed metadata independently checked after detachment",
            )
        )
    _write(Path(path), start, proposal)
    touches.append(
        (
            start,
            start + len(raw),
            "discard_metadata_cache_image",
            "explicit cache detachment; ordinary rooted metadata independently rechecked; checksum recomputed",
        )
    )
    return True


def require_covered_updates(touches, ranges):
    """Every replayed byte must belong to independently checked metadata."""
    ordered = sorted((start, end) for start, end, _ in ranges)
    for start, end, kind, _ in touches:
        if kind != "restore_cached_metadata":
            continue
        cursor = start
        for begin, stop in ordered:
            if begin <= cursor < stop:
                cursor = stop
            if cursor >= end:
                break
        if cursor < end:
            raise UnsupportedFormat(
                "cached entry is outside independently checked reachable metadata"
            )
