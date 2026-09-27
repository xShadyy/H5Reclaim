"""Partial chunk export after a file is physically cut short at its tail.

The private snapshot is sparsely extended to the *declared* HDF5 EOF for
bounded rooted metadata parsing. Zero extension is never measurement evidence:
all rooted metadata and every accepted raw chunk must fit inside the original
physical length. A cut indexed chunk is explicitly unavailable in the output
validity map. This route cannot recover a lost index or missing schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import replace
from math import prod
from pathlib import Path

import h5py
import numpy as np

from .evidence_adapter import build_recovery_evidence
from .format import FormatError, H5File, SIGNATURE
from .metadata import UnsupportedCase
from .metadata_fallback import read_dataset_spec_fallback
from .modern_evidence_adapter import build_modern_evidence
from .modern_indexes import ModernH5File
from .ownership_inventory import inventory_other_allocations, reject_sibling_overlap
from .output_annotations import add_output_annotations
from .recovery import (
    Analysis, ChunkDecodeError, ChunkRecord, MAX_CHUNKS, MAX_NODES,
    MAX_SOURCE_BYTES, MissingDecoderError, RecoveryError, STATUS_CODES, VERSION,
    _decode_chunk, _validate_paths, _verify_source, source_snapshot,
)
from .schema_codec import ChunkDecodeError as SchemaChunkDecodeError
from .schema_codec import fletcher32_applied, validate_stored_size


MAX_MISSING_TAIL_BYTES = 256 * 1024 * 1024


def _declared_end(snapshot: Path, physical_size: int, max_missing_tail_bytes: int) -> tuple[int, int]:
    """Read just enough untrusted superblock bytes to set the padding limit."""
    with snapshot.open("rb") as stream:
        signature = 0
        while signature + 16 <= physical_size:
            stream.seek(signature)
            if stream.read(8) == SIGNATURE:
                break
            signature = 512 if signature == 0 else signature * 2
        else:
            raise FormatError("HDF5 signature not found in physically present bytes")
        stream.seek(signature)
        prefix = stream.read(24)
        if len(prefix) != 24:
            raise FormatError("truncated HDF5 superblock prefix")
        version = prefix[8]
        if version in (0, 1):
            width = prefix[13]
            field = (28 if version == 1 else 24) + 2 * width
        elif version in (2, 3):
            width = prefix[9]
            field = 12 + 2 * width
        else:
            raise UnsupportedCase(f"superblock version {version} is unsupported")
        if width not in (2, 4, 8) or signature + field + width > physical_size:
            raise FormatError("declared EOF field is not physically present")
        stream.seek(signature + field)
        declared = int.from_bytes(stream.read(width), "little")
    if declared <= physical_size:
        raise UnsupportedCase("declared HDF5 EOF does not exceed physical EOF; use ordinary rescue")
    if declared > MAX_SOURCE_BYTES or declared - physical_size > max_missing_tail_bytes:
        raise UnsupportedCase(
            f"declared-minus-physical EOF exceeds the {max_missing_tail_bytes}-byte tail limit"
        )
    return declared, version


def _hash_prefix(snapshot: Path, size: int) -> str:
    digest = hashlib.sha256()
    with snapshot.open("rb") as stream:
        remaining = size
        while remaining:
            block = stream.read(min(1 << 20, remaining))
            if not block:
                raise RecoveryError("private snapshot lost physically present source bytes")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def _check_physical_metadata(ranges: tuple | list, physical_size: int) -> None:
    if any(not (0 <= start < end <= physical_size) for start, end, _kind in ranges):
        raise FormatError("required rooted metadata or index extends beyond physical EOF")


def _append_chunk(spec, reader, address, length, mask, coordinate, route, evidence,
                  leaf_address, physical_size, records, failed, truncated, ranges):
    try:
        validate_stored_size(spec, length, mask)
    except SchemaChunkDecodeError as exc:
        raise FormatError(f"indexed chunk contradicts filter or size metadata: {exc}") from exc
    if (len(coordinate) != len(spec.shape) or any(
        start < 0 or start % width or start >= bound
        for start, width, bound in zip(coordinate, spec.chunks, spec.shape)
    )):
        raise FormatError("index coordinate contradicts dataset grid")
    absolute = reader.absolute(address)
    index = tuple(start // width for start, width in zip(coordinate, spec.chunks))
    ranges.append((absolute, absolute + length, coordinate))
    if absolute + length > physical_size:
        available = max(0, physical_size - absolute)
        truncated.append({
            "coordinate": list(coordinate), "chunk_index": list(index),
            "source_address": address, "source_absolute_offset": absolute,
            "stored_size_bytes": length, "physical_bytes_available": available,
            "status": "unavailable", "reason": "indexed payload crosses physical EOF",
        })
        return
    raw = reader.read_at(address, length)
    try:
        payload = _decode_chunk(raw, spec, mask)
    except (ChunkDecodeError, MissingDecoderError) as exc:
        failed.append({
            "coordinate": list(coordinate), "chunk_index": list(index),
            "source_address": address,
            "status": "decoder_unavailable" if isinstance(exc, MissingDecoderError) else "decode_failed",
            "reason": str(exc),
        })
        return
    records.append(ChunkRecord(
        index, tuple(coordinate), address, absolute, length, leaf_address,
        route, evidence, payload, mask,
    ))


def _finish_ranges(reader, ranges, physical_size, rooted_metadata_ranges):
    _check_physical_metadata(reader.metadata_ranges, physical_size)
    _check_physical_metadata(rooted_metadata_ranges, physical_size)
    for earlier, later in zip(sorted(ranges), sorted(ranges)[1:]):
        if earlier[1] > later[0]:
            raise FormatError("indexed payload ranges overlap")
    for start, end, coordinate in ranges:
        for meta_start, meta_end, kind in (*reader.metadata_ranges, *rooted_metadata_ranges):
            if start < meta_end and meta_start < end:
                raise FormatError(f"indexed chunk {coordinate} overlaps parsed {kind}")


def _modern(snapshot, spec, original_size, original_hash, rooted_metadata_ranges):
    records, failed, truncated, ranges = [], [], [], []
    with ModernH5File(snapshot) as reader:
        index = reader.read_index(
            spec.object_address, spec.shape, spec.chunks, np.dtype(spec.dtype).itemsize,
            maxshape=spec.maxshape or spec.shape, filters=spec.filters,
            max_chunks=MAX_CHUNKS,
        )
        _check_physical_metadata(reader.metadata_ranges, original_size)
        seen = set()
        for chunk in index.chunks:
            if chunk.coordinate in seen:
                raise FormatError("modern index duplicates a coordinate")
            seen.add(chunk.coordinate)
            _append_chunk(spec, reader, chunk.address, chunk.size, chunk.filter_mask,
                          chunk.coordinate,
                          "reconstructed_fa_header_link" if index.reconstructed_data_block_pointer
                          else f"intact_{index.index_type}", chunk.evidence, None,
                          original_size, records, failed, truncated, ranges)
        _finish_ranges(reader, ranges, original_size, rooted_metadata_ranges)
        inventory = inventory_other_allocations(snapshot, spec.object_address)
        reject_sibling_overlap(ranges, inventory)
        # Only physically complete entries are offered to the evidence adapter.
        # Its SourceRecord and all accepted extents describe the original file.
        reader.size = original_size
        ledger_index = replace(index, chunks=tuple(
            chunk for chunk in index.chunks
            if reader.superblock.base_address + chunk.address + chunk.size <= original_size
        ))
        ledger = build_modern_evidence(
            spec, reader, ledger_index, records, source_sha256=original_hash,
            decode_chunk=_decode_chunk, failed=failed,
            rooted_metadata_ranges=rooted_metadata_ranges,
        )
        details = {"type": index.index_type, "layout_version": index.layout_version,
                   "base_address": index.base_address,
                   "reconstructed_data_block_pointer": index.reconstructed_data_block_pointer}
    return records, failed, truncated, ledger, inventory, details


def _legacy(snapshot, spec, original_size, original_hash, rooted_metadata_ranges):
    records, failed, truncated, ranges = [], [], [], []
    with H5File(snapshot) as reader:
        rank = len(spec.shape)
        element_size = np.dtype(spec.dtype).itemsize
        layout = reader.read_dataset_layout(spec.object_address, rank=rank)
        if layout.chunk_shape != spec.chunks or layout.element_size != element_size:
            raise FormatError("selected raw layout disagrees with rooted dataset schema")
        root = reader.read_tree(layout.root_address, rank=rank, element_size=element_size)
        walk = reader.walk_tree(layout.root_address, rank=rank,
                                element_size=element_size, max_nodes=MAX_NODES)
        if walk.broken_links:
            raise UnsupportedCase("truncation route needs an intact rooted v1 index")
        leaves = {}
        seen = set()
        for leaf in walk.nodes:
            if leaf.level != 0:
                continue
            leaves[leaf.address] = (leaf, "intact_tree", {"root_address": root.address,
                                                          "root_level": root.level})
            for entry in leaf.entries:
                if entry.address is None or entry.key.offsets[-1] != 0:
                    raise FormatError("v1 leaf has no valid payload pointer")
                coordinate = tuple(entry.key.offsets[:-1])
                if coordinate in seen:
                    raise FormatError("v1 index duplicates a coordinate")
                seen.add(coordinate)
                _append_chunk(spec, reader, entry.address, entry.key.stored_size,
                              entry.key.filter_mask, coordinate, "intact_tree",
                              {"root_address": root.address, "root_level": root.level},
                              leaf.address, original_size, records, failed, truncated, ranges)
        _finish_ranges(reader, ranges, original_size, rooted_metadata_ranges)
        inventory = inventory_other_allocations(snapshot, spec.object_address)
        reject_sibling_overlap(ranges, inventory)
        reader.size = original_size
        ledger = build_recovery_evidence(
            spec, reader, walk, root, leaves, records, source_sha256=original_hash,
            decode_chunk=_decode_chunk, failed=failed,
            rooted_metadata_ranges=rooted_metadata_ranges,
        )
        details = {"type": "v1_raw_data_btree", "root_address": root.address,
                   "root_level": root.level, "reachable_leaves": len(leaves)}
    return records, failed, truncated, ledger, inventory, details


def analyze_truncated(source: Path, dataset_path: str, *,
                      max_missing_tail_bytes: int = MAX_MISSING_TAIL_BYTES) -> Analysis:
    """Analyze a stable source without treating private zero padding as data."""
    if not isinstance(max_missing_tail_bytes, int) or not 0 < max_missing_tail_bytes <= MAX_SOURCE_BYTES:
        raise ValueError("max_missing_tail_bytes must be a positive bounded integer")
    source = Path(source)
    with source_snapshot(source) as (snapshot, original_hash, identity, physical_size):
        declared, version = _declared_end(snapshot, physical_size, max_missing_tail_bytes)
        if shutil.disk_usage(snapshot.parent).free < declared - physical_size + 32 * 1024 * 1024:
            raise UnsupportedCase("insufficient free space for bounded private tail extension")
        with snapshot.open("r+b") as stream:
            stream.truncate(declared)
        # The rooted raw parser checks the entire selected hard-link path,
        # schema, and index. Its padded-image hash is internal; the published
        # evidence ledger uses the independently hashed physical source.
        rooted = read_dataset_spec_fallback(snapshot, dataset_path)
        _check_physical_metadata(rooted.metadata_ranges, physical_size)
        spec = rooted.spec
        if prod(spec.chunk_grid) > MAX_CHUNKS:
            raise UnsupportedCase(f"dataset exceeds the {MAX_CHUNKS}-chunk limit")
        if version in (0, 1):
            result = _legacy(snapshot, spec, physical_size, original_hash,
                             rooted.metadata_ranges)
        else:
            result = _modern(snapshot, spec, physical_size, original_hash,
                             rooted.metadata_ranges)
        records, failed, truncated, ledger, inventory, index_details = result
        if _hash_prefix(snapshot, physical_size) != original_hash:
            raise RecoveryError("private snapshot physical prefix changed during analysis")
        _verify_source(source, identity, original_hash)

    status = np.full(spec.chunk_grid, STATUS_CODES["allocation_unknown"], dtype="u1")
    for chunk in truncated:
        status[tuple(chunk["chunk_index"])] = STATUS_CODES["unavailable"]
    for chunk in failed:
        status[tuple(chunk["chunk_index"])] = STATUS_CODES[chunk["status"]]
    for chunk in records:
        status[chunk.index] = STATUS_CODES["recovered"]
    counts = {name: int(np.count_nonzero(status == code)) for name, code in STATUS_CODES.items()}
    complete = bool(np.all(status == STATUS_CODES["recovered"]))
    report = {
        "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
        "execution_state": "finished", "operation": "truncated_chunked_partial_export",
        "outcome": "complete" if complete else "partial", "complete": complete,
        "source": {"path": str(source), "size_bytes": physical_size,
                   "declared_eof_bytes": declared, "missing_tail_bytes": declared - physical_size,
                   "sha256_before": original_hash, "sha256_after": original_hash},
        "dataset": {"path": spec.path, "object_address": spec.object_address,
                    "shape": list(spec.shape), "chunks": list(spec.chunks),
                    "dtype": spec.dtype, "chunk_grid": list(spec.chunk_grid),
                    "filters": list(spec.filters),
                    "filter_pipeline": [{"id": item.id, "flags": item.flags,
                                         "values": list(item.values)} for item in spec.filter_pipeline],
                    "maxshape": list(spec.maxshape or spec.shape),
                    "attributes_copied": [name for name, _value in spec.attributes],
                    "attributes_omitted": list(spec.omitted_attributes)
                                          + list(rooted.omitted_auxiliary_metadata)},
        "metadata_resolution": {"route": rooted.route,
                                "selected_hard_link_chain": [
                                    {"group_address": step.group_address, "name": step.name,
                                     "object_address": step.object_address}
                                    for step in rooted.link_chain]},
        "index": index_details, "counts": counts,
        "reconstructed_chunks": sum(r.route == "reconstructed_fa_header_link" for r in records),
        "truncated_payloads": truncated, "failed_chunks": failed,
        "mappings": [
            {"coordinate": list(r.coordinate), "chunk_index": list(r.index),
             "source_address": r.file_address, "source_absolute_offset": r.absolute_offset,
             "size_bytes": r.length, "leaf_address": r.leaf_address,
             "route": r.route, "evidence": r.evidence, "filter_mask": r.filter_mask,
             "integrity": "fletcher32_verified" if fletcher32_applied(spec, r.filter_mask)
                          else "not_independently_verified"}
            for r in sorted(records, key=lambda item: item.index)
        ],
        "evidence_ledger": ledger.to_dict(),
        "ownership_inventory": inventory.report(),
        "integrity_note": ("The original physical EOF bounds every accepted metadata extent "
                           "and payload. Structural placement does not prove historical byte "
                           "integrity when the chunk lacks an independent checksum."),
        "metadata_note": ("Only the selected dataset's rooted numeric schema and accepted "
                          "chunks are exported. Listed omitted attributes and other scientific "
                          "context are not copied."),
        "limits": ("A bounded tail-only truncation with fully present rooted path, schema, "
                   "and index. Missing metadata or index bytes cause refusal. Private zero "
                   "extension is not data evidence."),
    }
    return Analysis(spec, records, status, report, identity)


def recover_truncated(source: Path, dataset_path: str, output: Path,
                      report_path: Path, *,
                      max_missing_tail_bytes: int = MAX_MISSING_TAIL_BYTES) -> dict:
    """Publish a partial derived file and matching JSON report atomically."""
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    analysis = analyze_truncated(source, dataset_path,
                                 max_missing_tail_bytes=max_missing_tail_bytes)
    annotation_values = {
        "h5reclaim_chunk_status": "/_h5reclaim/chunk_status",
        "h5reclaim_complete": analysis.report["complete"],
        "h5reclaim_execution_state": "finished",
        "h5reclaim_integrity": "per_chunk_in_report; some chunks may lack a payload checksum",
        "h5reclaim_warning": (
            "Check chunk_status before using values. Unavailable output chunks "
            "read as fill zero but are not known measurements. Listed omitted attributes, "
            "other scientific context, and sibling objects are not preserved."
        ),
    }
    annotation_collisions = sorted(set(annotation_values) & {
        name for name, _value in analysis.spec.attributes
    })
    analysis.report["selected_annotation_collisions"] = annotation_collisions
    report_text = json.dumps(analysis.report, indent=2, sort_keys=True) + "\n"
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=output.parent) as out_dir:
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=report_path.parent) as rep_dir:
            output_temp = Path(out_dir) / "output.h5"
            report_temp = Path(rep_dir) / "report.json"
            published = False
            try:
                with h5py.File(output_temp, "x") as handle:
                    spec = analysis.spec
                    data = handle.create_dataset(
                        spec.path, shape=spec.shape, maxshape=spec.maxshape or spec.shape,
                        chunks=spec.chunks, dtype=spec.dtype, fillvalue=0,
                    )
                    for record in analysis.records:
                        data.id.write_direct_chunk(record.coordinate, record.payload, filter_mask=0)
                    for name, value in spec.attributes:
                        data.attrs[name] = value
                    meta = handle.create_group("/_h5reclaim")
                    validity = meta.create_dataset("chunk_status", data=analysis.status, dtype="u1")
                    validity.attrs["codes_json"] = json.dumps(STATUS_CODES, sort_keys=True)
                    validity.attrs["axis_meaning"] = ", ".join(
                        f"chunk axis {dimension}" for dimension in range(len(spec.shape)))
                    meta.create_dataset("report_json", data=report_text,
                                        dtype=h5py.string_dtype(encoding="utf-8"))
                    meta.attrs["source_sha256"] = analysis.report["source"]["sha256_before"]
                    meta.attrs["report_schema_version"] = 1
                    if add_output_annotations(data, annotation_values) != annotation_collisions:
                        raise RecoveryError("selected attributes changed during publication")
                    handle.flush()
                report_temp.write_text(report_text, encoding="utf-8")
                _verify_source(source, analysis.source_identity,
                               analysis.report["source"]["sha256_before"])
                _validate_paths(source, output, report_path)
                os.link(output_temp, output)
                published = True
                os.link(report_temp, report_path)
                return analysis.report
            except Exception:
                if published:
                    output.unlink(missing_ok=True)
                raise
