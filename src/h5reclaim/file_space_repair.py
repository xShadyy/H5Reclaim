"""Check and repair rooted persistent file free-space indexes.

Version-0 and version-1 file-space info messages in a checked modern
superblock extension and their version-0 file-client FSHD/FSSE pairs are covered.
Sections are parsed as evidence, never used to invent or overwrite objects.
"""

from pathlib import Path
from collections import Counter
import time

from .format import FormatError, SIGNATURE, UnsupportedFormat
from .large_streaming import LargeBudget, _deadline
from .metadata_correction import _unique_one_byte
from .metadata_fallback import _messages, _unique
from .modern_indexes import ModernH5File, lookup3


def _uint(raw):
    return int.from_bytes(raw, "little")


def _fixed_prefix_restoration(raw, known_prefix, stored_checksum):
    """Restore independently known format and reciprocal-owner bytes.

    Only a candidate matching the unchanged original checksum is returned.
    This handles several corrupt bytes within a field whose entire historical
    value is fixed by the format and a rooted FSHD address, without guessing
    any variable field or accepting a recomputed checksum.
    """
    if raw.startswith(known_prefix):
        return None
    candidate = known_prefix + raw[len(known_prefix):]
    return candidate if lookup3(candidate[:-4]) == stored_checksum else None


def _version(path):
    with path.open("rb") as source:
        size, position = path.stat().st_size, 0
        while position + 9 <= size:
            source.seek(position)
            prefix = source.read(9)
            if prefix[:8] == SIGNATURE:
                return prefix[8]
            position = 512 if position == 0 else position * 2
    raise FormatError("cannot inspect file-space managers without a rooted superblock")


def _information_addresses(data, flags, sb):
    """Decode the two published file-space information layouts exactly.

    The six-manager version 0 is specific to HDF5 1.10.0. Version 1 adds
    small/large manager pairs and permits an absent address table when space
    is not persisted. Message 0x18 has other meanings in newer format
    revisions, so it is not evidence of a missing file-space declaration.
    """
    if flags & 2 or not data:
        raise UnsupportedFormat("shared or empty file-space info message")
    offsize, lensize = sb.offset_size, sb.length_size
    version = data[0]
    if version == 0:
        prefix = 2 + lensize
        if len(data) < prefix or data[1] not in (1, 2, 3, 4):
            raise FormatError("invalid version-0 file-space strategy or threshold")
        persistent = data[1] == 1
        label = "v0_file_space_info_and_v0_file_manager"
        count, page_size = (6 if persistent else 0), 0
    elif version == 1:
        prefix = 5 + 2 * lensize + offsize
        if (len(data) < prefix or data[1] > 3 or data[2] not in (0, 1)):
            raise FormatError("invalid version-1 file-space strategy or persistence flag")
        persistent = bool(data[2])
        label = "v1_file_space_info_and_v0_file_manager"
        count = 12 if persistent else 0
        page_size = _uint(data[3 + lensize:3 + 2 * lensize]) if data[1] == 1 else 0
        if page_size and page_size & (page_size - 1):
            raise FormatError("paged free-space size is not a power of two")
        if data[1] == 1 and not page_size:
            raise FormatError("paged free-space size is zero")
    else:
        raise UnsupportedFormat("unknown file-space info message version")
    if len(data) != prefix + count * offsize:
        raise FormatError("file-space info message length contradicts strategy and persistence")
    addresses = [_uint(data[prefix + i * offsize:prefix + (i + 1) * offsize])
                 for i in range(count)]
    addresses = [address for address in addresses if address != sb.undefined_address]
    if len(addresses) != len(set(addresses)):
        raise FormatError("duplicate persistent file-space manager address")
    return addresses, label, page_size


