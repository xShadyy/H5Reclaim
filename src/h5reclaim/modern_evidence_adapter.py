"""Translate checksum-verified modern index rules into the evidence ledger."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from .evidence import (
    ChecksumEvidence, ChunkProposal, DatasetAnchor, EvidenceCheck, EvidenceReport,
    IndexLink, PhysicalExtent, SourceRecord, reconcile,
)
from .metadata import DatasetSpec
from .modern_indexes import ModernH5File, ModernIndex
from .schema_codec import fletcher32_applied


class ModernEvidenceError(ValueError):
    """A modern-format proposal disagrees with independently parsed evidence."""


_PASS = tuple(EvidenceCheck(code, "pass") for code in (
    "allocation", "coordinates", "datatype", "decoded_bytes", "filter_pipeline",
))


def _ea_page_rule(reader: ModernH5File, index: ModernIndex,
                  step: Mapping[str, object], preceding: Mapping[str, object]) -> bool:
    """Independently derive a paged EA bitmap bit from checksummed headers."""
    sb = reader.superblock
    root = reader.read_at(index.base_address, 12)
    max_bits, min_elems, page_bits = root[7], root[9], root[11]
    if not min_elems or min_elems & (min_elems - 1) or not 1 <= page_bits <= 24:
        return False
    offset_width = (max_bits + 7) // 8
    sblock_address = preceding.get("parent_address")
    block_address = step.get("parent_address")
    page_index = step.get("page_index")
    if not all(isinstance(x, int) for x in (sblock_address, block_address, page_index)):
        return False
    sblock = reader.read_at(sblock_address, 6 + sb.offset_size + offset_width)
    start = int.from_bytes(sblock[6+sb.offset_size:], "little")
    dblock = reader.read_at(block_address, 6 + sb.offset_size + offset_width)
    offset = int.from_bytes(dblock[6+sb.offset_size:], "little")
    row_start = 0
    for row in range(1 + max_bits - (min_elems.bit_length() - 1)):
        blocks = 1 << (row // 2)
        elements = min_elems << ((row + 1) // 2)
        if row_start == start:
            page_elems = 1 << page_bits
            if (elements <= page_elems or elements % page_elems
                    or offset < start or (offset - start) % elements):
                return False
            block_index = (offset - start) // elements
            page_count = elements // page_elems
            bit = block_index * page_count + page_index
            return (block_index < blocks and 0 <= page_index < page_count
                    and step.get("bitmap_bit") == bit
                    and step.get("bitmap_offset") == reader.absolute(sblock_address)
                    + 6 + sb.offset_size + offset_width + bit // 8)
        row_start += blocks * elements
    return False


def build_modern_evidence(
    spec: DatasetSpec, reader: ModernH5File, index: ModernIndex,
    records: Sequence[Any], *, source_sha256: str,
    decode_chunk: Callable[[bytes, DatasetSpec, int], bytes],
    failed: Sequence[Mapping[str, Any]] = (),
    rooted_metadata_ranges: Sequence[tuple[int, int, str]] = (),
    source_id: str = "damaged",
) -> EvidenceReport:
    """Reconcile parser-derived chunks before any output can be published.

    Implicit-index link addresses are *computed* from the one observed base
    pointer and validated row-major grid order. The pointer is not represented
    as if the file contained one literal address per chunk.
    """
    try:
        return _build(spec, reader, index, records, source_sha256,
                      decode_chunk, failed, source_id, rooted_metadata_ranges)
    except (IndexError, KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, ModernEvidenceError):
            raise
        raise ModernEvidenceError(f"cannot justify modern chunk evidence: {exc}") from exc


def _build(
    spec: DatasetSpec, reader: ModernH5File, index: ModernIndex,
    records: Sequence[Any], source_sha256: str,
    decode_chunk: Callable[[bytes, DatasetSpec, int], bytes],
    failed: Sequence[Mapping[str, Any]], source_id: str,
    rooted_metadata_ranges: Sequence[tuple[int, int, str]],
) -> EvidenceReport:
    if index.object_address != spec.object_address or index.chunk_shape != spec.chunks:
        raise ModernEvidenceError("index belongs to a different selected object or chunk shape")
    element_size = np.dtype(spec.dtype).itemsize
    if index.element_size != element_size:
        raise ModernEvidenceError("index element size disagrees with selected datatype")
    header_start = reader.absolute(spec.object_address)
    headers = [(start, end) for start, end, kind in reader.metadata_ranges
               if kind == "selected object header" and start == header_start]
    if len(headers) != 1:
        raise ModernEvidenceError("selected object header is not uniquely anchored")
    header_start, header_end = headers[0]
    pointer_start = index.layout_pointer_offset
    pointer_end = pointer_start + reader.superblock.offset_size
    if not any(start <= pointer_start and pointer_end <= end
               for start, end, kind in reader.metadata_ranges
               if kind in ("selected object header", "object header continuation")):
        raise ModernEvidenceError("layout base pointer lies outside validated object metadata")
    width = reader.superblock.offset_size
    if int.from_bytes(reader._read_absolute(pointer_start, width), "little") != index.base_address:
        raise ModernEvidenceError("layout pointer bytes disagree with parsed root address")
    anchor = DatasetAnchor(
        source_id, spec.path,
        PhysicalExtent(source_id, header_start, header_end - header_start),
        header_start, spec.shape, spec.chunks, element_size,
    )
    source = SourceRecord(source_id, reader.size, source_sha256)
    by_coordinate = {item.coordinate: item for item in index.chunks}
    if len(by_coordinate) != len(index.chunks):
        raise ModernEvidenceError("modern index proposes duplicate coordinates")
    pointer = PhysicalExtent(source_id, pointer_start, reader.superblock.offset_size)
    links: list[IndexLink] = []
    shared_path: tuple[str, ...] = ()
    if index.index_type in ("fixed_array", "extensible_array") and index.chunks:
        if index.data_block_address is None or index.data_block_pointer_offset is None:
            raise ModernEvidenceError("fixed-array index lacks data-block attribution")
        fa_header = reader.absolute(index.base_address)
        fa_block = reader.absolute(index.data_block_address)
        fa_pointer = index.data_block_pointer_offset
        header_kind = "fixed-array header" if index.index_type == "fixed_array" else "extensible-array header"
        if not any(start <= fa_pointer and fa_pointer + width <= end
                   for start, end, kind in reader.metadata_ranges
                   if kind == header_kind):
            raise ModernEvidenceError("index data-block pointer is not in its header")
        if int.from_bytes(reader._read_absolute(fa_pointer, width), "little") != index.data_block_address:
            raise ModernEvidenceError("index header pointer bytes disagree with parsed data block")
        prefix = "fixed" if index.index_type == "fixed_array" else "extensible"
        links.extend((
            IndexLink(f"layout:{prefix}-header", source_id, spec.path,
                      header_start, fa_header, "observed_index", pointer,
                      checks=(EvidenceCheck("address_rule", "pass", "validated layout points to index header"),)),
            IndexLink(f"{prefix}-header:data-block", source_id, spec.path,
                      fa_header, fa_block, "observed_index",
                      PhysicalExtent(source_id, fa_pointer, width),
                      checks=(EvidenceCheck("address_rule", "pass", "validated header points to index block"),)),
        ))
        shared_path = (f"layout:{prefix}-header", f"{prefix}-header:data-block")
    elif index.index_type == "v2_btree" and index.chunks:
        links.append(IndexLink(
            "layout:btree-header", source_id, spec.path,
            header_start, reader.absolute(index.base_address), "observed_index", pointer,
            checks=(EvidenceCheck("address_rule", "pass", "validated layout points to BTHD"),),
        ))
        shared_path = ("layout:btree-header",)
    proposals: list[ChunkProposal] = []
    linked_coordinates: set[tuple[int, ...]] = set()

    def add_proposal(
        proposal_id: str, coordinate: tuple[int, ...], address: int,
        accepted_payload: bytes | None, failure: str = "",
    ) -> None:
        chunk = by_coordinate.get(coordinate)
        if chunk is None or chunk.address != address or coordinate in linked_coordinates:
            raise ModernEvidenceError("proposal is not a unique record in the selected index")
        linked_coordinates.add(coordinate)
        absolute = reader.absolute(address)
        raw = reader.read_at(address, chunk.size)
        link_id = f"payload:{len(linked_coordinates)}"
        path = list(shared_path)
        if index.index_type in ("fixed_array", "extensible_array"):
            if chunk.pointer_offset is None or index.data_block_address is None:
                raise ModernEvidenceError("array chunk lacks slot pointer")
            fa_block = reader.absolute(index.data_block_address)
            parent = fa_block
            valid_kinds = (
                ("fixed-array data block", "fixed-array data block page")
                if index.index_type == "fixed_array" else
                ("extensible-array index block", "extensible-array data block",
                 "extensible-array data block page")
            )
            if index.index_type == "extensible_array":
                chain = chunk.evidence.get("index_chain")
                if not isinstance(chain, (list, tuple)) or len(chain) < 2:
                    raise ModernEvidenceError("extensible-array slot has no rooted index path")
                if (chain[0].get("target_address") != index.base_address
                        or chain[1].get("target_address") != index.data_block_address):
                    raise ModernEvidenceError("extensible-array index chain disagrees with header")
                for number, step in enumerate(chain[2:]):
                    if not isinstance(step, dict):
                        raise ModernEvidenceError("invalid extensible-array chain step")
                    if step.get("kind") == "eadb_to_page":
                        # Pages have no literal parent pointer. The EASB bitmap
                        # and fixed contiguous allocation prove this offset;
                        # retain EADB as the conceptual parent in the ledger.
                        page_address = step.get("target_address")
                        page_index = step.get("page_index")
                        page_size = step.get("page_size")
                        block_size = step.get("data_block_size")
                        bitmap_offset = step.get("bitmap_offset")
                        bitmap_bit = step.get("bitmap_bit")
                        block_address = step.get("parent_address")
                        header = reader.read_at(index.base_address, 12)
                        expected_page_size = (1 << header[11]) * header[6] + 4
                        if (number != len(chain[2:]) - 1 or number < 1
                                or chain[number + 1].get("kind") != "easb_to_eadb"
                                or chain[number + 1].get("target_address") != block_address
                                or not all(isinstance(x, int) for x in (
                                    page_address, page_index, page_size, block_size,
                                    bitmap_offset, bitmap_bit, block_address))
                                or page_index < 0 or page_size != expected_page_size
                                or not _ea_page_rule(reader, index, step, chain[number + 1])
                                or reader.absolute(block_address) != parent
                                or page_address != block_address + block_size + page_index * page_size
                                or page_address != chunk.evidence.get("slot_owner_address")
                                or not any(start == parent and end - start == block_size
                                           for start, end, kind in reader.metadata_ranges
                                           if kind == "extensible-array data block")
                                or not any(start == reader.absolute(page_address)
                                           and end - start == page_size
                                           for start, end, kind in reader.metadata_ranges
                                           if kind == "extensible-array data block page")
                                or not any(start == reader.absolute(chain[number + 1]["parent_address"])
                                           and start <= bitmap_offset < end
                                           for start, end, kind in reader.metadata_ranges
                                           if kind == "extensible-array secondary block")
                                or not reader._read_absolute(bitmap_offset, 1)[0]
                                       & (0x80 >> (bitmap_bit % 8))):
                            raise ModernEvidenceError("extensible-array page has no checked bitmap and offset")
                        continue
                    parent_address, target = step["parent_address"], step["target_address"]
                    offset = step["pointer_offset"]
                    if (not isinstance(offset, int) or not isinstance(target, int)
                            or reader.absolute(parent_address) != parent):
                        raise ModernEvidenceError("extensible-array child pointer has a different owner")
                    if not any(start <= offset and offset + width <= end
                               for start, end, kind in reader.metadata_ranges
                               if start == parent and kind in
                               ("extensible-array index block", "extensible-array secondary block")):
                        raise ModernEvidenceError("extensible-array child pointer is outside checked metadata")
                    if int.from_bytes(reader._read_absolute(offset, width), "little") != target:
                        raise ModernEvidenceError("extensible-array child pointer bytes disagree")
                    child = reader.absolute(target)
                    edge = f"array:{len(linked_coordinates)}:{number}"
                    links.append(IndexLink(
                        edge, source_id, spec.path, parent, child, "observed_index",
                        PhysicalExtent(source_id, offset, width),
                        checks=(EvidenceCheck("address_rule", "pass", str(step["kind"])),),
                    ))
                    path.append(edge)
                    parent = child
            if not any(start <= chunk.pointer_offset and chunk.pointer_offset + width <= end
                       for start, end, kind in reader.metadata_ranges
                       if kind in valid_kinds and
                       start == reader.absolute(chunk.evidence.get(
                           "slot_owner_address", index.data_block_address))):
                raise ModernEvidenceError("array chunk pointer lies outside checked slot metadata")
            if index.index_type == "fixed_array":
                page = chunk.evidence.get("computed_page")
                if page is not None:
                    if (not isinstance(page, dict)
                            or page.get("address") != chunk.evidence.get("slot_owner_address")
                            or page.get("offset_from_data_block") !=
                               page.get("address") - index.data_block_address
                            or not page.get("bitmap_initialized")
                            or page.get("page_index") != chunk.evidence.get("page_index")):
                        raise ModernEvidenceError("fixed-array computed page attribution is inconsistent")
            if int.from_bytes(reader._read_absolute(chunk.pointer_offset, width), "little") != address:
                raise ModernEvidenceError("array slot bytes disagree with proposal address")
            link_pointer = PhysicalExtent(source_id, chunk.pointer_offset, width)
            rule = "validated array slot and grid mapping give chunk address and coordinate"
        elif index.index_type == "v2_btree":
            parent = reader.absolute(index.base_address)
            chain = chunk.evidence.get("link_path")
            if not isinstance(chain, (list, tuple)) or not chain:
                raise ModernEvidenceError("v2 B-tree record has no rooted node chain")
            for number, step in enumerate(chain):
                if not isinstance(step, dict):
                    raise ModernEvidenceError("invalid v2 B-tree node link")
                target, offset, pointer_length = (step["target_address"],
                                                  step["pointer_offset"], step["pointer_length"])
                if (not step.get("checksum_verified") or step["source_address"] !=
                        (index.base_address if number == 0 else chain[number - 1]["target_address"])
                        or step["pointer_length"] != width
                        or not isinstance(offset, int) or not isinstance(target, int)
                        or not any(start == parent and start <= offset and offset + pointer_length <= end
                                   for start, end, kind in reader.metadata_ranges
                                   if kind in ("v2 B-tree header", "v2 B-tree node"))
                        or int.from_bytes(reader._read_absolute(offset, width), "little") != target):
                    raise ModernEvidenceError("v2 B-tree node pointer is not verified in selected index")
                child = reader.absolute(target)
                edge = f"btree:{len(linked_coordinates)}:{number}"
                links.append(IndexLink(
                    edge, source_id, spec.path, parent, child, "observed_index",
                    PhysicalExtent(source_id, offset, width),
                    checks=(EvidenceCheck("metadata_checksum", "pass", str(step["source_kind"])),),
                ))
                path.append(edge)
                parent = child
            if (chunk.pointer_offset is None or
                not any(start == parent and start <= chunk.pointer_offset and chunk.pointer_offset + width <= end
                        for start, end, kind in reader.metadata_ranges if kind == "v2 B-tree node")
                or int.from_bytes(reader._read_absolute(chunk.pointer_offset, width), "little") != address):
                raise ModernEvidenceError("v2 B-tree record pointer is outside checked node")
            link_pointer = PhysicalExtent(source_id, chunk.pointer_offset, width)
            rule = "validated B-tree record gives a scaled chunk coordinate"
        else:
            parent = header_start
            link_pointer = pointer
            rule = ("direct address stored in validated layout" if index.index_type == "single_chunk"
                    else "validated layout base plus row-major chunk size times grid index")
        links.append(IndexLink(
            link_id, source_id, spec.path, parent, absolute,
            "observed_index", link_pointer,
            checks=(EvidenceCheck("address_rule", "pass", rule),),
        ))
        if accepted_payload is None:
            try:
                decode_chunk(raw, spec, chunk.filter_mask)
            except ValueError:
                pass
            else:
                raise ModernEvidenceError("reported decode failure no longer reproduces")
            checks = tuple(c for c in _PASS if c.code != "decoded_bytes") + (
                EvidenceCheck("decoded_bytes", "fail", failure),
            )
            decoded_sha = decoded_length = None
            checked = fletcher32_applied(spec, chunk.filter_mask)
            checksum = ChecksumEvidence(
                "fletcher32" if checked else "none", None,
                "failed" if "Fletcher32 checksum mismatch" in failure else "unverified",
            )
        else:
            payload = decode_chunk(raw, spec, chunk.filter_mask)
            if payload != accepted_payload or len(payload) != spec.chunk_bytes:
                raise ModernEvidenceError("accepted payload disagrees with indexed source bytes")
            decoded_sha = hashlib.sha256(payload).hexdigest()
            decoded_length = len(payload)
            checks = _PASS
            checked = fletcher32_applied(spec, chunk.filter_mask)
            checksum = ChecksumEvidence(
                "fletcher32" if checked else "none", None,
                "passed" if checked else "absent",
            )
        proposals.append(ChunkProposal(
            proposal_id, PhysicalExtent(source_id, absolute, chunk.size),
            hashlib.sha256(raw).hexdigest(), spec.path, coordinate,
            tuple(path) + (link_id,), decoded_sha, decoded_length, chunk.filter_mask,
            checksum, checks,
        ))

    for i, record in enumerate(records):
        coordinate = tuple(record.coordinate)
        chunk = by_coordinate.get(coordinate)
        if (
            chunk is None or record.file_address != chunk.address
            or record.absolute_offset != reader.absolute(chunk.address)
            or record.length != chunk.size
            or tuple(record.index) != tuple(c // w for c, w in zip(coordinate, spec.chunks))
        ):
            raise ModernEvidenceError("accepted record conflicts with modern chunk index")
        add_proposal(f"accepted:{i}", coordinate, chunk.address, record.payload)
    for i, item in enumerate(failed):
        coordinate = tuple(int(v) for v in item["coordinate"])
        address = int(item["source_address"])
        if tuple(item["chunk_index"]) != tuple(c // w for c, w in zip(coordinate, spec.chunks)):
            raise ModernEvidenceError("decode-failed chunk coordinate is inconsistent")
        add_proposal(f"decode_failed:{i}", coordinate, address, None, str(item["reason"]))
    if len(linked_coordinates) != len(index.chunks):
        raise ModernEvidenceError("modern index chunks missing from accepted/failed evidence")
    metadata = tuple(PhysicalExtent(source_id, start, end - start)
                     for start, end, _ in (*reader.metadata_ranges, *rooted_metadata_ranges))
    ledger = reconcile((source,), (anchor,), tuple(links), tuple(proposals), metadata)
    for decision in ledger.decisions[:len(records)]:
        if decision.status != "accepted":
            raise ModernEvidenceError(
                f"accepted modern chunk has unresolved evidence: {decision.reasons}"
            )
    if any(decision.status == "accepted" for decision in ledger.decisions[len(records):]):
        raise ModernEvidenceError("decode-failed modern chunk was accepted")
    return ledger
