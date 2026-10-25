"""Check and repair a narrow, rooted persistent file free-space index.

Only version-1 file-space info messages in a checked modern superblock
extension and their version-0 file-client FSHD/FSSE pairs are covered.
Sections are parsed as evidence, never used to invent or overwrite objects.
"""

from pathlib import Path
import time

from .format import FormatError, SIGNATURE, UnsupportedFormat
from .large_streaming import LargeBudget, _deadline
from .metadata_correction import _unique_one_byte
from .metadata_fallback import _messages, _unique
from .modern_indexes import ModernH5File, lookup3


def _uint(raw):
    return int.from_bytes(raw, "little")


def _rooted_addresses(reader):
    sb = reader.superblock
    offsize = sb.offset_size
    raw = reader._read_absolute(sb.signature_offset, 16 + 4 * offsize)
    extension = _uint(raw[12 + offsize:12 + 2 * offsize])
    if extension == sb.undefined_address:
        return [], []
    messages = _messages(reader, extension)
    information = _unique(messages, 0x17)
    if information is None:
        return [], list(reader.metadata_ranges[1:])
    data = information.data
    if (information.flags & 2 or len(data) != 29 + 12 * offsize or data[:3] != b"\x01\x00\x01"):
        raise UnsupportedFormat("persistent file-space info message is outside the checked v1 encoding")
    addresses = [_uint(data[29 + i * offsize:29 + (i + 1) * offsize])
                 for i in range(12)]
    addresses = [address for address in addresses if address != sb.undefined_address]
    if len(addresses) != len(set(addresses)):
        raise FormatError("duplicate persistent file-space manager address")
    return addresses, list(reader.metadata_ranges[1:])


def _section_list(reader, owner, address, used, allocated, sections, maximum, address_bits, budget, deadline):
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
    raw = reader.read_at(address, used)
    if (raw[:5] != b"FSSE\x00" or _uint(raw[5:5 + sb.offset_size]) != owner
            or lookup3(raw[:-4]) != _uint(raw[-4:])):
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
            if typ != 0 or offset + length > sb.eof_address - sb.base_address:
                raise FormatError("persistent file free-space section has invalid type or extent")
            records.append((offset, length))
            section_bytes += length
    if position != used - 4 or len(records) != sections:
        raise FormatError("persistent file free-space serialized section count disagrees")
    return records, (start, start + allocated, "rooted file free-space section list")


def _manager(reader, address, budget, deadline, *, override=None, check_checksum=True):
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
            or count > budget.max_chunks or serialized > budget.max_chunks or total > reader.size):
        raise FormatError("rooted file free-space manager fields exceed checked bounds")
    records, section_range = _section_list(reader, address, section_address, used, allocated,
        serialized, maximum, address_bits, budget, deadline)
    if sum(length for _, length in records) > total:
        raise FormatError("serialized persistent free-space sections exceed declared total")
    return {"address": address, "total_bytes": total, "section_count": count,
        "serialized_sections": serialized, "section_list_address": section_address,
        "metadata_ranges": [(start, start + size, "rooted file free-space manager header")]
                           + ([section_range] if section_range else [])}


def inspect_file_space(path, budget=None):
    """Independently check declared persistent file free-space metadata.

    Missing declarations are an empty, successful inspection. The returned
    ranges are physical allocations, not inferred recoverable measurements.
    """
    path, budget = Path(path), budget or LargeBudget()
    deadline = time.monotonic() + budget.max_seconds
    with path.open("rb") as source:
        size, position = path.stat().st_size, 0
        while position + 9 <= size:
            source.seek(position)
            prefix = source.read(9)
            if prefix[:8] == SIGNATURE:
                break
            position = 512 if position == 0 else position * 2
        else:
            raise FormatError("cannot inspect file-space managers without a rooted superblock")
    if prefix[8] < 2:
        return {"version": "legacy_not_covered", "managers": [], "metadata_ranges": []}
    with ModernH5File(path, large_sparse_scan=True) as reader:
        addresses, extension_ranges = _rooted_addresses(reader)
        managers = [_manager(reader, address, budget, deadline) for address in addresses]
    ranges = extension_ranges + [item for manager in managers for item in manager["metadata_ranges"]]
    ordered = sorted(ranges)
    for left, right in zip(ordered, ordered[1:]):
        if left[1] > right[0]:
            raise FormatError("rooted persistent file free-space structures overlap")
    return {"version": "v1_file_space_info_and_v0_file_manager", "managers": managers,
            "metadata_ranges": [list(item) for item in ordered]}


def repair_file_space(path, touches, *, budget=None, allow_reseal=False):
    """Repair a header only with an original checksum, or explicit resealing.

    Full section-list parsing and its own original checksum are required for
    either operation. A damaged section list cannot be silently discarded.
    """
    path, budget = Path(path), budget or LargeBudget()
    deadline = time.monotonic() + budget.max_seconds
    with ModernH5File(path, large_sparse_scan=True) as reader:
        addresses, _ = _rooted_addresses(reader)
        for address in addresses:
            _deadline(deadline)
            size = 18 + 7 * reader.superblock.length_size + reader.superblock.offset_size
            raw = reader.read_at(address, size)
            original = _uint(raw[-4:])
            if lookup3(raw[:-4]) == original:
                _manager(reader, address, budget, deadline)
                continue
            mode = "original checksum restored"
            try:
                offset, value = _unique_one_byte(raw, range(4, size - 4), original)
                candidate = bytearray(raw)
                candidate[offset] = value
                _manager(reader, address, budget, deadline, override=bytes(candidate))
                kind = "restore_file_space_header_field"
            except FormatError as exc:
                if not allow_reseal or "no unique original-checksum correction" not in str(exc):
                    raise
                _manager(reader, address, budget, deadline, check_checksum=False)
                candidate = bytearray(raw[:-4] + lookup3(raw[:-4]).to_bytes(4, "little"))
                kind, mode = "reseal_file_space_header", "checked sections and current structural coherence; checksum recomputed"
            physical = reader.absolute(address)
            with path.open("r+b") as output:
                output.seek(physical)
                output.write(candidate)
            touches.append((physical, physical + size, kind, mode))
    # This second parse does not use the repaired in-memory candidates.
    inspect_file_space(path, budget)
