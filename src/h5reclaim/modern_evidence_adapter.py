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


class ModernEvidenceError(ValueError):
    """A modern-format proposal disagrees with independently parsed evidence."""


_PASS = tuple(EvidenceCheck(code, "pass") for code in (
    "allocation", "coordinates", "datatype", "decoded_bytes", "filter_pipeline",
))


def build_modern_evidence(
    spec: DatasetSpec, reader: ModernH5File, index: ModernIndex,
    records: Sequence[Any], *, source_sha256: str,
    decode_chunk: Callable[[bytes, DatasetSpec, int], bytes],
    failed: Sequence[Mapping[str, Any]] = (), source_id: str = "damaged",
) -> EvidenceReport:
    """Reconcile parser-derived chunks before any output can be published.

    Implicit-index link addresses are *computed* from the one observed base
    pointer and validated row-major grid order. The pointer is not represented
    as if the file contained one literal address per chunk.
    """
    try:
        return _build(spec, reader, index, records, source_sha256,
                      decode_chunk, failed, source_id)
    except (IndexError, KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, ModernEvidenceError):
            raise
        raise ModernEvidenceError(f"cannot justify modern chunk evidence: {exc}") from exc


def _build(
    spec: DatasetSpec, reader: ModernH5File, index: ModernIndex,
    records: Sequence[Any], source_sha256: str,
    decode_chunk: Callable[[bytes, DatasetSpec, int], bytes],
    failed: Sequence[Mapping[str, Any]], source_id: str,
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
    if index.index_type == "fixed_array":
        if index.data_block_address is None or index.data_block_pointer_offset is None:
            raise ModernEvidenceError("fixed-array index lacks data-block attribution")
        fa_header = reader.absolute(index.base_address)
        fa_block = reader.absolute(index.data_block_address)
        fa_pointer = index.data_block_pointer_offset
        if not any(start <= fa_pointer and fa_pointer + width <= end
                   for start, end, kind in reader.metadata_ranges
                   if kind == "fixed-array header"):
            raise ModernEvidenceError("fixed-array data-block pointer is not in its header")
        if int.from_bytes(reader._read_absolute(fa_pointer, width), "little") != index.data_block_address:
            raise ModernEvidenceError("fixed-array header pointer bytes disagree with parsed data block")
        links.extend((
            IndexLink("layout:fixed-header", source_id, spec.path,
                      header_start, fa_header, "observed_index", pointer,
                      checks=(EvidenceCheck("address_rule", "pass", "layout points to FAHD"),)),
            IndexLink("fixed-header:data-block", source_id, spec.path,
                      fa_header, fa_block, "observed_index",
                      PhysicalExtent(source_id, fa_pointer, width),
                      checks=(EvidenceCheck("address_rule", "pass", "FAHD points to FADB"),)),
        ))
        shared_path = ("layout:fixed-header", "fixed-header:data-block")
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
        if index.index_type == "fixed_array":
            if chunk.pointer_offset is None or index.data_block_address is None:
                raise ModernEvidenceError("fixed-array chunk lacks slot pointer")
            fa_block = reader.absolute(index.data_block_address)
            if not any(start <= chunk.pointer_offset and chunk.pointer_offset + width <= end
                       for start, end, kind in reader.metadata_ranges
                       if kind == "fixed-array data block"):
                raise ModernEvidenceError("fixed-array chunk pointer lies outside checked data block")
            if int.from_bytes(reader._read_absolute(chunk.pointer_offset, width), "little") != address:
                raise ModernEvidenceError("fixed-array slot bytes disagree with proposal address")
            parent = fa_block
            link_pointer = PhysicalExtent(source_id, chunk.pointer_offset, width)
            rule = "FADB array slot gives chunk coordinate in row-major grid"
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
            checksum = ChecksumEvidence(
                "fletcher32" if spec.filters else "none", None,
                "failed" if "Fletcher32 checksum mismatch" in failure else "unverified",
            )
        else:
            payload = decode_chunk(raw, spec, chunk.filter_mask)
            if payload != accepted_payload or len(payload) != spec.chunk_bytes:
                raise ModernEvidenceError("accepted payload disagrees with indexed source bytes")
            decoded_sha = hashlib.sha256(payload).hexdigest()
            decoded_length = len(payload)
            checks = _PASS
            checksum = ChecksumEvidence(
                "fletcher32" if spec.filters else "none", None,
                "passed" if spec.filters else "absent",
            )
        proposals.append(ChunkProposal(
            proposal_id, PhysicalExtent(source_id, absolute, chunk.size),
            hashlib.sha256(raw).hexdigest(), spec.path, coordinate,
            shared_path + (link_id,), decoded_sha, decoded_length, chunk.filter_mask,
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
                     for start, end, _ in reader.metadata_ranges)
    ledger = reconcile((source,), (anchor,), tuple(links), tuple(proposals), metadata)
    for decision in ledger.decisions[:len(records)]:
        if decision.status != "accepted":
            raise ModernEvidenceError(
                f"accepted modern chunk has unresolved evidence: {decision.reasons}"
            )
    if any(decision.status == "accepted" for decision in ledger.decisions[len(records):]):
        raise ModernEvidenceError("decode-failed modern chunk was accepted")
    return ledger
