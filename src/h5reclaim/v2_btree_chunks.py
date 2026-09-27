"""Read an *anchored* HDF5 v2 B-tree dataset chunk index (types 10 and 11).

The entry address must come from the selected dataset's checksum-validated
layout message in the same immutable source snapshot. A free-standing BTHD,
BTIN, or BTLF signature never establishes ownership. Each returned record is
supported by a checked header, a chain of checked child pointers and counts,
its exact record slot, and a coordinate consistent with the selected layout.

One BTHD root or BTIN child pointer can be reconstructed when substituting
only that pointer restores the original parent checksum and a unique checked
node completes the tree consistently. It refuses an inconsistent tree as a
whole. It does not decode filters or interpret scientific datatypes.

HDF5 specification, III.A.2, B-tree types 10/11:
https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
"""

from __future__ import annotations

from dataclasses import dataclass
from math import prod
from typing import TYPE_CHECKING

from .format import FormatError, UnsupportedFormat
from .modern_link_repair import candidate_addresses
from .modern_indexes import (
    MAX_CHUNK_BYTES, MAX_CHUNKS, ModernChunk, ModernIndex, lookup3,
)

if TYPE_CHECKING:
    from .modern_indexes import ModernH5File


MAX_NODE_BYTES = 1 << 20
MAX_TREE_DEPTH = 4
MAX_VISITED_NODES = 8192
MAX_METADATA_BYTES = 64 << 20
MAX_REPAIR_CANDIDATES = 8192
MAX_REPAIR_SLOTS = 512


def _uint(raw: bytes) -> int:
    return int.from_bytes(raw, "little")


