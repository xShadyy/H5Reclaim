"""Read a bounded selection from a recovery with its reported validity mask.

``read_masked("rescued.h5", "rescued.report.json", "/measurements",
               selection=(slice(100, 200),))`` returns a NumPy masked array.
Only status code 1 is exposed as a measurement. Other reported codes are
masked, even when the HDF5 dataset displays a plausible fill value. This is
current-value validity, not a claim of equality to a historical capture.

The byte limit bounds the logical result, the status-map selection, and each
HDF5 chunk's uncompressed size. HDF5 and filter implementations may use
additional internal memory; use a separate worker for untrusted files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import h5py
import numpy as np


class MaskedReadError(ValueError):
    """The report or status map cannot safely describe the requested values."""


def _selection(selection: Any, shape: tuple[int, ...]) -> tuple[tuple[slice, ...], tuple[int, ...]]:
    if selection is None:
        parts: tuple[Any, ...] = ()
    elif isinstance(selection, tuple):
        parts = selection
    else:
        parts = (selection,)
    ellipses = sum(part is Ellipsis for part in parts)
    if ellipses > 1 or len(parts) - ellipses > len(shape):
        raise MaskedReadError("selection has too many axes")
    if ellipses:
        where = next(i for i, part in enumerate(parts) if part is Ellipsis)
        parts = parts[:where] + (slice(None),) * (len(shape) - len(parts) + 1) + parts[where + 1:]
    parts += (slice(None),) * (len(shape) - len(parts))
    normalized = []
    squeezed = []
    for axis, (part, size) in enumerate(zip(parts, shape)):
        if isinstance(part, (int, np.integer)) and not isinstance(part, (bool, np.bool_)):
            index = int(part)
            if index < 0:
                index += size
            if index < 0 or index >= size:
                raise IndexError(f"selection index is outside axis {axis}")
            normalized.append(slice(index, index + 1))
            squeezed.append(axis)
        elif isinstance(part, slice):
            if part.step not in (None, 1):
                raise MaskedReadError("only forward unit-step slices are supported")
            start, stop, _ = part.indices(size)
            normalized.append(slice(start, max(start, stop)))
        else:
            raise MaskedReadError("selection must contain only integers, slices, or one ellipsis")
    return tuple(normalized), tuple(squeezed)


def _units(shape: tuple[int, ...]) -> int:
    return int(np.prod(shape, dtype=object)) if shape else 1


def _report(path: Path, max_bytes: int) -> dict[str, Any]:
    if path.stat().st_size > max_bytes:
        raise MaskedReadError("report exceeds the byte limit")
    with path.open("r", encoding="utf-8") as source:
        report = json.load(source)
    if not isinstance(report, dict) or report.get("schema_version") != 1 or report.get("tool") != "h5reclaim":
        raise MaskedReadError("expected an H5Reclaim version-1 report")
    return report


def _dataset_report(report: dict[str, Any], path: str) -> dict[str, Any]:
    if isinstance(report.get("datasets"), list):
        matches = [item for item in report["datasets"]
                   if isinstance(item, dict) and item.get("path") == path]
        if len(matches) != 1 or not isinstance(matches[0].get("report"), dict):
            raise MaskedReadError("dataset is absent or ambiguous in the whole-file report")
        selected = matches[0]["report"]
    else:
        selected = report
    if not isinstance(selected.get("dataset"), dict) or selected["dataset"].get("path") != path:
        raise MaskedReadError("dataset path does not match the selected report")
    return selected


def _map_definition(report: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    validity = report.get("validity")
    validity = validity if isinstance(validity, dict) else {}
    element_paths = [path for path in (validity.get("element_status"), report.get("element_status"))
                     if path is not None]
    if any(not isinstance(path, str) for path in element_paths):
        raise MaskedReadError("report has an invalid element status path")
    if len(set(element_paths)) > 1:
        raise MaskedReadError("report declares ambiguous element status maps")
    if not element_paths and "element" not in str(validity.get("granularity", "")):
        paths = [path for path in (validity.get("dataset"), validity.get("chunk_status"),
                                   report.get("validity_map")) if path is not None]
        if any(not isinstance(path, str) for path in paths):
            raise MaskedReadError("report has an invalid chunk status path")
        if len(set(paths)) > 1:
            raise MaskedReadError("report declares ambiguous chunk status maps")
    candidates = (
        (validity.get("element_status"), "element", validity.get("element_codes") or validity.get("codes")),
        (report.get("element_status"), "element", report.get("validity_codes")),
        (validity.get("dataset"), validity.get("granularity"), validity.get("codes")),
        (validity.get("chunk_status"), "chunk", validity.get("codes")),
        (report.get("validity_map"), None, report.get("validity_codes")),
    )
    for path, unit, codes in candidates:
        if path is None:
            continue
        if not isinstance(path, str) or not path.startswith("/") or not isinstance(codes, dict):
            raise MaskedReadError("report does not define an absolute map path and its status codes")
        if unit is not None and not isinstance(unit, str):
            raise MaskedReadError("report has an invalid status granularity")
        accepted = codes.get("recovered", codes.get("1"))
        if isinstance(accepted, int) and not isinstance(accepted, bool):
            if accepted != 1:
                raise MaskedReadError("report does not identify status code 1 as accepted")
        elif not isinstance(accepted, str):
            raise MaskedReadError("report does not declare accepted status code 1")
        return path, unit, codes
    raise MaskedReadError("report does not declare a validity map")


def _check_chunks(dataset: h5py.Dataset, limit: int) -> None:
    if dataset.chunks is not None and _units(dataset.chunks) * dataset.dtype.itemsize > limit:
        raise MaskedReadError("an HDF5 chunk exceeds the byte limit")


def read_masked(
    output_path: str | Path,
    report_path: str | Path,
    dataset_path: str,
    *,
    selection: Any = None,
    max_bytes: int = 64 * 1024 * 1024,
    max_elements: int = 1_000_000,
) -> np.ma.MaskedArray:
    """Return selected recovered values with all nonaccepted positions masked.

    ``selection`` supports integers, forward unit-step slices and an ellipsis.
    It is applied before data and status-map reads. Fixed-size HDF5 types are
    supported; variable-length values cannot be bounded by their dtype size.
    The report must explicitly name its map and accepted code. For a whole-file
    report, ``dataset_path`` selects exactly one nested dataset report.
    """
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(max_elements, int) or isinstance(max_elements, bool) or max_elements <= 0:
        raise ValueError("max_elements must be a positive integer")
    if not isinstance(dataset_path, str) or not dataset_path.startswith("/"):
        raise ValueError("dataset_path must be an absolute HDF5 path")
    report_limit = min(max(max_bytes, 1024 * 1024), 64 * 1024 * 1024)
    report = _dataset_report(_report(Path(report_path), report_limit), dataset_path)
    map_path, claimed_unit, codes = _map_definition(report)
    with h5py.File(output_path, "r") as recovered:
        if dataset_path not in recovered or map_path not in recovered:
            raise MaskedReadError("reported dataset or status map is missing from the output")
        data = recovered[dataset_path]
        status = recovered[map_path]
        if not isinstance(data, h5py.Dataset) or not isinstance(status, h5py.Dataset):
            raise MaskedReadError("reported value or status path is not a dataset")
        if data.shape is None or data.dtype.hasobject:
            raise MaskedReadError("null and variable-length datasets have no bounded fixed-size read")
        if status.dtype != np.dtype("u1"):
            raise MaskedReadError("status map must contain unsigned one-byte codes")
        source = report.get("source")
        digest = source.get("sha256_before") if isinstance(source, dict) else None
        metadata_group = report.get("metadata_group") or map_path.rsplit("/", 1)[0]
        if (not isinstance(digest, str) or not isinstance(metadata_group, str)
                or metadata_group not in recovered
                or recovered[metadata_group].attrs.get("source_sha256") != digest):
            raise MaskedReadError("output metadata does not match the selected report source")
        expected_shape = report["dataset"].get("shape")
        if expected_shape is not None and tuple(expected_shape) != data.shape:
            raise MaskedReadError("output dataset shape differs from the report")
        if map_path == dataset_path:
            raise MaskedReadError("value dataset cannot also serve as its status map")
        element = status.shape == data.shape
        chunk_grid = (tuple((size + width - 1) // width for size, width in zip(data.shape, data.chunks))
                      if data.chunks is not None else None)
        chunk = chunk_grid is not None and status.shape == chunk_grid
        if claimed_unit is not None:
            if "element" in claimed_unit and not element:
                raise MaskedReadError("reported element status has a different shape")
            if "chunk" in claimed_unit and not chunk:
                raise MaskedReadError("reported chunk grid has a different shape")
        if not element and not chunk:
            raise MaskedReadError("status shape is neither an element map nor the dataset chunk grid")
        use_chunk = chunk and not element if claimed_unit is None else "chunk" in claimed_unit
        parts, squeezed = _selection(selection, data.shape)
        result_shape = tuple(part.stop - part.start for part in parts)
        n = _units(result_shape)
        if n > max_elements:
            raise MaskedReadError("selection exceeds the element limit")
        if use_chunk:
            grid_start = tuple(part.start // width for part, width in zip(parts, data.chunks))
            grid_stop = tuple((part.stop - 1) // width + 1 if part.stop > part.start else start
                              for part, width, start in zip(parts, data.chunks, grid_start))
            map_selection = tuple(slice(start, stop) for start, stop in zip(grid_start, grid_stop))
            map_count = _units(tuple(stop - start for start, stop in zip(grid_start, grid_stop)))
        else:
            map_selection = parts
            map_count = n
        if (n * (data.dtype.itemsize + 2) + map_count > max_bytes):
            raise MaskedReadError("selection exceeds the byte limit")
        _check_chunks(data, max_bytes)
        _check_chunks(status, max_bytes)
        observed = np.asarray(status[map_selection if status.ndim else ()])
        declared = {int(value) for key, value in codes.items() if key != "1" and isinstance(value, int)
                    and not isinstance(value, bool)}
        declared.update(int(key) for key in codes if isinstance(key, str) and key.isdecimal())
        if not set(np.unique(observed).tolist()).issubset(declared):
            raise MaskedReadError("status map contains a code absent from the report")
        if use_chunk:
            indices = tuple(np.arange(part.start, part.stop, dtype=np.int64) // width - start
                            for part, width, start in zip(parts, data.chunks, grid_start))
            decisions = observed[np.ix_(*indices)]
        else:
            decisions = observed
        values = np.asarray(data[parts if data.ndim else ()])
        masked = np.ma.array(values, mask=decisions != 1, copy=False)
        if squeezed:
            masked = np.squeeze(masked, axis=squeezed)
        return masked
