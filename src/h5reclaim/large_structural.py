"""Stream fixed-record chunks from independently checked modern HDF5 indexes.

A broken link is accepted only when a unique child restores the parent's
original checksum and its independently checked index agrees with the rooted
schema. Chunk payloads are decoded one at a time; damaged or unavailable
payloads remain explicit nonaccepted entries in a sparse validity map.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from math import prod
from pathlib import Path

import h5py
import numpy as np

from .format import FormatError
from .large_streaming import LargeBudget, sparse_snapshot
from .metadata import UnsupportedCase, read_dataset_spec
from .modern_indexes import ModernH5File
from .native_io import read_fixed_block, write_fixed_block
from .output_annotations import add_output_annotations, ensure_group, metadata_group_for_path
from .ownership_inventory import inventory_other_allocations, reject_sibling_overlap
from .recovery import RecoveryError, VERSION, _validate_paths, _verify_source, sha256_file
from .schema_codec import (ChunkDecodeError, MissingFilterDecoder, decode_chunk,
                           validate_stored_size, fletcher32_applied)


# Compatibility names. Actual dataset and decoder limits come from LargeBudget.
MAX_LARGE_ELEMENT_COUNT = 1_048_576
MAX_LARGE_CHUNK_BYTES = 64 << 20
MAX_LARGE_REPORT_BYTES = 1 << 20
DISK_RESERVE_BYTES = 64 << 20
STATUS_CODES = {"unknown": 0, "recovered": 1, "decode_failed": 2, "decoder_unavailable": 3}


def _repair_dependencies(index, record, repairs):
    """List repaired pointers traversed by the selected chunk's checked path."""
    if index.index_type == "fixed_array":
        offsets = {index.data_block_pointer_offset}
    elif index.index_type == "extensible_array":
        offsets = {step.get("pointer_offset") for step in record.evidence.get("index_chain", ())}
    else:
        offsets = {step.get("pointer_offset") for step in record.evidence.get("link_path", ())}
    return [repair for repair in repairs if repair["pointer_offset"] in offsets]


def recover_large_fixed_array(source, dataset_path, output, report_path, *,
                              published_output=None, budget=None):
    """Compatibility entry point requiring one checked FAHD-to-FADB repair."""
    return recover_large_structural(source, dataset_path, output, report_path,
                                    published_output=published_output, budget=budget,
                                    require_fixed_array=True)