def _width(value: int) -> int:
    if value < 0:
        raise FormatError("negative B-tree capacity")
    return max(1, (value.bit_length() + 7) // 8)


@dataclass(frozen=True)
class _Header:
    tree_type: int
    node_size: int
    record_size: int
    depth: int
    root: int
    root_records: int
    total_records: int
    size: int


def _candidate_checked(reader: ModernH5File, address: int, *,
                       node_size: int, tree_type: int, record_size: int,
                       level: int, count: int, offsize: int,
                       max_records: list[int], max_subtree: list[int]) -> bool:
    """The expected child must be an independently checksummed complete node."""
    if count > max_records[level]:
        return False
    try:
        start = reader.absolute(address)
        if start + node_size > reader.superblock.eof_address:
            return False
        node = reader.read_at(address, node_size)
    except FormatError:
        return False
    if (node[:4] != (b"BTIN" if level else b"BTLF")
            or node[4] != 0 or node[5] != tree_type):
        return False
    end = 6 + count * record_size
    if level:
        width = offsize + _width(max_records[level - 1])
        if level > 1:
            width += _width(max_subtree[level - 1])
        end += (count + 1) * width
    return end + 4 <= node_size and lookup3(node[:end]) == _uint(node[end:end+4])


def _repair_pointer(reader: ModernH5File, raw: bytes, *,
                    pointer_slots: tuple[tuple[int, int], ...], checksum_at: int,
                    signature: bytes, child_level: int, node_size: int,
                    tree_type: int, record_size: int, offsize: int,
                    max_records: list[int], max_subtree: list[int]) -> tuple[int, int, bytes]:
    """Return (pointer position, unique child, parent with that one link fixed)."""
    if not pointer_slots or len(pointer_slots) > MAX_REPAIR_SLOTS:
        raise UnsupportedFormat("v2 B-tree repair pointer slot budget exceeded")
    wanted = _uint(raw[checksum_at:checksum_at+4])
    if checksum_at + 4 > len(raw) or lookup3(raw[:checksum_at]) == wanted:
        raise FormatError("v2 B-tree repair requires an invalid original parent checksum")
    matches: list[tuple[int, int, bytes]] = []
    count_candidates = 0
    for candidate in candidate_addresses(reader, signature):
        count_candidates += 1
        if count_candidates > MAX_REPAIR_CANDIDATES:
            raise UnsupportedFormat("v2 B-tree repair candidate budget exceeded")
        absolute = reader.superblock.base_address + candidate
        if any(absolute < end and start < absolute + node_size
               for start, end, _kind in reader.metadata_ranges):
            continue
        for position, child_count in pointer_slots:
            if _uint(raw[position:position+offsize]) == candidate:
                continue
            patched = bytearray(raw)
            patched[position:position+offsize] = candidate.to_bytes(offsize, "little")
            if lookup3(patched[:checksum_at]) != wanted:
                continue
            if not _candidate_checked(
                reader, candidate, node_size=node_size, tree_type=tree_type,
                record_size=record_size, level=child_level, count=child_count,
                offsize=offsize, max_records=max_records, max_subtree=max_subtree,
            ):
                continue
            matches.append((position, candidate, bytes(patched)))
            if len(matches) > 1:
                raise FormatError("v2 B-tree pointer reconstruction is ambiguous")
    if not matches:
        raise FormatError("v2 B-tree checksum mismatch; no unique checked child restores pointer")
    return matches[0]


def read_v2_btree_chunks(
    reader: ModernH5File,
    *,
    object_address: int,
    index_address: int,
    layout_version: int,
    layout_pointer_offset: int,
    shape: tuple[int, ...],
    chunk_shape: tuple[int, ...],
    element_size: int,
    filters: tuple[int, ...],
    maxshape: tuple[int | None, ...] | None = None,
    max_chunks: int = MAX_CHUNKS,
) -> ModernIndex:
    """Return checked ``ModernIndex`` records for an observed v2 B-tree.

    ``reader`` must already have parsed the same source's modern superblock
    and selected object header. ``index_address`` and
    ``layout_pointer_offset`` must be extracted from that object's layout
    message. These inputs are not independently authenticated by this API.
    """
    sb = reader.superblock
    shape = tuple(shape)
    chunk_shape = tuple(chunk_shape)
    filters = tuple(filters)
    if layout_version not in (4, 5):
        raise UnsupportedFormat("v2 B-tree chunk index requires layout version 4 or 5")
    if len(shape) not in (1, 2, 3, 4) or len(chunk_shape) != len(shape):
        raise UnsupportedFormat("v2 B-tree chunk index requires rank 1 through 4")
    if any(not isinstance(x, int) or x < 0 for x in shape):
        raise FormatError("invalid dataset dimensions")
    if any(not isinstance(x, int) or x <= 0 for x in (*chunk_shape, element_size)):
        raise FormatError("invalid chunk dimensions or element size")
    if maxshape is not None:
        if len(maxshape) != len(shape) or any(
            bound is not None and (not isinstance(bound, int) or bound < current)
            for bound, current in zip(maxshape, shape)
        ):
            raise FormatError("v2 B-tree maximum extents contradict current dimensions")
        if sum(bound is None for bound in maxshape) < 2:
            raise FormatError("v2 B-tree chunk index requires multiple unlimited dimensions")
    if max_chunks < 1 or max_chunks > MAX_CHUNKS:
        raise UnsupportedFormat("v2 B-tree record limit is out of range")
    if len(filters) > 32:
        raise UnsupportedFormat("v2 B-tree filter mask supports at most 32 filters")
    chunk_bytes = prod(chunk_shape) * element_size
    if chunk_bytes > MAX_CHUNK_BYTES:
        raise UnsupportedFormat("v2 B-tree unfiltered chunk exceeds byte limit")
    grid = tuple((s + c - 1) // c for s, c in zip(shape, chunk_shape))
    if prod(grid) > (1 << 63):
        raise UnsupportedFormat("v2 B-tree chunk grid exceeds coordinate limit")

    offsize = sb.offset_size
    header_size = 22 + offsize + sb.length_size
    raw = reader.read_at(index_address, header_size)
    if raw[:4] != b"BTHD" or raw[4] != 0:
        raise FormatError("v2 B-tree header signature or version is invalid")
    tree_type = raw[5]
    if tree_type != (11 if filters else 10):
        raise FormatError("v2 B-tree client type disagrees with selected filter pipeline")
    header_checksum_valid = lookup3(raw[:-4]) == _uint(raw[-4:])
    node_size = _uint(raw[6:10])
    record_size = _uint(raw[10:12])
    depth = _uint(raw[12:14])
    split, merge = raw[14:16]
    ptr = 16
    root = _uint(raw[ptr:ptr + offsize]); ptr += offsize
    root_records = _uint(raw[ptr:ptr + 2]); ptr += 2
    total_records = _uint(raw[ptr:ptr + sb.length_size])
    header = _Header(tree_type, node_size, record_size, depth, root,
                     root_records, total_records, header_size)
    if not (16 <= node_size <= MAX_NODE_BYTES) or record_size == 0:
        raise UnsupportedFormat("v2 B-tree node or record size is outside bounded parser")
    if depth > MAX_TREE_DEPTH:
        raise UnsupportedFormat("v2 B-tree depth exceeds bounded parser")
    if not (0 < split <= 100 and 0 < merge <= 100):
        raise FormatError("invalid v2 B-tree split or merge percentage")
    if total_records > max_chunks:
        raise UnsupportedFormat("v2 B-tree total record count exceeds limit")
    size_width = min(8, _width(chunk_bytes) + 1) if layout_version == 4 else offsize
    expected_size = offsize + len(shape) * 8 + (size_width + 4 if filters else 0)
    if record_size != expected_size:
        raise FormatError("v2 B-tree record size disagrees with selected layout")

    # Capacities define the on-disk widths of child counts. They are not used
    # to infer records or ownership. Every actual count is checked on traversal.
    max_records = [(node_size - 10) // record_size]
    if max_records[0] < 1:
        raise UnsupportedFormat("v2 B-tree node cannot contain a chunk record")
    max_subtree = [max_records[0]]
    for level in range(1, depth + 1):
        child_nrec_width = _width(max_records[level - 1])
        child_total_width = _width(max_subtree[level - 1]) if level > 1 else 0
        pointer_size = offsize + child_nrec_width + child_total_width
        capacity = (node_size - 10 - pointer_size) // (record_size + pointer_size)
        if capacity < 1:
            raise UnsupportedFormat("v2 B-tree internal node cannot hold records")
        max_records.append(capacity)
        max_subtree.append(capacity + (capacity + 1) * max_subtree[level - 1])
    if root_records > max_records[depth] or root_records > total_records:
        raise FormatError("v2 B-tree root record count exceeds header bounds")
    reconstructed_links: list[dict[str, object]] = []
    if not header_checksum_valid:
        if total_records == 0 or root_records == 0:
            raise FormatError("v2 B-tree header checksum mismatch without a usable root count")
        pointer_pos, candidate, _repaired = _repair_pointer(
            reader, raw, pointer_slots=((16, root_records),),
            checksum_at=header_size - 4,
            signature=b"BTIN" if depth else b"BTLF", child_level=depth,
            node_size=node_size, tree_type=tree_type, record_size=record_size,
            offsize=offsize, max_records=max_records, max_subtree=max_subtree)
        root = candidate
        reconstructed_links.append({
            "kind": "bthd_to_root", "source_address": index_address,
            "target_address": candidate,
            "pointer_offset": reader.absolute(index_address) + pointer_pos,
            "parent_checksum_offset": reader.absolute(index_address) + header_size - 4,
            "parent_kind": "v2 B-tree header", "child_kind": "v2 B-tree node",
            "resolution": "original BTHD checksum restored by one pointer; unique checked root and consistent full tree",
        })

    header_start = reader.absolute(index_address)
    known_ranges = list(reader.metadata_ranges)
    selected = reader.absolute(object_address)
    if not any(start == selected and kind == "selected object header"
               for start, _end, kind in known_ranges):
        raise FormatError("v2 B-tree selected object header has not been validated")
    if not any(start <= layout_pointer_offset and
               layout_pointer_offset + offsize <= end and
               kind in ("selected object header", "object header continuation")
               for start, end, kind in known_ranges):
        raise FormatError("v2 B-tree layout pointer is outside validated object metadata")
    if _uint(reader._read_absolute(layout_pointer_offset, offsize)) != index_address:
        raise FormatError("v2 B-tree layout pointer disagrees with selected index")
    local_ranges: list[tuple[int, int, str]] = []
    seen_nodes: set[int] = set()

    def reserve(address: int, length: int, label: str) -> int:
        start = reader.absolute(address)
        end = start + length
        if end > sb.eof_address:
            raise FormatError("v2 B-tree metadata crosses declared end-of-file")
        for before, after, existing in (*known_ranges, *local_ranges):
            if start < after and before < end:
                raise FormatError(f"v2 B-tree metadata overlaps {existing}")
        local_ranges.append((start, end, label))
        return start

    reserve(index_address, header_size, "v2 B-tree header")
    if total_records == 0:
        if root != sb.undefined_address or root_records:
            raise FormatError("nonempty v2 B-tree root for an empty index")
        reader.metadata_ranges.extend(local_ranges)
        return ModernIndex("v2_btree", layout_version, object_address,
                           index_address, layout_pointer_offset, chunk_shape,
                           element_size, (),
                           reconstructed_links=tuple(reconstructed_links))
    if root == sb.undefined_address or root_records == 0:
        raise FormatError("v2 B-tree header has no root for nonempty index")
    root_path: tuple[dict[str, object], ...] = ({
        "source_address": index_address,
        "target_address": root,
        "pointer_offset": header_start + 16,
        "pointer_length": offsize,
        "source_kind": "BTHD",
        "checksum_verified": True,
    },)

    def decode_record(node_address: int, node_start: int, raw: bytes,
                      position: int, chain: tuple[int, ...],
                      link_path: tuple[dict[str, object], ...]) -> ModernChunk:
        rec = raw[position:position + record_size]
        address = _uint(rec[:offsize])
        cursor = offsize
        if filters:
            size = _uint(rec[cursor:cursor + size_width]); cursor += size_width
            mask = _uint(rec[cursor:cursor + 4]); cursor += 4
            if mask >> len(filters):
                raise FormatError("v2 B-tree chunk filter mask exceeds pipeline")
        else:
            size, mask = chunk_bytes, 0
        scaled = tuple(_uint(rec[i:i + 8])
                       for i in range(cursor, len(rec), 8))
        if len(scaled) != len(shape) or any(s >= g for s, g in zip(scaled, grid)):
            raise FormatError("v2 B-tree record has a coordinate outside selected dataset")
        if size < 1 or size > MAX_CHUNK_BYTES:
            raise UnsupportedFormat("v2 B-tree stored chunk exceeds byte limit")
        physical = reader.absolute(address)
        if size > sb.eof_address - physical:
            raise FormatError("v2 B-tree chunk crosses declared end-of-file")
        coordinate = tuple(s * c for s, c in zip(scaled, chunk_shape))
        return ModernChunk(
            coordinate, address, size, mask,
            {"rule": "checked v2 B-tree record and parent child counts",
             "object_address": object_address, "layout_version": layout_version,
             "index_type": "v2_btree", "header_address": index_address,
             "tree_type": tree_type, "record_type": tree_type,
             "index_variant": "v2_btree_filtered" if filters else "v2_btree_unfiltered",
             "header_checksum_verified": True,
             "node_address": node_address,
             "node_chain": list(chain), "record_offset": node_start + position,
             "node_checksum_verified": True,
             "link_path": list(link_path)},
            pointer_offset=node_start + position,
        )

    def visit(address: int, level: int, count: int,
              chain: tuple[int, ...],
              link_path: tuple[dict[str, object], ...]) -> tuple[list[ModernChunk], int]:
        if address in seen_nodes or len(seen_nodes) >= MAX_VISITED_NODES:
            raise FormatError("repeated or excessive v2 B-tree node link")
        if (len(seen_nodes) + 1) * node_size > MAX_METADATA_BYTES:
            raise UnsupportedFormat("v2 B-tree metadata exceeds byte budget")
        seen_nodes.add(address)
        if not 0 <= count <= max_records[level]:
            raise FormatError("v2 B-tree child count exceeds node capacity")
        start = reserve(address, node_size, "v2 B-tree node")
        node = reader.read_at(address, node_size)
        signature = b"BTIN" if level else b"BTLF"
        if node[:4] != signature or node[4] != 0 or node[5] != tree_type:
            raise FormatError("v2 B-tree node signature, version, or client is invalid")
        records_end = 6 + count * record_size
        if level:
            child_nrec_width = _width(max_records[level - 1])
            child_total_width = _width(max_subtree[level - 1]) if level > 1 else 0
            child_entry = offsize + child_nrec_width + child_total_width
            checksum_start = records_end + (count + 1) * child_entry
        else:
            checksum_start = records_end
        if checksum_start + 4 > node_size:
            raise FormatError("v2 B-tree node records cross node boundary")
        observed_checksum = _uint(node[checksum_start:checksum_start + 4])
        if lookup3(node[:checksum_start]) != observed_checksum:
            if not level or reconstructed_links:
                raise FormatError("v2 B-tree node checksum mismatch")
            child_nrec_width = _width(max_records[level - 1])
            child_total_width = _width(max_subtree[level - 1]) if level > 1 else 0
            child_entry = offsize + child_nrec_width + child_total_width
            slots: list[tuple[int, int]] = []
            for i in range(count + 1):
                slot = records_end + i * child_entry
                child_count = _uint(node[slot+offsize:slot+offsize+child_nrec_width])
                if child_count > max_records[level - 1]:
                    raise FormatError("v2 B-tree damaged node has an invalid child count")
                slots.append((slot, child_count))
            pointer_pos, candidate, repaired = _repair_pointer(
                reader, node, pointer_slots=tuple(slots),
                checksum_at=checksum_start,
                signature=b"BTIN" if level > 1 else b"BTLF",
                child_level=level-1, node_size=node_size, tree_type=tree_type,
                record_size=record_size, offsize=offsize, max_records=max_records,
                max_subtree=max_subtree)
            node = repaired
            reconstructed_links.append({
                "kind": "btin_to_child", "source_address": address,
                "target_address": candidate,
                "pointer_offset": start + pointer_pos,
                "parent_checksum_offset": start + checksum_start,
                "parent_kind": "v2 B-tree node", "child_kind": "v2 B-tree node",
                "resolution": "original BTIN checksum restored by one pointer; unique checked child and consistent full tree",
            })
        # The remainder of an allocated node is unused; HDF5 need not zero it.
        parsed = [decode_record(address, start, node, 6 + i * record_size,
                                chain + (address,), link_path) for i in range(count)]
        if not level:
            return parsed, count
        children: list[tuple[int, int, int | None, int]] = []
        cursor = records_end
        for _ in range(count + 1):
            link_offset = start + cursor
            child_addr = _uint(node[cursor:cursor + offsize]); cursor += offsize
            child_count = _uint(node[cursor:cursor + child_nrec_width]); cursor += child_nrec_width
            child_total = None
            if level > 1:
                child_total = _uint(node[cursor:cursor + child_total_width])
                cursor += child_total_width
                if child_total < child_count or child_total > max_subtree[level - 1]:
                    raise FormatError("v2 B-tree child subtree total is inconsistent")
            if child_addr == sb.undefined_address:
                raise FormatError("v2 B-tree internal node has undefined child")
            children.append((child_addr, child_count, child_total, link_offset))
        traversed: list[ModernChunk] = []
        aggregate = count
        for i, (child_addr, child_count, expected_total, link_offset) in enumerate(children):
            child_path = link_path + ({
                "source_address": address,
                "target_address": child_addr,
                "pointer_offset": link_offset,
                "pointer_length": offsize,
                "source_kind": "BTIN",
                "checksum_verified": True,
            },)
            child_records, child_total = visit(child_addr, level - 1,
                                               child_count, chain + (address,),
                                               child_path)
            if expected_total is not None and child_total != expected_total:
                raise FormatError("v2 B-tree child subtree count contradicts link")
            traversed.extend(child_records)
            aggregate += child_total
            if i < count:
                traversed.append(parsed[i])
        return traversed, aggregate

    records, observed_total = visit(root, depth, root_records, (), root_path)
    if observed_total != total_records or len(records) != total_records:
        raise FormatError("v2 B-tree traversed record total contradicts header")
    if len(records) > max_chunks:
        raise UnsupportedFormat("v2 B-tree traversal exceeds chunk limit")
    previous: tuple[int, ...] | None = None
    payloads: list[tuple[int, int]] = []
    for rec in records:
        if previous is not None and rec.coordinate <= previous:
            raise FormatError("v2 B-tree chunk coordinates repeat or are out of order")
        previous = rec.coordinate
        start = reader.absolute(rec.address)
        end = start + rec.size
        for before, after, label in (*known_ranges, *local_ranges):
            if start < after and before < end:
                raise FormatError(f"v2 B-tree payload overlaps {label}")
        payloads.append((start, end))
    for (_start, end), (next_start, _next_end) in zip(
        sorted(payloads), sorted(payloads)[1:]
    ):
        if next_start < end:
            raise FormatError("v2 B-tree records claim overlapping chunk payloads")
    reader.metadata_ranges.extend(local_ranges)
    return ModernIndex("v2_btree", layout_version, object_address,
                       index_address, layout_pointer_offset, chunk_shape,
                       element_size, tuple(records),
                       reconstructed_links=tuple(reconstructed_links))
