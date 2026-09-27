"""Conservative structural export from two modern HDF5 chunk indexes.

This route needs a native lookup of the selected local dataset from the same
private snapshot. The raw parser independently checks the selected object's
checksum and layout. Its single-chunk or implicit rule must attribute every
accepted coordinate to a bounded physical byte range. No orphan scan occurs.
"""

from __future__ import annotations

import json
from math import prod
from pathlib import Path
from typing import Any

import numpy as np

from .format import FormatError
from .metadata import DatasetSpec, UnsupportedCase
from .modern_indexes import MAX_CHUNKS, ModernH5File


def analyze_modern_snapshot(
    snapshot: Path, spec: DatasetSpec, source: Path, before_hash: str,
    identity: tuple[int, int, int, int, int], size: int,
):
    """Return a regular recovery.Analysis for a validated v2/v3 modern file.

    Imported by recovery only after the caller has created a stable source
    snapshot and selected the dataset. Delaying the import avoids a module
    initialization cycle with the shared output/report implementation.
    """
    from .recovery import (
        MAX_CHUNKS as RECOVERY_MAX_CHUNKS, Analysis, ChunkDecodeError,
        ChunkRecord, STATUS_CODES, VERSION, _decode_chunk,
    )

    grid = spec.chunk_grid
    if prod(grid) > min(MAX_CHUNKS, RECOVERY_MAX_CHUNKS):
        raise UnsupportedCase("modern index exceeds supported chunk count")
    rank = len(spec.shape)
    element_size = np.dtype(spec.dtype).itemsize
    accepted: list[ChunkRecord] = []
    failed: list[dict[str, Any]] = []
    ranges: list[tuple[int, int, tuple[int, ...]]] = []
    with ModernH5File(snapshot) as reader:
        index = reader.read_index(
            spec.object_address, spec.shape, spec.chunks, element_size,
            maxshape=spec.shape, filters=spec.filters,
            max_chunks=min(MAX_CHUNKS, RECOVERY_MAX_CHUNKS),
        )
        if len(index.chunks) != prod(grid):
            raise FormatError("modern index has incomplete coordinate coverage")
        coordinates: set[tuple[int, ...]] = set()
        for chunk in index.chunks:
            coordinate = chunk.coordinate
            if coordinate in coordinates or any(
                start % width or start >= length for start, width, length
                in zip(coordinate, spec.chunks, spec.shape)
            ):
                raise FormatError("modern index duplicates or misplaces a coordinate")
            coordinates.add(coordinate)
            absolute = reader.absolute(chunk.address)
            ranges.append((absolute, absolute + chunk.size, coordinate))
            raw = reader.read_at(chunk.address, chunk.size)
            chunk_index = tuple(start // width for start, width in zip(coordinate, spec.chunks))
            try:
                payload = _decode_chunk(raw, spec, chunk.filter_mask)
            except ChunkDecodeError as exc:
                failed.append({
                    "coordinate": list(coordinate),
                    "chunk_index": list(chunk_index),
                    "source_address": chunk.address,
                    "reason": str(exc),
                })
                continue
            accepted.append(ChunkRecord(
                index=chunk_index, coordinate=coordinate,
                file_address=chunk.address, absolute_offset=absolute,
                length=chunk.size, leaf_address=None,
                route=f"intact_{index.index_type}", evidence=chunk.evidence,
                payload=payload,
            ))
        ordered = sorted(ranges)
        for previous, current in zip(ordered, ordered[1:]):
            if previous[1] > current[0]:
                raise FormatError("modern index payload ranges overlap")
        for start, end, coordinate in ordered:
            for meta_start, meta_end, kind in reader.metadata_ranges:
                if start < meta_end and meta_start < end:
                    raise FormatError(f"chunk {coordinate} overlaps parsed {kind}")

        from .modern_evidence_adapter import build_modern_evidence
        ledger = build_modern_evidence(
            spec, reader, index, accepted,
            source_sha256=before_hash, decode_chunk=_decode_chunk, failed=failed,
        )

    status = np.full(grid, STATUS_CODES["allocation_unknown"], dtype="u1")
    for chunk in failed:
        status[tuple(chunk["chunk_index"])] = STATUS_CODES["decode_failed"]
    for record in accepted:
        status[record.index] = STATUS_CODES["recovered"]
    counts = {name: int(np.count_nonzero(status == code)) for name, code in STATUS_CODES.items()}
    complete = bool(np.all(status == STATUS_CODES["recovered"]))
    report = {
        "schema_version": 1,
        "tool": "h5reclaim",
        "tool_version": VERSION,
        "execution_state": "finished",
        "operation": "intact_index_export",
        "structural_repair": False,
        "outcome": "complete" if complete else "partial",
        "complete": complete,
        "source": {
            "path": str(source), "size_bytes": size,
            "sha256_before": before_hash, "sha256_after": before_hash,
        },
        "dataset": {
            "path": spec.path, "object_address": spec.object_address,
            "shape": list(spec.shape), "chunks": list(spec.chunks), "dtype": spec.dtype,
            "chunk_grid": list(grid), "filters": list(spec.filters),
            "attributes_copied": [name for name, _ in spec.attributes],
            "attributes_omitted": list(spec.omitted_attributes),
        },
        "index": {
            "type": index.index_type, "layout_version": index.layout_version,
            "selected_object_address": spec.object_address,
            "base_address": index.base_address,
            "fixed_array_data_block_address": index.data_block_address,
            "root_address": None, "root_level": None,
            "reachable_leaves": 0, "broken_links": 0,
        },
        "counts": counts,
        "reconstructed_chunks": 0,
        "unresolved_links": [],
        "failed_chunks": failed,
        "evidence_ledger": json.loads(json.dumps(ledger.to_dict())),
        "mappings": [
            {
                "coordinate": list(record.coordinate), "chunk_index": list(record.index),
                "source_address": record.file_address,
                "source_absolute_offset": record.absolute_offset,
                "size_bytes": record.length, "leaf_address": None,
                "route": record.route, "evidence": record.evidence,
                "integrity": "fletcher32_verified" if spec.filters else "not_checked_no_checksum",
            }
            for record in sorted(accepted, key=lambda item: item.index)
        ],
        "assumptions": [
            "local selected dataset resolved from same snapshot",
            "version-2/3 superblock and checksum-verified version-2 object header",
            "version-4/5 chunked layout with single-chunk, implicit, or nonpaged unfiltered fixed-array index",
            "index is intact; no broken modern index pointers were inferred",
        ],
        "coverage_note": (
            "This route exports allocated chunks whose modern index is intact. "
            "It does not show that native HDF5 could not already read the dataset "
            "and must not be scored as repair of damaged index links."
        ),
        "integrity_note": (
            "Chunk address and coordinate follow the selected object's validated layout. "
            "The stored Fletcher32 checksum was verified per accepted chunk; it detects "
            "some errors but does not prove historical authenticity."
            if spec.filters else
            "Chunk address and coordinate follow the selected object's validated layout. "
            "Unfiltered payload bytes have no independent checksum here; historical "
            "measurement integrity is not established."
        ),
        "metadata_note": (
            "Only the selected dataset's values, shape, chunking, datatype, and listed "
            "primitive scalar attributes are exported. Other scientific context, links, "
            "scales, and sibling objects are not preserved."
        ),
    }
    return Analysis(spec, accepted, status, report, identity)
