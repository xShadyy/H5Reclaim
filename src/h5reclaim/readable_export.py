"""Copy a fully native-readable numeric dataset without claiming damage recovery.

This route is intentionally separate from structural recovery. The HDF5 library
must resolve every selected value and every chunk must be allocated. A successful
round trip establishes what the current input reads as, not what was measured
before a possible corruption. A private bounded snapshot keeps source reads
consistent and the caller's file remains untouched.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import tempfile
from math import prod
from pathlib import Path
from typing import Any, Iterator

import h5py
import numpy as np

from .hints import DatasetHints, HintsError, compare_hints, require_no_conflicts
from .metadata import UnsupportedCase, _safe_scalar_attributes
from .recovery import (
    RecoveryError,
    VERSION,
    _validate_paths,
    _verify_source,
    sha256_file,
    source_snapshot,
)


MAX_DATA_BYTES = 128 * 1024 * 1024
MAX_BLOCK_BYTES = 1024 * 1024
MAX_STORED_CHUNK_BYTES = 2 * 1024 * 1024
MAX_CHUNKS = 8192
MAX_RANK = 4
MAX_PATH_BYTES = 4096
# These filters ship with HDF5. Unknown filters may load third-party plugins.
NATIVE_FILTERS = frozenset((h5py.h5z.FILTER_DEFLATE, h5py.h5z.FILTER_SHUFFLE,
                            h5py.h5z.FILTER_FLETCHER32))


def _selected_dataset(handle: h5py.File, path: str) -> h5py.Dataset:
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise UnsupportedCase("select an absolute dataset path such as /group/data")
    if len(path.encode("utf-8")) > MAX_PATH_BYTES or any(
        ord(character) < 32 or ord(character) == 127 for character in path
    ):
        raise UnsupportedCase("selected dataset path is too long")
    parts = path[1:].split("/")
    if len(parts) > 64 or any(part in ("", ".", "..") for part in parts):
        raise UnsupportedCase("selected dataset path must be canonical")
    if parts[0] == "_h5reclaim":
        raise UnsupportedCase("the /_h5reclaim output namespace is reserved")
    current: h5py.Group | h5py.Dataset = handle["/"]
    for position, part in enumerate(parts):
        if not isinstance(current, h5py.Group):
            raise UnsupportedCase("selected path contains a non-group parent")
        link = current.get(part, getlink=True)
        if not isinstance(link, h5py.HardLink):
            raise UnsupportedCase("selected path must use only local hard links")
        current = current[part]
        if position != len(parts) - 1 and not isinstance(current, h5py.Group):
            raise UnsupportedCase("selected path contains a non-group parent")
    if not isinstance(current, h5py.Dataset):
        raise UnsupportedCase("selected path is not a dataset")
    return current


def _canonical_numeric(dataset: h5py.Dataset) -> np.dtype:
    """Accept ordinary full-width fixed numeric HDF5 representations only."""
    dtype = dataset.dtype
    if dtype.kind not in "iuf" or dtype.subdtype is not None or dtype.itemsize not in (1, 2, 4, 8):
        raise UnsupportedCase("readable export requires a primitive fixed-width integer or IEEE float")
    datatype = dataset.id.get_type()
    klass = datatype.get_class()
    if datatype.get_size() != dtype.itemsize or datatype.get_precision() != dtype.itemsize * 8:
        raise UnsupportedCase("numeric datatype has noncanonical storage or bit precision")
    if datatype.get_offset() != 0 or datatype.get_pad() != (h5py.h5t.PAD_ZERO, h5py.h5t.PAD_ZERO):
        raise UnsupportedCase("numeric datatype has noncanonical bit offset or padding")
    orders = (h5py.h5t.ORDER_LE, h5py.h5t.ORDER_BE)
    if dtype.itemsize == 1:
        orders += (h5py.h5t.ORDER_NONE,)
    if datatype.get_order() not in orders:
        raise UnsupportedCase("numeric datatype has unsupported byte order")
    if dtype.kind in "iu":
        if klass != h5py.h5t.INTEGER or datatype.get_sign() != (
            h5py.h5t.SGN_2 if dtype.kind == "i" else h5py.h5t.SGN_NONE
        ):
            raise UnsupportedCase("integer representation is not canonical")
    else:
        fields, bias = {
            4: ((31, 23, 8, 0, 23), 127),
            8: ((63, 52, 11, 0, 52), 1023),
        }.get(dtype.itemsize, (None, None))
        if (klass != h5py.h5t.FLOAT or datatype.get_fields() != fields
                or datatype.get_ebias() != bias or datatype.get_norm() != h5py.h5t.NORM_IMPLIED
                or datatype.get_inpad() != h5py.h5t.PAD_ZERO):
            raise UnsupportedCase("floating-point representation is not IEEE binary32 or binary64")
    return dtype


def _check_storage(dataset: h5py.Dataset, snapshot_size: int) -> tuple[str, int]:
    creation = dataset.id.get_create_plist()
    if creation.get_external_count() or dataset.is_virtual:
        raise UnsupportedCase("external and virtual dataset storage is not supported")
    filter_count = creation.get_nfilters()
    if filter_count > 8:
        raise UnsupportedCase("filter pipeline exceeds the eight-filter limit")
    filters = [creation.get_filter(index)[0] for index in range(filter_count)]
    if any(value not in NATIVE_FILTERS for value in filters):
        raise UnsupportedCase("filter pipeline would require a plugin or exceeds the limit")
    layout = creation.get_layout()
    if layout == h5py.h5d.CHUNKED:
        chunks = dataset.chunks
        assert chunks is not None
        if prod(chunks) * dataset.dtype.itemsize > MAX_BLOCK_BYTES:
            raise UnsupportedCase("decoded chunk exceeds the 1 MiB limit")
        grid = prod((length + width - 1) // width for length, width in zip(dataset.shape, chunks))
        if grid > MAX_CHUNKS:
            raise UnsupportedCase(f"dataset exceeds the {MAX_CHUNKS}-chunk limit")
        # Native reads may silently substitute fill values for missing index
        # entries. Demand an allocated record at every coordinate instead.
        if dataset.id.get_num_chunks() != grid:
            raise UnsupportedCase("some chunks are unallocated or unreachable; native reads could substitute fill values")
        ranges: list[tuple[int, int]] = []
        for indices in itertools.product(*(range(0, length, width) for length, width in zip(dataset.shape, chunks))):
            chunk = dataset.id.get_chunk_info_by_coord(indices)
            address, length = int(chunk.byte_offset), int(chunk.size)
            if tuple(int(position) for position in chunk.chunk_offset) != tuple(indices):
                raise UnsupportedCase("a chunk record disagrees with its requested coordinate")
            if int(chunk.filter_mask) & ~((1 << filter_count) - 1):
                raise UnsupportedCase("a chunk filter mask references an absent filter")
            if address < 0 or length <= 0 or address + length > snapshot_size:
                raise UnsupportedCase("a chunk has no valid allocated payload in the source file")
            if length > MAX_STORED_CHUNK_BYTES:
                raise UnsupportedCase("a stored chunk exceeds the 2 MiB limit")
            ranges.append((address, address + length))
        ranges.sort()
        if any(first[1] > second[0] for first, second in zip(ranges, ranges[1:])):
            raise UnsupportedCase("different chunk records claim overlapping source bytes")
        return "chunked", grid
    if filters:
        raise UnsupportedCase("filters require chunked storage")
    if layout in (h5py.h5d.CONTIGUOUS, h5py.h5d.COMPACT):
        # For these two layouts there are no per-chunk allocation records.
        # Refuse a merely defined, unwritten dataset whose reads use fill.
        if dataset.id.get_storage_size() != prod(dataset.shape) * dataset.dtype.itemsize:
            raise UnsupportedCase("the contiguous or compact dataset is not fully allocated")
        if layout == h5py.h5d.CONTIGUOUS:
            address = dataset.id.get_offset()
            if address is None or address < 0 or address + dataset.id.get_storage_size() > snapshot_size:
                raise UnsupportedCase("contiguous payload address lies outside the source file")
        return ("compact" if layout == h5py.h5d.COMPACT else "contiguous"), 0
    raise UnsupportedCase("dataset layout is not a local compact, contiguous, or chunked layout")


def _blocks(shape: tuple[int, ...], itemsize: int) -> Iterator[tuple[slice, ...]]:
    dimensions = [1] * len(shape)
    allowance = MAX_BLOCK_BYTES // itemsize
    for index in range(len(shape) - 1, -1, -1):
        dimensions[index] = min(shape[index], max(1, allowance))
        allowance = max(1, allowance // dimensions[index])
    for starts in itertools.product(*(range(0, size, step) for size, step in zip(shape, dimensions))):
        yield tuple(slice(start, min(start + step, size)) for start, step, size in zip(starts, dimensions, shape))


def export_readable(source: str | Path, dataset_path: str, output: str | Path,
                    report_path: str | Path, *, hints: DatasetHints | None = None) -> dict[str, Any]:
    """Export one entirely readable, allocated numeric dataset to a new file.

    This uses only the damaged/current file as input. It never reconstructs an
    index or verifies earlier scientific truth. Destinations must not exist.
    """
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    with source_snapshot(source) as (snapshot, digest, identity, source_size):
        try:
            with h5py.File(snapshot, "r") as original:
                selected = _selected_dataset(original, dataset_path)
                dtype = _canonical_numeric(selected)
                shape = tuple(int(value) for value in selected.shape)
                if not 1 <= len(shape) <= MAX_RANK or any(length <= 0 for length in shape):
                    raise UnsupportedCase("expected a nonempty dataset with rank one through four")
                logical_bytes = prod(shape) * dtype.itemsize
                if logical_bytes > MAX_DATA_BYTES:
                    raise UnsupportedCase("selected logical data exceeds the 128 MiB limit")
                layout, allocated_chunks = _check_storage(selected, source_size)
                creation = selected.id.get_create_plist()
                observed_filters = tuple(int(creation.get_filter(index)[0])
                                         for index in range(creation.get_nfilters()))
                if hints is not None and hints.chunks is not None and selected.chunks is None:
                    raise HintsError("operator hints assert chunks but selected storage is not chunked")
                comparisons = compare_hints(
                    hints,
                    observed_fields={
                        "path": dataset_path,
                        "shape": shape,
                        "chunks": selected.chunks,
                        "dtype": dtype.str,
                        "filters": observed_filters,
                    },
                    input_sha256=digest,
                ) if hints is not None else ()
                require_no_conflicts(comparisons)
                attributes, omitted = _safe_scalar_attributes(selected)
                source_values = hashlib.sha256()
                block_count = 0
                with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=output.parent) as out_dir:
                    with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=report_path.parent) as rep_dir:
                        output_temp = Path(out_dir) / "output.h5"
                        report_temp = Path(rep_dir) / "report.json"
                        with h5py.File(output_temp, "x") as target:
                            exported = target.create_dataset(dataset_path, shape=shape, dtype=dtype)
                            for selection in _blocks(shape, dtype.itemsize):
                                block = selected[selection]
                                if block.dtype != dtype or block.nbytes > MAX_BLOCK_BYTES:
                                    raise RecoveryError("native read returned an unexpected numeric block")
                                exported[selection] = block
                                source_values.update(block.tobytes(order="C"))
                                block_count += 1
                            for name, value in attributes:
                                exported.attrs[name] = value
                            exported.attrs["h5reclaim_mode"] = "readable_export"
                            exported.attrs["h5reclaim_warning"] = (
                                "Native HDF5 read and bitwise output round trip only; "
                                "historical scientific values are not established."
                            )
                            target.flush()

                        output_values = hashlib.sha256()
                        with h5py.File(output_temp, "r") as target:
                            exported = target[dataset_path]
                            for selection in _blocks(shape, dtype.itemsize):
                                output_values.update(exported[selection].tobytes(order="C"))
                        if output_values.digest() != source_values.digest():
                            raise RecoveryError("output values differ from native reads of the source snapshot")

                        report: dict[str, Any] = {
                            "schema_version": 1,
                            "tool": "h5reclaim",
                            "tool_version": VERSION,
                            "mode": "readable_export",
                            "outcome": "complete",
                            "source": {
                                "path": str(source), "size_bytes": source_size,
                                "sha256_before": digest, "sha256_after": digest,
                            },
                            "dataset": {
                                "path": dataset_path, "shape": list(shape), "dtype": dtype.str,
                                "layout": layout, "allocated_chunks_checked": allocated_chunks,
                                "logical_bytes": logical_bytes, "attributes_copied": [name for name, _ in attributes],
                                "attributes_omitted": list(omitted),
                            },
                            "output_path": str(output),
                            "blocks_verified": block_count,
                            "native_value_sha256": source_values.hexdigest(),
                            "operator_hints": {
                                "trust_level": hints.trust_level,
                                "note": hints.note,
                                "comparisons": [vars(comparison) for comparison in comparisons],
                                "warning": "Matching hints do not prove the origin or historical value of measurements.",
                            } if hints is not None else None,
                            "verification": "all allocated values read through native HDF5 and output read back bitwise equal",
                            "limits": (
                                "This copies current native-readable values, not structurally recovered bytes. "
                                "Allocation and round-trip checks cannot establish earlier scientific truth. "
                                "Other objects, links, dimension scales, and non-scalar attributes are not copied."
                            ),
                        }
                        report_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
                        with h5py.File(output_temp, "r+") as target:
                            meta = target.create_group("/_h5reclaim")
                            meta.create_dataset(
                                "report_json", data=report_text,
                                dtype=h5py.string_dtype(encoding="utf-8"),
                            )
                            meta.attrs["source_sha256"] = digest
                            meta.attrs["report_schema_version"] = 1
                        report_temp.write_text(report_text, encoding="utf-8")
                        if sha256_file(snapshot) != digest:
                            raise RecoveryError("private source snapshot changed during readable export")
                        _verify_source(source, identity, digest)
                        _validate_paths(source, output, report_path)
                        published = False
                        try:
                            os.link(output_temp, output)
                            published = True
                            os.link(report_temp, report_path)
                        except Exception:
                            if published:
                                output.unlink(missing_ok=True)
                            raise
                        return report
        except (UnsupportedCase, HintsError):
            raise
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            raise RecoveryError(f"native HDF5 readable export failed: {exc}") from exc