def recover_large_structural(source: str | Path, dataset_path: str, output: str | Path,
                             report_path: str | Path, *, published_output: Path | None = None,
                             budget: LargeBudget | None = None,
                             require_fixed_array: bool = False) -> dict[str, object]:
    """Recover a broken fixed-array, extensible-array or v2 B-tree chunk index.

    Supports checked chunk indexes, filtered or unfiltered chunks, partial
    edges, and grids constrained by caller quotas and worker memory. Each
    damaged metadata parent may contribute up to two uniquely checked index links.
    No native read of the damaged dataset's measurements is used.
    """
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    budget = budget or LargeBudget()
    if budget.max_metadata_bytes < 8:
        raise UnsupportedCase("large structural metadata budget cannot hold a datatype header")
    deadline = time.monotonic() + budget.max_seconds
    decoded_limit = min(MAX_LARGE_CHUNK_BYTES, budget.max_chunk_bytes)
    stored_limit = min(128 << 20, max(decoded_limit * 2, 2 << 20))
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, copied):
        spec = read_dataset_spec(image, dataset_path, max_elements=budget.max_logical_bytes,
                                 max_chunk_bytes=decoded_limit, max_rank=32,
                                 max_type_depth=budget.max_type_depth,
                                 max_type_members=min(budget.max_type_members, 65536),
                                 max_array_rank=32,
                                 max_type_encoding_bytes=min(budget.max_metadata_bytes, 1 << 20))
        element_size = np.dtype(spec.dtype).itemsize
        total, grid = prod(spec.shape), spec.chunk_grid
        grid_count = prod(grid)
        if total * element_size > budget.max_logical_bytes:
            raise UnsupportedCase("structural dataset exceeds the logical byte budget")
        per_slot_reserve = 512 if spec.maxshape == spec.shape else 8192
        max_index_chunks = min(budget.max_chunks, budget.max_grid,
                               max(1, budget.worker_memory_bytes // per_slot_reserve))
        if grid_count > max_index_chunks:
            raise UnsupportedCase("large structural chunk grid exceeds the configured quota")
        with ModernH5File(image, large_sparse_scan=True, max_chunks=max_index_chunks,
                          max_chunk_bytes=stored_limit,
                          repair_scan_bytes=min(budget.max_scan_bytes, budget.max_source_bytes),
                          repair_scan_seconds=min(budget.max_seconds, 86400)) as reader:
            index = reader.read_index(spec.object_address, spec.shape, spec.chunks, element_size,
                                      maxshape=spec.maxshape, filters=spec.filters,
                                      max_chunks=max_index_chunks)
            repairs = list(index.reconstructed_links)
            if index.reconstructed_data_block_pointer:
                repairs.append({"kind": "fahd_to_fadb", "source_address": index.base_address,
                                "target_address": index.data_block_address,
                                "pointer_offset": index.data_block_pointer_offset})
            if require_fixed_array and (index.index_type != "fixed_array" or not index.reconstructed_data_block_pointer):
                raise UnsupportedCase("large structural route requires one damaged checked FAHD pointer")
            if not repairs:
                raise UnsupportedCase("large structural route requires a uniquely checked damaged index link")
            if len({repair["pointer_offset"] for repair in repairs}) != len(repairs):
                raise FormatError("reconstructed index repairs reuse a pointer slot")
            if not index.chunks or len(index.chunks) > budget.max_chunks:
                raise UnsupportedCase("allocated chunks are absent or exceed the configured budget")
            coordinates, ranges = set(), []
            for record in index.chunks:
                coordinate = record.coordinate
                if (len(coordinate) != len(spec.shape) or coordinate in coordinates
                        or any(start < 0 or start >= extent or start % width
                               for start, extent, width in zip(coordinate, spec.shape, spec.chunks))):
                    raise FormatError("checked index duplicates or misplaces a chunk coordinate")
                coordinates.add(coordinate)
                try:
                    validate_stored_size(spec, record.size, record.filter_mask,
                                         max_decoded_bytes=decoded_limit, max_stored_bytes=stored_limit)
                except ChunkDecodeError as exc:
                    raise FormatError(f"indexed chunk size or filter mask contradicts schema: {exc}") from exc
                start = reader.absolute(record.address)
                if record.size > size - start or start + record.size > reader.superblock.eof_address:
                    raise FormatError("indexed raw chunk extends outside the source")
                ranges.append((start, start + record.size, coordinate))
            ordered = sorted(ranges)
            if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
                raise FormatError("selected raw chunk ranges overlap")
            for start, end, coordinate in ordered:
                if any(start < stop and begin < end for begin, stop, _ in reader.metadata_ranges):
                    raise FormatError(f"chunk {coordinate} overlaps independently checked metadata")
            inventory = inventory_other_allocations(image, spec.object_address,
                max_allocations=budget.max_chunks, max_seconds=min(120, budget.max_seconds),
                skip_selected_object_info=True)
            reject_sibling_overlap(ordered, inventory)
            with h5py.File(image, "r") as original:
                creation = original[spec.path].id.get_create_plist()
                datatype = original[spec.path].id.get_type().copy()
            metadata_group = metadata_group_for_path(spec.path)
            rank = len(spec.shape)
            evidence_dtype = np.dtype([
                ("origin", "<u8") if rank == 1 else ("origin", "<u8", (rank,)),
                ("source_offset", "<u8"), ("stored_bytes", "<u8"), ("filter_mask", "<u4"),
                ("raw_sha256", "S64"), ("decoded_sha256", "S64"), ("status", "u1"),
                ("repair_link_count", "<u4"), ("parent_pointer_offset", "<u8"),
                ("parent_address", "<u8"), ("child_address", "<u8")])
            records = sorted(index.chunks, key=lambda item: item.coordinate)
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-structural-", dir=output.parent) as out_dir, \
                 tempfile.TemporaryDirectory(prefix=".h5reclaim-structural-", dir=report_path.parent) as rep_dir:
                staged_output, staged_report = Path(out_dir) / "output.h5", Path(rep_dir) / "report.json"
                accepted, failures, checksummed = 0, [], 0
                counts = {name: 0 for name in STATUS_CODES}
                counts["unknown"] = grid_count - len(records)
                value_hash = hashlib.sha256()
                with h5py.File(staged_output, "x") as result:
                    parent_path, leaf = spec.path.rsplit("/", 1)
                    parent = ensure_group(result, parent_path or "/")
                    output_creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                    output_creation.set_chunk(spec.chunks)
                    from .native_io import copy_fixed_fill
                    copy_fixed_fill(creation, output_creation, datatype)
                    maximum = tuple(h5py.h5s.UNLIMITED if n is None else n for n in spec.maxshape)
                    target = h5py.Dataset(h5py.h5d.create(parent.id, leaf.encode("utf-8"), datatype,
                        h5py.h5s.create_simple(spec.shape, maximum), dcpl=output_creation))
                    for name, value in spec.attributes:
                        target.attrs[name] = value
                    meta = ensure_group(result, metadata_group)
                    validity = meta.create_dataset("validity", shape=grid, dtype="u1", fillvalue=0,
                        chunks=tuple(min(n, max(1, int(4096 ** (1 / rank)))) for n in grid))
                    validity.attrs["codes_json"] = json.dumps(STATUS_CODES, sort_keys=True)
                    evidence = meta.create_dataset("physical_evidence", shape=(len(records),),
                        dtype=evidence_dtype, chunks=(min(len(records), 1024),))
                    for row, record in enumerate(records):
                        if time.monotonic() > deadline:
                            raise UnsupportedCase("large structural recovery exceeded the time quota")
                        coordinate = record.coordinate
                        cell = tuple(start // width for start, width in zip(coordinate, spec.chunks))
                        raw = reader.read_at(record.address, record.size)
                        code, decoded_digest = 1, ""
                        try:
                            payload = decode_chunk(raw, spec, record.filter_mask,
                                max_decoded_bytes=decoded_limit, max_stored_bytes=stored_limit)
                        except MissingFilterDecoder as exc:
                            code, reason = 3, str(exc)
                        except ChunkDecodeError as exc:
                            code, reason = 2, str(exc)
                        if code == 1:
                            extent = tuple(min(width, size - start) for start, width, size
                                           in zip(coordinate, spec.chunks, spec.shape))
                            selection = tuple(slice(start, start + count) for start, count in zip(coordinate, extent))
                            block = np.frombuffer(payload, dtype=f"V{element_size}").reshape(spec.chunks)
                            cropped = np.ascontiguousarray(block[tuple(slice(0, n) for n in extent)])
                            write_fixed_block(target, selection, cropped)
                            checked = read_fixed_block(target, selection).tobytes()
                            if checked != cropped.tobytes():
                                raise RecoveryError("structural chunk output differs bitwise on readback")
                            decoded_digest = hashlib.sha256(checked).hexdigest()
                            value_hash.update(checked)
                            accepted += prod(extent)
                            checksummed += int(fletcher32_applied(spec, record.filter_mask,
                                                                 max_decoded_bytes=decoded_limit))
                        elif len(failures) < 64:
                            failures.append({"coordinate": list(coordinate), "status": code, "reason": reason[:500]})
                        validity[cell] = code
                        counts[next(name for name, value in STATUS_CODES.items() if value == code)] += 1
                        dependencies = _repair_dependencies(index, record, repairs)
                        sole_repair = dependencies[0] if len(dependencies) == 1 else {}
                        evidence[row] = (coordinate[0] if rank == 1 else coordinate,
                            reader.absolute(record.address), record.size, record.filter_mask,
                            hashlib.sha256(raw).hexdigest(), decoded_digest, code,
                            len(dependencies), sole_repair.get("pointer_offset", 0),
                            sole_repair.get("source_address", 0), sole_repair.get("target_address", 0))
                        if (staged_output.stat().st_size > budget.max_output_bytes
                                or shutil.disk_usage(staged_output.parent).free < budget.disk_reserve_bytes):
                            raise UnsupportedCase("large structural output exceeds the disk quota")
                    collisions = add_output_annotations(target, {
                        "h5reclaim_chunk_status": metadata_group + "/validity",
                        "h5reclaim_warning": "Use the reported validity map; nonaccepted positions are not measurements."})
                    legacy_operation = (index.index_type == "fixed_array" and rank == 1 and not spec.filters)
                    report = {
                        "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                        "operation": "large_checked_fixed_array_pointer_recovery" if legacy_operation else "large_checked_chunk_index_recovery",
                        "outcome": "complete" if accepted == total else "partial",
                        "source": {"path": str(source), "size_bytes": size, "sha256_before": digest,
                                   "sha256_after": digest, "snapshot_physical_data_bytes_copied": copied},
                        "dataset": {"path": spec.path, "shape": list(spec.shape), "dtype": np.dtype(spec.dtype).str,
                            "chunks": list(spec.chunks), "maxshape": list(spec.maxshape),
                            "filters_in_order": list(spec.filters), "output_filters": [],
                            "attributes_copied": [name for name, _ in spec.attributes],
                            "attributes_omitted": list(spec.omitted_attributes)},
                        "index": {"type": index.index_type, "repairs": repairs,
                            "fahd_address": index.base_address if index.index_type == "fixed_array" else None,
                            "fadb_address": index.data_block_address,
                            "damaged_pointer_absolute_offset": repairs[0].get("pointer_offset") if len(repairs) == 1 else None,
                            "justification": "unique original-checksum-restoring link sets in damaged metadata parents, independently checked children/index and rooted schema"},
                        "allocated_chunks_checked": len(records), "chunk_grid_count": grid_count,
                        "accepted_elements": accepted, "unknown_elements": total - accepted,
                        "counts": counts, "validity_codes": STATUS_CODES,
                        "validity": {"dataset": metadata_group + "/validity", "granularity": "chunk", "codes": STATUS_CODES},
                        "metadata_group": metadata_group, "validity_map": metadata_group + "/validity",
                        "physical_evidence": metadata_group + "/physical_evidence",
                        "physical_evidence_schema": "chunk_origin_decoded_sha256_repair_dependency_count_v2",
                        "repair_dependency_scope": "Each row counts repaired pointer links traversed on its rooted index path. Parent address fields identify the repair only when the count is one; all repairs are listed in index.repairs.",
                        "failed_chunks_sample": failures, "checksummed_accepted_chunks": checksummed,
                        "selected_annotation_collisions": collisions, "ownership_inventory": inventory.report(),
                        "output_values_sha256": value_hash.hexdigest(),
                        "output_values_sha256_scope": "accepted chunks in lexicographic coordinate order, cropped to dataset edges",
                        "output_path": str(published_output or output),
                        "limits": "At most two uniquely checked pointers per damaged metadata parent; bounded chunk grid and scan. Missing or undecodable chunks remain nonaccepted. Current bytes do not establish historical authenticity."}
                    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
                    if len(serialized.encode()) > MAX_LARGE_REPORT_BYTES:
                        raise UnsupportedCase("large structural report exceeds the size quota")
                    meta.create_dataset("report_json", data=serialized, dtype=h5py.string_dtype("utf-8"))
                    meta.attrs["source_sha256"] = digest
                    result.flush()
                staged_report.write_text(serialized, encoding="utf-8")
                if staged_output.stat().st_size > budget.max_output_bytes or sha256_file(image) != digest:
                    raise RecoveryError("large structural output quota or source snapshot changed")
                _verify_source(source, identity, digest)
                _validate_paths(source, output, report_path)
                published = False
                try:
                    os.link(staged_output, output)
                    published = True
                    os.link(staged_report, report_path)
                except Exception:
                    if published:
                        output.unlink(missing_ok=True)
                    raise
                return report