def _rooted_addresses(reader):
    sb = reader.superblock
    offsize = sb.offset_size
    raw = reader._read_absolute(sb.signature_offset, 16 + 4 * offsize)
    extension = _uint(raw[12 + offsize:12 + 2 * offsize])
    if extension == sb.undefined_address:
        return [], [], "no_file_space_info", 0
    messages = _messages(reader, extension)
    information = _unique(messages, 0x17)
    if information is None:
        # The older 1.10.0 specification assigned 0x18 to file-space info;
        # newer versions assign it to a metadata-cache image instead. Their
        # fixed lengths differ for all supported superblock address widths.
        # Only the exact six-manager declaration layout is interpreted here.
        older = _unique(messages, 0x18)
        if older is None or len(older.data) not in (
                2 + sb.length_size, 2 + sb.length_size + 6 * offsize):
            return [], list(reader.metadata_ranges[1:]), "no_file_space_info", 0
        if not older.data or older.data[0] != 0:
            raise FormatError("historical file-space declaration version is invalid")
        information = older
    addresses, label, page_size = _information_addresses(information.data, information.flags, sb)
    if information.kind == 0x18:
        label = "v0_legacy_type_0x18_file_space_info_and_v0_file_manager"
    return addresses, list(reader.metadata_ranges[1:]), label, page_size


