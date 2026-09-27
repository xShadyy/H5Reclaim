"""Streaming structural recovery of one checked fixed-array pointer in a large file.

This route is intentionally narrow. A genuine modern FAHD-to-FADB pointer has
to be damaged; its *original* FAHD checksum must uniquely identify the FADB
over a complete bounded sparse/dense scan. The selected object header, FAHD
substituted checksum, FADB and its initialized pages are checked independently.
Values are decoded directly from those indexed physical ranges, never read
through the damaged native chunk index. Sparse grid slots remain unknown.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import h5py
import numpy as np

from .format import FormatError
from .large_streaming import LargeBudget, sparse_snapshot
from .metadata import UnsupportedCase, read_dataset_spec
from .modern_indexes import ModernH5File
from .ownership_inventory import inventory_other_allocations, reject_sibling_overlap
from .recovery import RecoveryError, VERSION, _validate_paths, _verify_source, sha256_file


MAX_LARGE_INDEX_CHUNKS = 65536
MAX_LARGE_ELEMENT_COUNT = 1_048_576
MAX_LARGE_CHUNK_BYTES = 1 << 20
MAX_LARGE_REPORT_BYTES = 1 << 20
DISK_RESERVE_BYTES = 64 << 20


def recover_large_fixed_array(
    source: str | Path, dataset_path: str, output: str | Path, report_path: str | Path,
    *, published_output: Path | None = None, budget: LargeBudget | None = None,
) -> dict[str, object]:
    """Recover one broken FAHD data-block link under explicit scale quotas.

    The input's HDF5 superblock may be above 4 GiB, and the selected grid
    may exceed 8192 chunks. Allocated records and output evidence are streamed;
    there is no list of decoded chunk payloads held in memory. This route
    accepts only one-dimensional unfiltered primitive numeric fixed arrays.
    """
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    budget = budget or LargeBudget()
    deadline = time.monotonic() + budget.max_seconds
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, copied):
        spec = read_dataset_spec(image, dataset_path)
        dtype = np.dtype(spec.dtype)
        grid = spec.chunk_grid[0]
        if (len(spec.shape) != 1 or spec.filters or spec.file_type_encoding is not None
                or dtype.kind not in "iuf" or dtype.itemsize not in (1, 2, 4, 8)
                or spec.shape[0] > MAX_LARGE_ELEMENT_COUNT
                or spec.chunk_bytes > MAX_LARGE_CHUNK_BYTES
                or not 1 <= grid <= min(MAX_LARGE_INDEX_CHUNKS, budget.max_chunks)):
            raise UnsupportedCase(
                "large structural route requires a bounded 1D unfiltered primitive "
                "numeric fixed-array dataset with at most 65536 grid chunks"
            )
        with ModernH5File(image, large_sparse_scan=True,
                          max_chunks=MAX_LARGE_INDEX_CHUNKS) as reader:
            index = reader.read_index(
                spec.object_address, spec.shape, spec.chunks, dtype.itemsize,
                maxshape=spec.maxshape or spec.shape, filters=spec.filters,
                max_chunks=MAX_LARGE_INDEX_CHUNKS,
            )
            if index.index_type != "fixed_array" or not index.reconstructed_data_block_pointer:
                raise UnsupportedCase("large structural route requires one damaged checked FAHD pointer")
            if index.reconstructed_links:
                raise FormatError("more than one checked index link needs repair")
            if not 1 <= len(index.chunks) <= min(MAX_LARGE_INDEX_CHUNKS, budget.max_chunks):
                raise UnsupportedCase("allocated chunk count exceeds large structural quota")

            ranges = []
            seen: set[int] = set()
            for record in index.chunks:
                origin = record.coordinate[0]
                if (origin < 0 or origin >= spec.shape[0] or origin % spec.chunks[0]
                        or origin in seen or record.filter_mask or record.size != spec.chunk_bytes):
                    raise FormatError("fixed-array record contradicts selected dataset grid")
                seen.add(origin)
                start = reader.absolute(record.address)
                if start > size or record.size > size - start:
                    raise FormatError("indexed raw chunk extends past the physical source")
                ranges.append((start, start + record.size, (origin,)))
            ordered = sorted(ranges)
            if any(first[1] > second[0] for first, second in zip(ordered, ordered[1:])):
                raise FormatError("selected fixed-array payload ranges overlap")
            for start, end, coordinate in ordered:
                for meta_start, meta_end, kind in reader.metadata_ranges:
                    if start < meta_end and meta_start < end:
                        raise FormatError(f"chunk {coordinate} overlaps checked {kind}")

            inventory = inventory_other_allocations(
                image, spec.object_address, max_allocations=budget.max_chunks,
                max_seconds=min(120, max(1, budget.max_seconds)),
                skip_selected_object_info=True,
            )
            reject_sibling_overlap(ordered, inventory)
            evidence_dtype = np.dtype([
                ("origin", "<u8"), ("source_offset", "<u8"),
                ("stored_bytes", "<u8"), ("raw_sha256", "S64"),
                ("parent_pointer_offset", "<u8"), ("parent_address", "<u8"),
                ("child_address", "<u8"),
            ])
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-large-", dir=output.parent) as out_dir:
                with tempfile.TemporaryDirectory(prefix=".h5reclaim-large-", dir=report_path.parent) as rep_dir:
                    staged_output = Path(out_dir) / "output.h5"
                    staged_report = Path(rep_dir) / "report.json"
                    source_values = hashlib.sha256()
                    with h5py.File(staged_output, "x") as result:
                        group, _, leaf = spec.path.rpartition("/")
                        parent = result.require_group(group or "/")
                        target = parent.create_dataset(
                            leaf, shape=spec.shape, dtype=dtype, chunks=spec.chunks,
                        )
                        for name, value in spec.attributes:
                            target.attrs[name] = value
                        meta = result.require_group("/_h5reclaim")
                        validity = meta.create_dataset(
                            "validity", shape=(grid,), dtype="u1", fillvalue=0,
                            chunks=(min(grid, 4096),),
                        )
                        evidence = meta.create_dataset(
                            "physical_evidence", shape=(len(index.chunks),),
                            dtype=evidence_dtype,
                            chunks=(min(len(index.chunks), 1024),),
                        )
                        accepted = 0
                        for row, record in enumerate(sorted(index.chunks,
                                                              key=lambda item: item.coordinate)):
                            if time.monotonic() > deadline:
                                raise UnsupportedCase("large structural recovery exceeded its time quota")
                            origin = record.coordinate[0]
                            raw = reader.read_at(record.address, record.size)
                            values = np.frombuffer(raw, dtype=dtype)
                            count = min(spec.chunks[0], spec.shape[0] - origin)
                            target[origin:origin + count] = values[:count]
                            validity[origin // spec.chunks[0]] = 1
                            evidence[row] = (
                                origin, reader.absolute(record.address), record.size,
                                hashlib.sha256(raw).hexdigest(),
                                index.data_block_pointer_offset, index.base_address,
                                index.data_block_address,
                            )
                            source_values.update(values[:count].tobytes(order="C"))
                            accepted += count
                            if (staged_output.stat().st_size > budget.max_output_bytes
                                    or shutil.disk_usage(staged_output.parent).free <
                                    budget.disk_reserve_bytes):
                                raise UnsupportedCase("large structural output exceeds disk quota")
                        result.flush()
                    output_values = hashlib.sha256()
                    with h5py.File(staged_output, "r") as result:
                        target = result[spec.path]
                        for record in sorted(index.chunks, key=lambda item: item.coordinate):
                            origin = record.coordinate[0]
                            count = min(spec.chunks[0], spec.shape[0] - origin)
                            output_values.update(np.asarray(
                                target[origin:origin + count]).tobytes(order="C"))
                    if output_values.digest() != source_values.digest():
                        raise RecoveryError("structural output differs bitwise from checked-index raw chunks")
                    report: dict[str, object] = {
                        "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                        "operation": "large_checked_fixed_array_pointer_recovery",
                        "outcome": "complete" if accepted == spec.shape[0] else "partial",
                        "source": {
                            "path": str(source), "size_bytes": size,
                            "sha256_before": digest, "sha256_after": digest,
                            "snapshot_physical_data_bytes_copied": copied,
                        },
                        "dataset": {
                            "path": spec.path, "shape": list(spec.shape),
                            "dtype": dtype.str, "chunks": list(spec.chunks),
                            "maxshape": list(spec.maxshape or spec.shape),
                            "attributes_copied": [name for name, _ in spec.attributes],
                            "attributes_omitted": list(spec.omitted_attributes),
                        },
                        "index": {
                            "type": index.index_type,
                            "fahd_address": index.base_address,
                            "fadb_address": index.data_block_address,
                            "damaged_pointer_absolute_offset": index.data_block_pointer_offset,
                            "justification": (
                                "unique candidate in a complete bounded scan restores the original "
                                "FAHD checksum; FADB back-pointer and checksum, initialized page "
                                "checksums, rooted object layout and chunk slot ranges validate"
                            ),
                        },
                        "allocated_chunks_checked": len(index.chunks),
                        "chunk_grid_count": grid,
                        "accepted_elements": accepted,
                        "unknown_elements": spec.shape[0] - accepted,
                        "validity_map": "/_h5reclaim/validity",
                        "physical_evidence": "/_h5reclaim/physical_evidence",
                        "ownership_inventory": inventory.report(),
                        "output_values_sha256": output_values.hexdigest(),
                        "output_path": str(published_output or output),
                        "limits": (
                            "One checked FAHD-to-FADB pointer in a fixed-size unfiltered 1D "
                            "numeric dataset. A recovered coordinate is justified by checked "
                            "metadata and decoded bytes, but unchecksummed measurements have no "
                            "historical authenticity proof. Unknown slots must not be treated as "
                            "fill measurements. Other objects and dimension scales are omitted."
                        ),
                    }
                    report_text = json.dumps(report, sort_keys=True, indent=2) + "\n"
                    if len(report_text.encode("utf-8")) > MAX_LARGE_REPORT_BYTES:
                        raise UnsupportedCase("large structural report exceeds size quota")
                    with h5py.File(staged_output, "r+") as result:
                        meta = result["/_h5reclaim"]
                        meta.create_dataset("report_json", data=report_text,
                                            dtype=h5py.string_dtype(encoding="utf-8"))
                        meta.attrs["source_sha256"] = digest
                    staged_report.write_text(report_text, encoding="utf-8")
                    if (staged_output.stat().st_size > budget.max_output_bytes
                            or sha256_file(image) != digest):
                        raise RecoveryError("large structural output size or private snapshot changed")
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
