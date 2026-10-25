"""Relocate heap-backed values after a checked modern index-link repair.

An HDF5 variable-length descriptor or reference is meaningful only inside its
source file.  Copying decoded chunk bytes to a different file would leave the
new file pointing at old heap allocations and object addresses.  This route
repairs independently justified index pointers in a disposable source view,
then uses native HDF5 to read logical values and remap them into a new file.
The original file remains unchanged and supplies every reported allocation.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from math import prod
from pathlib import Path

import h5py

from .format import FormatError
from .large_streaming import LargeBudget, _copy_sparse, sparse_snapshot
from .logical_types import contains_pointers, validate_type
from .metadata import UnsupportedCase
from .modern_indexes import ModernH5File
from .native_stream import export_native_stream
from .output_annotations import report_metadata_group
from .readable_export import _selected_dataset
from .recovery import RecoveryError, _validate_paths, _verify_source, sha256_file


def _file_element_width(typ: h5py.h5t.TypeID, offset_size: int) -> int:
    """Derive the chunk-layout width independently of the layout message.

    H5Tget_size exposes the memory pointer width for a top-level VLEN value,
    while its file representation contains a length and a global heap ID.
    Compound and array elements gain that difference for each VLEN member.
    """
    kind = typ.get_class()
    if kind == h5py.h5t.VLEN or (kind == h5py.h5t.STRING and typ.is_variable_str()):
        return offset_size + 8
    if kind == h5py.h5t.ARRAY:
        return prod(typ.get_array_dims()) * _file_element_width(typ.get_super(), offset_size)
    if kind == h5py.h5t.COMPOUND:
        return typ.get_size() + sum(
            _file_element_width(typ.get_member_type(i), offset_size)
            - typ.get_member_type(i).get_size()
            for i in range(typ.get_nmembers())
        )
    return typ.get_size()


def _index_records(index) -> tuple[tuple[tuple[int, ...], int, int, int], ...]:
    return tuple(sorted((record.coordinate, record.address, record.size, record.filter_mask)
                        for record in index.chunks))


def recover_relocated_index(source: str | Path, dataset_path: str, output: str | Path,
                            report_path: str | Path, *, budget: LargeBudget | None = None,
                            published_output: str | Path | None = None) -> dict:
    """Recover a broken checked index holding VLEN or reference records.

    Native HDF5 decodes and relocates only allocated, independently attributed
    records.  References to other objects remain deferred for whole-file rescue
    unless those targets are independently recovered there; a one-dataset
    export never invents a missing target.
    """
    source, output, report_path = Path(source), Path(output), Path(report_path)
    budget = budget or LargeBudget()
    _validate_paths(source, output, report_path)
    deadline = time.monotonic() + budget.max_seconds
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, _copied):
        with h5py.File(image, "r") as opened:
            selected = _selected_dataset(opened, dataset_path)
            typ = selected.id.get_type()
            validate_type(typ, budget=budget)
            if not contains_pointers(typ):
                raise UnsupportedCase("checked native relocation requires a heap-backed or reference datatype")
            shape, chunks, maxshape = selected.shape, selected.chunks, selected.maxshape
            if (shape is None or chunks is None or not 1 <= len(shape) <= 32
                    or any(n <= 0 for n in shape) or prod(shape) > budget.max_grid):
                raise UnsupportedCase("checked native relocation requires a bounded nonempty chunked dataset")
            creation = selected.id.get_create_plist()
            if creation.get_external_count() or selected.is_virtual:
                raise UnsupportedCase("external and virtual storage need a separate dependency route")
            filters = tuple(int(creation.get_filter(i)[0]) for i in range(creation.get_nfilters()))
            parent_path, leaf = dataset_path.rsplit("/", 1)
            # H5Oget_info may inspect the damaged index and fail even though
            # the rooted hard link and selected dataset header remain readable.
            parent = opened[parent_path or "/"]
            object_address = int(parent.id.links.get_info(leaf.encode("utf-8")).u)
        grid_count = prod((axis + width - 1) // width for axis, width in zip(shape, chunks))
        if grid_count > min(budget.max_grid, budget.max_chunks):
            raise UnsupportedCase("checked pointer index grid exceeds the configured quota")
        with ModernH5File(image, large_sparse_scan=True, max_chunks=grid_count,
                          max_chunk_bytes=min(128 << 20, max(2 * budget.max_chunk_bytes, 2 << 20)),
                          repair_scan_bytes=min(budget.max_scan_bytes, budget.max_source_bytes),
                          repair_scan_seconds=min(budget.max_seconds, 86400)) as reader:
            width = _file_element_width(typ, reader.superblock.offset_size)
            if width <= 0 or prod(chunks) * width > budget.max_chunk_bytes:
                raise UnsupportedCase("heap-backed file records exceed the decoded chunk budget")
            index = reader.read_index(object_address, shape, chunks, width,
                                      maxshape=maxshape, filters=filters, max_chunks=grid_count)
            repairs = list(index.reconstructed_links)
            if index.reconstructed_data_block_pointer:
                repairs.append({"kind": "fahd_to_fadb", "source_address": index.base_address,
                                "target_address": index.data_block_address,
                                "pointer_offset": index.data_block_pointer_offset})
            if not repairs:
                raise UnsupportedCase("checked native relocation requires a damaged, uniquely repaired index link")
            if not index.chunks or len(index.chunks) > budget.max_chunks:
                raise UnsupportedCase("checked pointer index has no bounded allocated chunks")
            offsets = [item["pointer_offset"] for item in repairs]
            if len(set(offsets)) != len(offsets):
                raise FormatError("checked index repairs reuse a pointer slot")
            ranges = []
            seen = set()
            for record in index.chunks:
                coordinate = record.coordinate
                if (coordinate in seen or len(coordinate) != len(shape)
                        or any(n < 0 or n >= axis or n % width
                               for n, axis, width in zip(coordinate, shape, chunks))):
                    raise FormatError("checked pointer index has a repeated or invalid chunk coordinate")
                seen.add(coordinate)
                start = reader.absolute(record.address)
                if (record.size <= 0 or record.size > 2 * budget.max_chunk_bytes
                        or start + record.size > min(size, reader.superblock.eof_address)
                        or record.filter_mask >> len(filters)):
                    raise FormatError("checked pointer index has an invalid chunk size, range, or filter mask")
                ranges.append((start, start + record.size))
            ranges.sort()
            if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
                raise FormatError("checked pointer index has overlapping chunk allocations")
            if any(start < meta_end and meta_start < end
                   for start, end in ranges for meta_start, meta_end, _ in reader.metadata_ranges):
                raise FormatError("checked chunk allocation overlaps independently parsed metadata")
            expected_records = _index_records(index)
            offset_size = reader.superblock.offset_size

        # A second sparse copy is writable only inside this private directory.
        # The first snapshot and the caller's source are never modified.
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-index-view-", dir=output.parent) as directory:
            working = Path(directory) / "repaired-view.h5"
            with image.open("rb") as original, working.open("xb") as replacement:
                cloned_digest, _ = _copy_sparse(original, replacement, size=size,
                                                parent=working.parent, budget=budget, deadline=deadline)
            if cloned_digest != digest:
                raise RecoveryError("disposable index view differs from the checked source snapshot")
            with working.open("r+b") as editable:
                for repair in repairs:
                    location, address = int(repair["pointer_offset"]), int(repair["target_address"])
                    if location < 0 or location + offset_size > size:
                        raise FormatError("checked repaired pointer is outside the source image")
                    editable.seek(location)
                    if editable.read(offset_size) == address.to_bytes(offset_size, "little"):
                        raise FormatError("checked repaired pointer has not changed")
                    editable.seek(location)
                    editable.write(address.to_bytes(offset_size, "little"))
            with ModernH5File(working, large_sparse_scan=True, max_chunks=grid_count,
                              max_chunk_bytes=min(128 << 20, max(2 * budget.max_chunk_bytes, 2 << 20)),
                              repair_scan_bytes=min(budget.max_scan_bytes, budget.max_source_bytes),
                              repair_scan_seconds=min(budget.max_seconds, 86400)) as checked:
                restored = checked.read_index(object_address, shape, chunks, width,
                                              maxshape=maxshape, filters=filters, max_chunks=grid_count)
                if (restored.reconstructed_links or restored.reconstructed_data_block_pointer
                        or _index_records(restored) != expected_records):
                    raise FormatError("disposable index view fails checked original-link restoration")
            staged_output = Path(directory) / "result.h5"
            staged_report = Path(directory) / "result.json"
            with h5py.File(working, "r") as native:
                result = export_native_stream(
                    source, dataset_path, staged_output, staged_report,
                    published_output=published_output or output, budget=budget,
                    opened_file=native, inspection_image=working,
                )
            result["mode"] = "checked_index_native_relocation"
            result["operation"] = "checked_index_native_relocation"
            result["index"] = {
                "type": index.index_type, "repairs": repairs,
                "original_checksum_restored_in_private_view": True,
                "checked_allocation_count": len(expected_records),
                "original_source_sha256": digest,
            }
            result["value_evidence"] = (
                "Rooted checked index reconstruction in a private source view; native heap and "
                "reference decoding, target relocation, and logical output readback. Unallocated "
                "and failed elements are not accepted; other-object references remain deferred."
            )
            serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
            with h5py.File(staged_output, "r+") as derived:
                derived[report_metadata_group(result) + "/report_json"][()] = serialized
            staged_report.write_text(serialized, encoding="utf-8")
            if (time.monotonic() > deadline or staged_output.stat().st_size > budget.max_output_bytes
                    or sha256_file(image) != digest):
                raise RecoveryError("relocated index export exceeded the quota or original snapshot changed")
            _verify_source(source, identity, digest)
            _validate_paths(source, output, report_path)
            os.link(staged_report, report_path)
            try:
                os.link(staged_output, output)
            except Exception:
                report_path.unlink(missing_ok=True)
                raise
            return result