def _section_list(reader, owner, address, used, allocated, sections, maximum, address_bits,
                  budget, deadline, *, page_size=0, override=None, check_checksum=True):
    sb = reader.superblock
    if not used:
        if sections or allocated or address != sb.undefined_address:
            raise FormatError("persistent free-space section-list fields disagree")
        return [], None
    if (address == sb.undefined_address or used < 13 + 4 or used > allocated
            or allocated > budget.max_metadata_bytes):
        raise FormatError("persistent free-space section-list allocation is invalid")
    start = reader.absolute(address)
    if start + allocated > min(sb.eof_address, reader.size):
        raise FormatError("persistent free-space section list crosses EOF")
    _deadline(deadline)
    raw = override if override is not None else reader.read_at(address, used)
    if (raw[:5] != b"FSSE\x00" or _uint(raw[5:5 + sb.offset_size]) != owner
            or check_checksum and lookup3(raw[:-4]) != _uint(raw[-4:])):
        raise FormatError("persistent free-space section list has wrong signature, owner or checksum")
    count_width = max(1, (sections.bit_length() + 7) // 8)
    size_width = max(1, (maximum.bit_length() + 7) // 8)
    offset_width = max(1, (address_bits + 7) // 8)
    if max(count_width, size_width, offset_width) > 8:
        raise UnsupportedFormat("free-space section field width exceeds eight bytes")
    position, records, section_bytes = 5 + sb.offset_size, [], 0
    while position < used - 4:
        _deadline(deadline)
        if position + count_width + size_width > used - 4:
            raise FormatError("truncated persistent free-space section set")
        count = _uint(raw[position:position + count_width]); position += count_width
        length = _uint(raw[position:position + size_width]); position += size_width
        if not count or not length or length > maximum or count > sections - len(records):
            raise FormatError("persistent free-space section count or size contradicts header")
        if count > (used - 4 - position) // (offset_width + 1):
            raise FormatError("persistent free-space section records cross list boundary")
        for _ in range(count):
            offset = _uint(raw[position:position + offset_width]); position += offset_width
            typ = raw[position]; position += 1
            if (typ not in ((0, 1, 2) if page_size else (0,))
                    or offset + length > sb.eof_address - sb.base_address
                    or typ == 1 and offset // page_size != (offset + length - 1) // page_size):
                raise FormatError("persistent file free-space section has invalid type or extent")
            records.append((offset, length, typ))
            section_bytes += length
    if position != used - 4 or len(records) != sections:
        raise FormatError("persistent file free-space serialized section count disagrees")
    return records, (start, start + allocated, "rooted file free-space section list")


def _manager(reader, address, budget, deadline, *, override=None, check_checksum=True,
             check_sections=True, page_size=0):
    sb = reader.superblock
    offsize, lensize = sb.offset_size, sb.length_size
    size = 18 + 7 * lensize + offsize
    start = reader.absolute(address)
    raw = override if override is not None else reader.read_at(address, size)
    if len(raw) != size or raw[:6] != b"FSHD\x00\x01":
        raise FormatError("rooted file free-space manager header signature or client is invalid")
    if check_checksum and lookup3(raw[:-4]) != _uint(raw[-4:]):
        raise FormatError("rooted file free-space manager header checksum mismatch")
    total = _uint(raw[6:6 + lensize])
    count = _uint(raw[6 + lensize:6 + 2 * lensize])
    serialized = _uint(raw[6 + 2 * lensize:6 + 3 * lensize])
    unserialized = _uint(raw[6 + 3 * lensize:6 + 4 * lensize])
    config = 6 + 4 * lensize
    classes = _uint(raw[config:config + 2])
    shrink = _uint(raw[config + 2:config + 4])
    expand = _uint(raw[config + 4:config + 6])
    address_bits = _uint(raw[config + 6:config + 8])
    fields = config + 8
    maximum = _uint(raw[fields:fields + lensize]); fields += lensize
    section_address = _uint(raw[fields:fields + offsize]); fields += offsize
    used = _uint(raw[fields:fields + lensize]); fields += lensize
    allocated = _uint(raw[fields:fields + lensize])
    if (not 1 <= classes <= 16 or not 1 <= shrink <= 100 or not 100 <= expand <= 1000
            or not 1 <= address_bits <= 64 or not maximum or count != serialized + unserialized
            or count > budget.max_chunks or serialized > budget.max_chunks or total > reader.size
            or used > allocated or allocated > budget.max_metadata_bytes
            or used and (used < 13 + 4 or section_address == sb.undefined_address)
            or not used and (serialized or allocated or section_address != sb.undefined_address)):
        raise FormatError("rooted file free-space manager fields exceed checked bounds")
    if used and reader.absolute(section_address) + allocated > min(sb.eof_address, reader.size):
        raise FormatError("rooted file free-space section allocation crosses EOF")
    if check_sections:
        records, section_range = _section_list(reader, address, section_address, used, allocated,
            serialized, maximum, address_bits, budget, deadline, page_size=page_size)
        if sum(length for _, length, _ in records) > total:
            raise FormatError("serialized persistent free-space sections exceed declared total")
    else:
        section_range = None
    return {"address": address, "total_bytes": total, "section_count": count,
        "serialized_sections": serialized, "section_list_address": section_address,
        "serialized_section_types": dict(sorted(Counter(typ for _, _, typ in records).items())) if check_sections else {},
        "section_list_used_bytes": used, "section_list_allocated_bytes": allocated,
        "maximum_section_bytes": maximum, "address_bits": address_bits,
        "metadata_ranges": [(start, start + size, "rooted file free-space manager header")]
                           + ([section_range] if section_range else [])}


def inspect_file_space(path, budget=None):
    """Independently check declared persistent file free-space metadata.

    Missing declarations are an empty, successful inspection. The returned
    ranges are physical allocations, not inferred recoverable measurements.
    """
    path, budget = Path(path), budget or LargeBudget()
    deadline = time.monotonic() + budget.max_seconds
    if _version(path) < 2:
        return {"version": "legacy_not_covered", "managers": [], "metadata_ranges": []}
    with ModernH5File(path, large_sparse_scan=True) as reader:
        addresses, extension_ranges, version, page_size = _rooted_addresses(reader)
        managers = [_manager(reader, address, budget, deadline, page_size=page_size)
                    for address in addresses]
    ranges = extension_ranges + [item for manager in managers for item in manager["metadata_ranges"]]
    ordered = sorted(ranges)
    for left, right in zip(ordered, ordered[1:]):
        if left[1] > right[0]:
            raise FormatError("rooted persistent file free-space structures overlap")
    return {"version": version, "managers": managers,
            "metadata_ranges": [list(item) for item in ordered]}


def repair_file_space(path, touches, *, budget=None, allow_reseal=False):
    """Repair a header only with an original checksum, or explicit resealing.

    Each repaired header/list must pass a separate full section parse before
    the repaired copy can be published. Missing sections are never invented.
    """
    path, budget = Path(path), budget or LargeBudget()
    if _version(path) < 2:
        return
    deadline = time.monotonic() + budget.max_seconds
    with ModernH5File(path, large_sparse_scan=True) as reader:
        addresses, _, _, page_size = _rooted_addresses(reader)
        for address in addresses:
            _deadline(deadline)
            size = 18 + 7 * reader.superblock.length_size + reader.superblock.offset_size
            raw = reader.read_at(address, size)
            original = _uint(raw[-4:])
            if lookup3(raw[:-4]) == original:
                header = _manager(reader, address, budget, deadline,
                                  check_sections=False, page_size=page_size)
            else:
                mode = "original checksum restored"
                fixed = _fixed_prefix_restoration(raw, b"FSHD\x00\x01", original)
                if fixed is not None:
                    candidate = fixed
                    header = _manager(reader, address, budget, deadline,
                                      override=candidate, check_sections=False, page_size=page_size)
                    kind = "restore_file_space_header_prefix"
                else:
                    try:
                        offset, value = _unique_one_byte(raw, range(0, size - 4), original)
                        candidate = bytearray(raw)
                        candidate[offset] = value
                        header = _manager(reader, address, budget, deadline,
                                          override=bytes(candidate), check_sections=False, page_size=page_size)
                        kind = "restore_file_space_header_field"
                    except FormatError as exc:
                        if not allow_reseal or "no unique original-checksum correction" not in str(exc):
                            raise
                        header = _manager(reader, address, budget, deadline,
                                          check_checksum=False, check_sections=False, page_size=page_size)
                        candidate = bytearray(raw[:-4] + lookup3(raw[:-4]).to_bytes(4, "little"))
                        kind, mode = "reseal_file_space_header", "current structural coherence; checksum recomputed"
                physical = reader.absolute(address)
                with path.open("r+b") as output:
                    output.seek(physical)
                    output.write(candidate)
                touches.append((physical, physical + size, kind, mode))
            section_address = header["section_list_address"]
            used = header["section_list_used_bytes"]
            allocated = header["section_list_allocated_bytes"]
            count = header["serialized_sections"]
            maximum = header["maximum_section_bytes"]
            bits = header["address_bits"]
            if not used:
                _section_list(reader, address, section_address, used, allocated, count,
                              maximum, bits, budget, deadline, page_size=page_size)
                continue
            raw_list = reader.read_at(section_address, used)
            stored = _uint(raw_list[-4:])
            if lookup3(raw_list[:-4]) == stored:
                entries, _ = _section_list(reader, address, section_address, used, allocated,
                                           count, maximum, bits, budget, deadline, page_size=page_size)
            else:
                mode = "original checksum restored"
                fixed = _fixed_prefix_restoration(
                    raw_list, b"FSSE\x00" + address.to_bytes(reader.superblock.offset_size, "little"), stored)
                if fixed is not None:
                    candidate = fixed
                    entries, _ = _section_list(reader, address, section_address, used, allocated,
                        count, maximum, bits, budget, deadline, page_size=page_size,
                        override=candidate)
                    kind = "restore_file_space_section_prefix"
                else:
                    try:
                        if used > 1024:
                            raise FormatError("section list exceeds one-byte original-checksum trial budget")
                        position, value = _unique_one_byte(raw_list, range(0, used - 4), stored)
                        candidate = bytearray(raw_list)
                        candidate[position] = value
                        entries, _ = _section_list(reader, address, section_address, used, allocated,
                            count, maximum, bits, budget, deadline, page_size=page_size,
                            override=bytes(candidate))
                        kind = "restore_file_space_section_field"
                    except FormatError as exc:
                        bounded_trial_missed = (
                            "no unique original-checksum correction" in str(exc)
                            or "exceeds one-byte original-checksum trial budget" in str(exc)
                        )
                        if not allow_reseal or not bounded_trial_missed:
                            raise
                        entries, _ = _section_list(reader, address, section_address, used, allocated,
                            count, maximum, bits, budget, deadline, page_size=page_size,
                            check_checksum=False)
                        candidate = bytearray(raw_list[:-4] + lookup3(raw_list[:-4]).to_bytes(4, "little"))
                        kind, mode = "reseal_file_space_section_list", "rooted section structure and current coherence; checksum recomputed"
                section_physical = reader.absolute(section_address)
                with path.open("r+b") as output:
                    output.seek(section_physical)
                    output.write(candidate)
                touches.append((section_physical, section_physical + used, kind, mode))
            if sum(length for _, length, _ in entries) > header["total_bytes"]:
                raise FormatError("repaired file free-space sections exceed header total")
    # This second parse does not use the repaired in-memory candidates.
    inspect_file_space(path, budget)
