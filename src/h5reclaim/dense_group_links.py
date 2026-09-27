"""Checksummed, rooted v2-B-tree and fractal-heap lookup for modern dense groups.

Only a group's own Link Info message can supply the heap and name-index
addresses. Every name record is reached through the checked type-5 B-tree,
its managed heap ID resolves through a checked FRHP/FHIB/FHDB chain, and its
name hash must match the indexed key. This intentionally refuses unknown
fractal-heap layouts rather than scanning blocks for plausible names.

Format: https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
sections III.A.2 (type 5), III.G, IV.A.3.c/g.
"""

from __future__ import annotations

from dataclasses import dataclass

from .format import FormatError, UnsupportedFormat
from .modern_indexes import ModernH5File, lookup3


MAX_LINKS = 4096
MAX_TREE_DEPTH = 3
MAX_NODE_SIZE = 1 << 20
MAX_BLOCK_SIZE = 1 << 20
MAX_METADATA_BYTES = 64 << 20
MAX_NAME_BYTES = 2048


def _uint(raw: bytes) -> int:
    return int.from_bytes(raw, "little")


def _width(value: int) -> int:
    return max(1, (value.bit_length() + 7) // 8)


def _power_two(value: int) -> bool:
    return value > 0 and value & (value - 1) == 0


@dataclass(frozen=True)
class DenseLink:
    name: str
    object_address: int | None
    btree_record_offset: int
    heap_record_offset: int
    name_hash: int


@dataclass(frozen=True)
class _Heap:
    address: int
    id_length: int
    flags: int
    managed_max: int
    managed_count: int
    max_bits: int
    start_size: int
    max_direct: int
    table_width: int
    root_address: int
    root_rows: int
    managed_space: int

    @property
    def offset_width(self) -> int:
        return (self.max_bits + 7) // 8

    @property
    def length_width(self) -> int:
        return _width(min(self.max_direct, self.managed_max))


def _reserve(reader: ModernH5File, pending: list[tuple[int, int, str]],
             address: int, size: int, label: str) -> int:
    start = reader.absolute(address)
    end = start + size
    if size < 1 or end > reader.superblock.eof_address:
        raise FormatError("dense-group metadata exceeds declared end-of-file")
    if sum(b-a for a, b, _ in pending) + size > MAX_METADATA_BYTES:
        raise UnsupportedFormat("dense-group metadata exceeds 64 MiB budget")
    for a, b, previous in (*reader.metadata_ranges, *pending):
        if start < b and a < end:
            raise FormatError(f"dense-group metadata overlaps parsed {previous}")
    pending.append((start, end, label))
    return start


def _read_heap(reader: ModernH5File, address: int,
               pending: list[tuple[int, int, str]]) -> _Heap:
    offsize, lsize = reader.superblock.offset_size, reader.superblock.length_size
    fixed_size = 26 + 3 * offsize + 12 * lsize
    # The fixed portion ends immediately before optional root filter data and
    # the checksum. Compute offsets from the format rather than magic values.
    pos = 0
    raw = reader.read_at(address, fixed_size)
    if raw[:4] != b"FRHP" or raw[4] != 0:
        raise FormatError("rooted fractal-heap header signature/version invalid")
    pos = 5
    id_length = _uint(raw[pos:pos+2]); pos += 2
    filter_length = _uint(raw[pos:pos+2]); pos += 2
    flags = raw[pos]; pos += 1
    managed_max = _uint(raw[pos:pos+4]); pos += 4
    if flags & ~3 or not flags & 2 or filter_length:
        raise UnsupportedFormat("dense-group heap needs checksummed, unfiltered direct blocks")
    if not 1 <= managed_max <= MAX_BLOCK_SIZE:
        raise UnsupportedFormat("dense-group managed object bound exceeds limit")
    pos += lsize  # next huge-object ID
    huge_index = _uint(raw[pos:pos+offsize]); pos += offsize
    pos += lsize  # free space
    pos += offsize  # managed free-space manager
    managed_space = _uint(raw[pos:pos+lsize]); pos += lsize
    allocated = _uint(raw[pos:pos+lsize]); pos += lsize
    pos += lsize  # iterator
    managed_count = _uint(raw[pos:pos+lsize]); pos += lsize
    pos += lsize  # huge size
    huge_count = _uint(raw[pos:pos+lsize]); pos += lsize
    pos += lsize  # tiny size
    tiny_count = _uint(raw[pos:pos+lsize]); pos += lsize
    table_width = _uint(raw[pos:pos+2]); pos += 2
    start_size = _uint(raw[pos:pos+lsize]); pos += lsize
    max_direct = _uint(raw[pos:pos+lsize]); pos += lsize
    max_bits = _uint(raw[pos:pos+2]); pos += 2
    start_rows = _uint(raw[pos:pos+2]); pos += 2
    root = _uint(raw[pos:pos+offsize]); pos += offsize
    rows = _uint(raw[pos:pos+2]); pos += 2
    if pos != fixed_size - 4:
        raise AssertionError("fractal-heap header size calculation disagrees")
    full = reader.read_at(address, fixed_size)
    if lookup3(full[:-4]) != _uint(full[-4:]):
        raise FormatError("fractal-heap header checksum mismatch")
    _reserve(reader, pending, address, fixed_size, "dense fractal-heap header")
    if (not _power_two(table_width) or table_width > 128
        or not _power_two(start_size) or start_size > MAX_BLOCK_SIZE
        or not _power_two(max_direct) or not start_size <= max_direct <= MAX_BLOCK_SIZE
        or not 1 <= max_bits <= 48 or rows > 16 or start_rows > 16):
        raise UnsupportedFormat("unsupported fractal-heap doubling-table parameters")
    if managed_count > MAX_LINKS or huge_count or tiny_count or huge_index != reader.superblock.undefined_address:
        raise UnsupportedFormat("dense-group heap has excessive or non-managed objects")
    if allocated > managed_space or managed_space > 1 << max_bits:
        raise FormatError("fractal-heap managed-space accounting is invalid")
    if id_length != 1 + (max_bits+7)//8 + _width(min(managed_max, max_direct)):
        raise UnsupportedFormat("fractal-heap managed ID width is unsupported")
    if root == reader.superblock.undefined_address and managed_count:
        raise FormatError("nonempty dense-group heap has no root block")
    return _Heap(address, id_length, flags, managed_max, managed_count, max_bits,
                 start_size, max_direct, table_width, root, rows, managed_space)


def _tree_records(reader: ModernH5File, index_address: int, id_length: int,
                  pending: list[tuple[int, int, str]]) -> tuple[tuple[int, bytes, int], ...]:
    offsize, lsize = reader.superblock.offset_size, reader.superblock.length_size
    length = 22 + offsize + lsize
    header = reader.read_at(index_address, length)
    if header[:4] != b"BTHD" or header[4] != 0 or header[5] != 5:
        raise FormatError("dense name index BTHD signature/client invalid")
    if lookup3(header[:-4]) != _uint(header[-4:]):
        raise FormatError("dense name index header checksum mismatch")
    node_size = _uint(header[6:10]); record_size = _uint(header[10:12])
    depth = _uint(header[12:14]); split, merge = header[14:16]
    root = _uint(header[16:16+offsize]); root_count = _uint(header[16+offsize:18+offsize])
    total = _uint(header[18+offsize:18+offsize+lsize])
    if (not 16 <= node_size <= MAX_NODE_SIZE or record_size != 4 + id_length
        or depth > MAX_TREE_DEPTH or not 0 < split <= 100 or not 0 < merge <= 100
        or total > MAX_LINKS):
        raise UnsupportedFormat("dense name index exceeds bounded type-5 parser")
    _reserve(reader, pending, index_address, length, "dense name-index BTHD")
    if not total:
        if root != reader.superblock.undefined_address or root_count:
            raise FormatError("empty dense name index has nonempty root")
        return ()
    if root == reader.superblock.undefined_address or root_count == 0:
        raise FormatError("nonempty dense name index has no root")
    capacities = [(node_size - 10) // record_size]
    subtree = [capacities[0]]
    if capacities[0] < 1:
        raise UnsupportedFormat("dense name-index leaf cannot hold a record")
    for level in range(1, depth+1):
        count_width = _width(capacities[level-1])
        total_width = _width(subtree[level-1]) if level > 1 else 0
        pointer_size = offsize + count_width + total_width
        capacity = (node_size - 10 - pointer_size) // (record_size + pointer_size)
        if capacity < 1:
            raise UnsupportedFormat("dense name-index internal node is too small")
        capacities.append(capacity)
        subtree.append(capacity + (capacity+1)*subtree[level-1])
    if root_count > capacities[depth] or root_count > total:
        raise FormatError("dense name-index root count exceeds capacity")
    seen: set[int] = set()

    def visit(address: int, level: int, count: int) -> tuple[list[tuple[int, bytes, int]], int]:
        if address in seen or len(seen) >= MAX_LINKS or not 0 <= count <= capacities[level]:
            raise FormatError("repeated, excessive, or invalid dense name-index node")
        seen.add(address)
        physical = _reserve(reader, pending, address, node_size, "dense name-index node")
        raw = reader.read_at(address, node_size)
        if raw[:4] != (b"BTIN" if level else b"BTLF") or raw[4] != 0 or raw[5] != 5:
            raise FormatError("dense name-index node signature/client invalid")
        cursor = 6 + count*record_size
        links = []
        if level:
            count_width = _width(capacities[level-1])
            total_width = _width(subtree[level-1]) if level > 1 else 0
            for _ in range(count+1):
                if cursor+offsize+count_width+total_width > node_size-4:
                    raise FormatError("dense name-index child pointers cross node")
                target = _uint(raw[cursor:cursor+offsize]); cursor += offsize
                child_count = _uint(raw[cursor:cursor+count_width]); cursor += count_width
                child_total = None
                if level > 1:
                    child_total = _uint(raw[cursor:cursor+total_width]); cursor += total_width
                if target == reader.superblock.undefined_address or child_count > capacities[level-1]:
                    raise FormatError("dense name-index child pointer or count invalid")
                links.append((target, child_count, child_total))
        if cursor+4 > node_size or lookup3(raw[:cursor]) != _uint(raw[cursor:cursor+4]):
            raise FormatError("dense name-index node checksum mismatch")
        records = [(_uint(raw[6+i*record_size:10+i*record_size]),
                    raw[10+i*record_size:6+(i+1)*record_size],
                    physical+6+i*record_size) for i in range(count)]
        if not level:
            return records, count
        ordered: list[tuple[int, bytes, int]] = []
        observed = count
        for i, (child, n, expected) in enumerate(links):
            result, actual = visit(child, level-1, n)
            if expected is not None and actual != expected:
                raise FormatError("dense name-index subtree count disagrees")
            ordered.extend(result)
            observed += actual
            if i < count:
                ordered.append(records[i])
        return ordered, observed

    records, observed = visit(root, depth, root_count)
    if observed != total or len(records) != total:
        raise FormatError("dense name-index total record count disagrees")
    if any(before[0] > after[0] for before, after in zip(records, records[1:])):
        raise FormatError("dense name-index hashes are not ordered")
    return tuple(records)


def _direct_block(reader: ModernH5File, heap: _Heap, offset: int,
                  pending: list[tuple[int, int, str]],
                  cache: dict[int, tuple[int, int, bytes]]) -> tuple[int, int, bytes]:
    if heap.root_rows == 0:
        address, size, block_offset = heap.root_address, heap.start_size, 0
    else:
        rows = heap.root_rows
        max_rows = (heap.max_direct.bit_length() - heap.start_size.bit_length()) + 2
        if rows > max_rows:
            raise UnsupportedFormat("indirect fractal-heap descendant is outside bounded route")
        count = rows*heap.table_width
        iblock_len = 5 + reader.superblock.offset_size + heap.offset_width + count*reader.superblock.offset_size + 4
        if iblock_len > MAX_BLOCK_SIZE:
            raise UnsupportedFormat("fractal-heap indirect block exceeds limit")
        if heap.root_address not in cache:
            _reserve(reader, pending, heap.root_address, iblock_len, "dense root FHIB")
            iblock = reader.read_at(heap.root_address, iblock_len)
            if (iblock[:4] != b"FHIB" or iblock[4] != 0
                or _uint(iblock[5:5+reader.superblock.offset_size]) != heap.address
                or _uint(iblock[5+reader.superblock.offset_size:
                                5+reader.superblock.offset_size+heap.offset_width]) != 0
                or lookup3(iblock[:-4]) != _uint(iblock[-4:])):
                raise FormatError("dense root FHIB signature, back-pointer, offset, or checksum invalid")
            cache[heap.root_address] = (-1, -1, iblock)
        iblock = cache[heap.root_address][2]
        cursor = 5 + reader.superblock.offset_size + heap.offset_width
        heap_cursor = 0
        slot = None
        for row in range(rows):
            block_size = heap.start_size if row < 2 else heap.start_size << (row-1)
            if block_size > MAX_BLOCK_SIZE:
                raise UnsupportedFormat("fractal-heap direct block exceeds limit")
            for col in range(heap.table_width):
                if heap_cursor <= offset < heap_cursor + block_size:
                    slot = (cursor, block_size, heap_cursor)
                    break
                cursor += reader.superblock.offset_size
                heap_cursor += block_size
            if slot is not None:
                break
        if slot is None:
            raise FormatError("managed heap ID lies outside root indirect coverage")
        pointer, size, block_offset = slot
        address = _uint(iblock[pointer:pointer+reader.superblock.offset_size])
        if address == reader.superblock.undefined_address:
            raise FormatError("managed heap ID references unallocated direct block")
    if address in cache and cache[address][0] != -1:
        actual_offset, actual_size, raw = cache[address]
        if (actual_offset, actual_size) != (block_offset, size):
            raise FormatError("fractal-heap direct block reused at conflicting offset")
        return actual_offset, actual_size, raw
    _reserve(reader, pending, address, size, "dense FHDB")
    raw = reader.read_at(address, size)
    osize = reader.superblock.offset_size
    position = 5+osize+heap.offset_width
    if (raw[:4] != b"FHDB" or raw[4] != 0
        or _uint(raw[5:5+osize]) != heap.address
        or _uint(raw[5+osize:position]) != block_offset):
        raise FormatError("dense FHDB signature, heap back-pointer, or block offset invalid")
    checksum = _uint(raw[position:position+4])
    if lookup3(raw[:position] + b"\x00"*4 + raw[position+4:]) != checksum:
        raise FormatError("dense FHDB checksum mismatch")
    cache[address] = (block_offset, size, raw)
    return block_offset, size, raw


def _link_from_id(reader: ModernH5File, heap: _Heap,
                  identifier: bytes, key_hash: int, record_offset: int,
                  pending: list[tuple[int, int, str]],
                  cache: dict[int, tuple[int, int, bytes]]) -> DenseLink:
    if len(identifier) != heap.id_length or identifier[0] != 0:
        raise UnsupportedFormat("dense link heap ID is not a bounded managed ID")
    width = heap.offset_width
    offset = _uint(identifier[1:1+width])
    size = _uint(identifier[1+width:])
    if size < 4 or size > min(heap.managed_max, MAX_NAME_BYTES+32):
        raise UnsupportedFormat("dense link record exceeds managed-object limit")
    if offset+size > heap.managed_space:
        raise FormatError("dense link heap ID exceeds managed space")
    block_offset, block_size, block = _direct_block(reader, heap, offset, pending, cache)
    first_data = 5 + reader.superblock.offset_size + heap.offset_width + 4
    relative = offset-block_offset
    if relative < first_data or relative+size > block_size:
        raise FormatError("dense link heap ID intersects direct-block header or boundary")
    data = block[relative:relative+size]
    if data[0] != 1 or data[1] & 0xE0:
        raise FormatError("dense link version or flags invalid")
    flags = data[1]
    pos = 2
    if flags & 8:
        if pos >= size or data[pos] not in (1, 64):
            raise UnsupportedFormat("unknown dense link type")
        link_type = data[pos]; pos += 1
    else:
        link_type = 0
    if flags & 4:
        pos += 8
    if flags & 16:
        if pos >= size or data[pos] != 1:
            raise UnsupportedFormat("dense link name charset unsupported")
        pos += 1
    nwidth = 1 << (flags & 3)
    if pos+nwidth > size:
        raise FormatError("truncated dense link name length")
    nlength = _uint(data[pos:pos+nwidth]); pos += nwidth
    if not 1 <= nlength <= MAX_NAME_BYTES or pos+nlength > size:
        raise FormatError("dense link name length is invalid")
    name_bytes = data[pos:pos+nlength]; pos += nlength
    if lookup3(name_bytes) != key_hash:
        raise FormatError("dense name-index hash contradicts heap link name")
    try:
        name = name_bytes.decode("utf-8" if flags & 16 else "ascii", "strict")
    except UnicodeDecodeError as exc:
        raise FormatError("dense link name encoding is invalid") from exc
    if "\x00" in name or "/" in name or name in (".", ".."):
        raise FormatError("dense link name is not a canonical component")
    if link_type:
        if size-pos < 2 or size != pos+2+_uint(data[pos:pos+2]):
            raise FormatError("dense nonhard link target length invalid")
        target = None
    else:
        osize = reader.superblock.offset_size
        if size-pos != osize:
            raise FormatError("dense hard-link target address length invalid")
        target = _uint(data[pos:])
        reader.absolute(target)
    # Absolute record byte within the direct block's physical allocation.
    direct_address = next(a for a, (o, s, _b) in cache.items()
                          if o == block_offset and s == block_size)
    return DenseLink(name, target, record_offset,
                     reader.absolute(direct_address)+relative, key_hash)


def read_dense_group_links(
    reader: ModernH5File, *, group_address: int,
    link_info: bytes, link_info_offset: int,
) -> tuple[DenseLink, ...]:
    """Validate and return every local dense name-index record (max 4,096).

    The caller must supply the Link Info message bytes and absolute data
    offset from the `group_address` object's checked header in this reader.
    The addresses are derived here, never taken from an independent hint.
    """
    osize = reader.superblock.offset_size
    if not any(start == reader.absolute(group_address) and kind == "rooted object header"
               for start, _end, kind in reader.metadata_ranges):
        raise FormatError("dense group object header has not been validated")
    if (not isinstance(link_info, bytes) or len(link_info) < 2
        or link_info[0] != 0 or link_info[1] & ~3):
        raise FormatError("dense group Link Info version or flags invalid")
    position = 2 + (8 if link_info[1] & 1 else 0)
    expected = position + (3 if link_info[1] & 2 else 2)*osize
    if len(link_info) != expected:
        raise FormatError("dense group Link Info field length invalid")
    if not any(start == reader.absolute(group_address) and
               start <= link_info_offset and link_info_offset+len(link_info) <= end and
               kind == "rooted object header"
               for start, end, kind in reader.metadata_ranges):
        raise FormatError("dense Link Info is outside validated group header")
    if reader._read_absolute(link_info_offset, len(link_info)) != link_info:
        raise FormatError("dense Link Info bytes disagree with rooted group header")
    heap_address = _uint(link_info[position:position+osize])
    name_index_address = _uint(link_info[position+osize:position+2*osize])
    if (heap_address == reader.superblock.undefined_address
        or name_index_address == reader.superblock.undefined_address):
        raise FormatError("dense group lacks heap or name-index pointer")
    pending: list[tuple[int, int, str]] = []
    heap = _read_heap(reader, heap_address, pending)
    indexed = _tree_records(reader, name_index_address, heap.id_length, pending)
    if len(indexed) != heap.managed_count:
        raise FormatError("dense heap managed-object count differs from name index")
    cache: dict[int, tuple[int, int, bytes]] = {}
    links: list[DenseLink] = []
    names: set[str] = set()
    ids: set[bytes] = set()
    for key_hash, identifier, physical in indexed:
        if identifier in ids:
            raise FormatError("dense name index repeats a heap object ID")
        ids.add(identifier)
        link = _link_from_id(reader, heap, identifier, key_hash, physical, pending, cache)
        if link.name in names:
            raise FormatError("dense group has duplicate name")
        names.add(link.name)
        links.append(link)
    reader.metadata_ranges.extend(pending)
    return tuple(links)
