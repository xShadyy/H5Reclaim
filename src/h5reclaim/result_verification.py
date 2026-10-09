"""Bounded consistency checks for a published H5Reclaim output and report.

This checks the *current* artifact's report, source annotation and validity
maps. It does not read or hash recovered values, authenticate jointly modified
artifacts, or prove historical measurement values. The CLI runs the HDF5
reader in a limited child process.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


class ResultMismatch(ValueError):
    """Published output and evidence report disagree."""


class ResultUnsupported(ValueError):
    """This artifact cannot be completely checked with the available maps."""


DEFAULT_REPORT_BYTES = 32 * 1024 * 1024
DEFAULT_MAP_BYTES = 64 * 1024 * 1024
DEFAULT_SOURCE_BYTES = 16 * 1024 * 1024 * 1024
DEFAULT_MEMORY_BYTES = 1536 * 1024 * 1024


def _positive(value: int, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _read_json(path: Path, limit: int) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ResultUnsupported("saved report exceeds the configured byte budget")
    try:
        result = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ResultMismatch("saved report is not valid JSON") from exc
    if not isinstance(result, dict) or result.get("schema_version") != 1 or result.get("tool") != "h5reclaim":
        raise ResultUnsupported("expected a H5Reclaim schema-version-1 recovery report")
    return result


def _hash_source(path: Path, expected: str, limit: int) -> None:
    before = path.stat()
    if before.st_size > limit:
        raise ResultUnsupported("source exceeds the configured hashing byte budget")
    digest = hashlib.sha256()
    consumed = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            consumed += len(block)
            if consumed > limit:
                raise ResultUnsupported("source grew beyond the configured hashing byte budget")
            digest.update(block)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ResultMismatch("source changed while its digest was being checked")
    if digest.hexdigest() != expected:
        raise ResultMismatch("explicit source SHA-256 differs from the report")


def _digest(report: dict[str, Any]) -> str:
    source = report.get("source")
    digest = source.get("sha256_before") if isinstance(source, dict) else None
    if (not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)):
        raise ResultMismatch("report has no valid source SHA-256")
    if source.get("sha256_after", digest) != digest:
        raise ResultMismatch("report declares inconsistent source SHA-256 values")
    return digest


def _check_source_annotation(metadata: Any, expected: str, label: str) -> None:
    if "source_sha256" not in metadata.attrs:
        raise ResultUnsupported(f"{label} publishes no source SHA-256 annotation")
    if metadata.attrs["source_sha256"] != expected:
        raise ResultMismatch(f"{label} source digest annotation differs from the saved report")


def _absolute(path: Any, label: str) -> str:
    if (not isinstance(path, str) or not path.startswith("/") or path == "/"
            or "\x00" in path or any(part in ("", ".", "..") for part in path[1:].split("/"))):
        raise ResultMismatch(f"{label} is not a valid absolute HDF5 path")
    return path


def _local_object(handle: Any, path: str, label: str):
    """Resolve a path only after every link in its chain is a local hard link."""
    import h5py

    prefix = ""
    for component in path[1:].split("/"):
        prefix += "/" + component
        link = handle.get(prefix, getlink=True)
        if not isinstance(link, h5py.HardLink):
            raise ResultMismatch(f"{label} is missing or follows a soft/external link: {prefix}")
    try:
        return handle[path]
    except (KeyError, OSError, ValueError) as exc:
        raise ResultMismatch(f"{label} cannot be opened at {path}") from exc


def _local_storage(dataset: Any, label: str) -> None:
    """Avoid virtual or external raw storage despite a hard-linked HDF5 object."""
    if dataset.is_virtual or dataset.id.get_create_plist().get_external_count():
        raise ResultMismatch(f"{label} uses virtual or external raw storage")


def _embedded(handle: Any, metadata_group: str, limit: int,
              aggregate_remaining: list[int]) -> dict[str, Any]:
    import h5py

    path = metadata_group + "/report_json"
    obj = _local_object(handle, path, "embedded report")
    if not isinstance(obj, h5py.Dataset) or obj.shape != () or h5py.check_string_dtype(obj.dtype) is None:
        raise ResultMismatch(f"embedded report at {path} is not a scalar string")
    _local_storage(obj, "embedded report")
    # A variable-length HDF5 string can allocate more than its on-disk pointer
    # indicates. The CLI runs this read in a memory-limited child process.
    value = obj[()]
    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, bytes):
        raw = value
    else:
        raise ResultMismatch(f"embedded report at {path} is not UTF-8 text")
    if len(raw) > limit:
        raise ResultUnsupported("embedded report exceeds the configured byte budget")
    aggregate_remaining[0] -= len(raw)
    if aggregate_remaining[0] < 0:
        raise ResultUnsupported("embedded reports exceed the aggregate byte budget")
    try:
        result = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ResultMismatch(f"embedded report at {path} is not valid JSON") from exc
    if not isinstance(result, dict):
        raise ResultMismatch(f"embedded report at {path} is not a JSON object")
    return result


def _codes(raw: Any, label: str) -> dict[str, int]:
    if not isinstance(raw, dict) or not raw:
        raise ResultMismatch(f"{label} has no declared status codes")
    result: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ResultMismatch(f"{label} has a non-string status name")
        if key.isdecimal() and isinstance(value, str):
            code = int(key)
        elif type(value) is int:
            code = value
        else:
            raise ResultMismatch(f"{label} has an invalid status code")
        if not 0 <= code <= 255:
            raise ResultMismatch(f"{label} declares a status code outside uint8")
        result[key] = code
    if (result.get("recovered", result.get("1")) != 1
            or 1 not in result.values()):
        raise ResultMismatch(f"{label} does not identify code 1 as accepted")
    return result


def _map_definitions(report: dict[str, Any]) -> list[tuple[str, str, dict[str, int]]]:
    validity = report.get("validity")
    if validity is not None and not isinstance(validity, dict):
        raise ResultMismatch("report validity definition is invalid")
    validity = validity or {}
    definitions: list[tuple[Any, str, Any]] = []
    generic = validity.get("dataset")
    if generic is not None:
        granularity = validity.get("granularity")
        if not isinstance(granularity, str):
            raise ResultMismatch("generic validity map has no granularity")
        unit = "element" if "element" in granularity else "chunk" if "chunk" in granularity else None
        if unit is None:
            raise ResultMismatch("generic validity map has unsupported granularity")
        definitions.append((generic, unit, validity.get("codes")))
    if validity.get("chunk_status") is not None:
        definitions.append((validity["chunk_status"], "chunk", validity.get("codes")))
    if validity.get("element_status") is not None:
        definitions.append((validity["element_status"], "element",
                            validity.get("element_codes")))
    if report.get("element_status") is not None:
        definitions.append((report["element_status"], "element", report.get("validity_codes")))
    if report.get("validity_map") is not None:
        path = report["validity_map"]
        element_paths = {item[0] for item in definitions if item[1] == "element"}
        definitions.append((path, "element" if path in element_paths else "auto",
                            report.get("validity_codes")))
    if not definitions:
        raise ResultUnsupported(
            "output/report consistency could not be fully checked because this route publishes no validity map"
        )
    unique: dict[str, tuple[str, dict[str, int]]] = {}
    for path, unit, raw_codes in definitions:
        path = _absolute(path, "validity map path")
        codes = _codes(raw_codes, f"validity map {path}")
        previous = unique.get(path)
        if previous is not None:
            if previous[0] != unit and "auto" not in (unit, previous[0]):
                raise ResultMismatch(f"validity map {path} has conflicting granularity")
            if set(previous[1].values()) != set(codes.values()):
                raise ResultMismatch(f"validity map {path} has conflicting code declarations")
            if previous[0] == "auto":
                unique[path] = (unit, codes)
        else:
            unique[path] = (unit, codes)
    if len(unique) > 2 or sum(unit == "element" for unit, _ in unique.values()) > 1:
        raise ResultUnsupported("more than one element validity map is declared")
    return [(path, unit, codes) for path, (unit, codes) in unique.items()]


def _tiles(shape: tuple[int, ...], limit: int):
    from itertools import product

    if not shape:
        yield ()
        return
    if any(size == 0 for size in shape):
        return
    widths = [1] * len(shape)
    capacity = limit
    for axis in range(len(shape) - 1, -1, -1):
        widths[axis] = min(shape[axis], capacity)
        capacity = max(1, capacity // widths[axis])
    ranges = (range(0, size, width) for size, width in zip(shape, widths))
    for origin in product(*ranges):
        yield tuple(slice(start, min(start + width, size))
                    for start, width, size in zip(origin, widths, shape))


def _scan_map(status: Any, shape: tuple[int, ...], chunks: tuple[int, ...] | None,
              unit: str, remaining: int) -> tuple[dict[int, int], int, str, int]:
    import numpy as np

    if status.dtype != np.dtype("u1") or status.shape is None:
        raise ResultMismatch("validity map must be an unsigned one-byte dataset")
    grid = (tuple((size + width - 1) // width for size, width in zip(shape, chunks))
            if chunks is not None else None)
    if unit == "auto":
        unit = "element" if status.shape == shape else "chunk" if status.shape == grid else (
            "whole" if chunks is None and status.shape == (1,) else "unsupported")
    expected = shape if unit == "element" else grid if unit == "chunk" else (1,) if unit == "whole" else None
    if expected is None or status.shape != expected:
        raise ResultMismatch("validity map shape does not match its declared granularity or chunk grid")
    count = int(np.prod(status.shape, dtype=object)) if status.shape else 1
    if count > remaining:
        raise ResultUnsupported("status maps exceed the configured scan byte budget")
    if status.chunks is not None and int(np.prod(status.chunks, dtype=object)) > min(remaining, 8 * 1024 * 1024):
        raise ResultUnsupported("a status-map chunk exceeds the scan memory budget")
    histogram: dict[int, int] = {}
    accepted_elements = 0
    for selection in _tiles(status.shape, min(1024 * 1024, max(1, remaining))):
        observed = np.asarray(status[selection if selection else ()])
        counts = np.bincount(observed.reshape(-1), minlength=256)
        for code in np.flatnonzero(counts):
            histogram[int(code)] = histogram.get(int(code), 0) + int(counts[code])
        if unit == "element":
            accepted_elements += int(counts[1])
        elif unit == "whole":
            if int(counts[1]):
                accepted_elements += int(np.prod(shape, dtype=object)) if shape else 1
        elif int(counts[1]):
            weight = np.asarray(observed == 1, dtype=np.int64)
            for axis, (size, width) in enumerate(zip(shape, chunks)):
                start = selection[axis].start
                stop = selection[axis].stop
                lengths = np.minimum(width, size - np.arange(start, stop, dtype=np.int64) * width)
                reshape = [1] * len(shape)
                reshape[axis] = len(lengths)
                weight *= lengths.reshape(reshape)
            accepted_elements += int(weight.sum())
    return histogram, accepted_elements, unit, count


def _verify_dataset(handle: Any, report: dict[str, Any], max_report_bytes: int,
                    map_remaining: int, embedded_remaining: list[int]) -> tuple[dict[str, Any], int]:
    import h5py
    import numpy as np

    descriptor = report.get("dataset")
    if not isinstance(descriptor, dict):
        raise ResultMismatch("dataset report has no dataset descriptor")
    path = _absolute(descriptor.get("path"), "dataset path")
    metadata_group = _absolute(report.get("metadata_group") or "/_h5reclaim", "metadata group")
    embedded = _embedded(handle, metadata_group, max_report_bytes, embedded_remaining)
    if embedded != report:
        raise ResultMismatch(f"saved and embedded dataset reports differ for {path}")
    metadata = _local_object(handle, metadata_group, "metadata group")
    if not isinstance(metadata, h5py.Group):
        raise ResultMismatch(f"metadata group is missing for {path}")
    _check_source_annotation(metadata, _digest(report), f"dataset {path}")
    data = _local_object(handle, path, "reported dataset")
    if not isinstance(data, h5py.Dataset):
        raise ResultMismatch(f"reported dataset is missing: {path}")
    shape = data.shape
    if shape is None:
        raise ResultUnsupported(f"null dataset {path} has no checkable validity map")
    claimed_shape = descriptor.get("shape")
    if (not isinstance(claimed_shape, list) or any(type(n) is not int or n < 0 for n in claimed_shape)
            or tuple(claimed_shape) != shape):
        raise ResultMismatch(f"output dataset shape differs from the report for {path}")
    if "chunks" in descriptor:
        declared_chunks = descriptor["chunks"]
        if (declared_chunks is not None and
                (not isinstance(declared_chunks, list)
                 or any(type(size) is not int or size <= 0 for size in declared_chunks))):
            raise ResultMismatch(f"reported chunk dimensions are invalid for {path}")
        if (tuple(declared_chunks) if declared_chunks is not None else None) != data.chunks:
            raise ResultMismatch(f"output chunk dimensions differ from the report for {path}")
    total = int(np.prod(shape, dtype=object)) if shape else 1
    definitions = _map_definitions(report)
    primary = None
    chunk_result = None
    scanned = 0
    for map_path, unit, codes in definitions:
        if not map_path.startswith(metadata_group + "/") or map_path == metadata_group + "/report_json":
            raise ResultMismatch(f"validity map is outside the dataset's metadata group: {map_path}")
        status = _local_object(handle, map_path, "validity map")
        if not isinstance(status, h5py.Dataset):
            raise ResultMismatch(f"declared validity map is missing: {map_path}")
        _local_storage(status, "validity map")
        histogram, accepted, actual_unit, byte_count = _scan_map(
            status, shape, data.chunks, unit, map_remaining - scanned)
        scanned += byte_count
        if not set(histogram).issubset(set(codes.values())):
            raise ResultMismatch(f"validity map contains an undeclared status code: {map_path}")
        if "codes_json" in status.attrs:
            annotation = status.attrs["codes_json"]
            if not isinstance(annotation, (bytes, str)) or len(annotation) > 8192:
                raise ResultMismatch(f"status code annotation is invalid: {map_path}")
            try:
                annotated = _codes(json.loads(annotation), f"status code annotation {map_path}")
            except (UnicodeError, ValueError) as exc:
                raise ResultMismatch(f"status code annotation is invalid: {map_path}") from exc
            if set(annotated.values()) != set(codes.values()):
                raise ResultMismatch(f"status code annotation differs from report: {map_path}")
        if actual_unit == "chunk":
            chunk_result = (histogram, accepted, codes)
        if primary is None or actual_unit == "element":
            primary = (histogram, accepted, actual_unit, codes)
    if primary is None:
        raise ResultUnsupported(f"no checkable validity map for {path}")
    _, accepted, unit, _ = primary
    if accepted > total:
        raise ResultMismatch(f"accepted element total exceeds dataset extent for {path}")
    claimed_accepted = report.get("accepted_elements")
    claimed_unknown = report.get("unknown_elements")
    if (claimed_accepted is None) != (claimed_unknown is None):
        raise ResultMismatch(f"report has incomplete element totals for {path}")
    if claimed_accepted is not None:
        if (type(claimed_accepted) is not int or type(claimed_unknown) is not int
                or claimed_accepted != accepted or claimed_unknown != total - accepted):
            raise ResultMismatch(f"accepted/unknown element totals differ from validity for {path}")
    counts = report.get("counts")
    if counts is not None:
        if not isinstance(counts, dict):
            raise ResultMismatch(f"report status counts are invalid for {path}")
        hist, _, map_codes = chunk_result if chunk_result is not None else (
            primary[0], accepted, primary[3])
        # Structural routes name every status. A report with additional unit
        # maps still uses the chunk map for its chunk counts.
        for name, claimed in counts.items():
            if name not in map_codes or type(claimed) is not int or claimed != hist.get(map_codes[name], 0):
                raise ResultMismatch(f"reported status counts differ from the validity map for {path}")
        for code, observed_count in hist.items():
            if (observed_count and not any(name in counts and declared == code
                                           for name, declared in map_codes.items())):
                raise ResultMismatch(f"observed status is absent from reported counts for {path}")
    if report.get("outcome") == "complete" and accepted != total:
        raise ResultMismatch(f"complete outcome conflicts with unknown elements for {path}")
    if report.get("outcome") not in ("complete", "partial"):
        raise ResultMismatch(f"dataset outcome is invalid for {path}")
    return {"path": path, "accepted_elements": accepted, "unknown_elements": total - accepted,
            "primary_map_granularity": unit, "maps_checked": len(definitions)}, scanned


def _verify_local(output_path: Path, report_path: Path, *, source_path: Path | None,
                  max_report_bytes: int, max_map_bytes: int, max_source_bytes: int) -> dict[str, Any]:
    import h5py

    report = _read_json(report_path, max_report_bytes)
    source_digest = _digest(report)
    if source_path is not None:
        _hash_source(source_path, source_digest, max_source_bytes)
    with h5py.File(output_path, "r") as output:
        embedded_remaining = [max_report_bytes * 2]
        group = _absolute(report.get("metadata_group") or "/_h5reclaim", "main metadata group")
        metadata = _local_object(output, group, "main metadata group")
        if not isinstance(metadata, h5py.Group):
            raise ResultMismatch("main report metadata group is missing")
        _check_source_annotation(metadata, source_digest, "main report")
        if _embedded(output, group, max_report_bytes, embedded_remaining) != report:
            raise ResultMismatch("saved and embedded main reports differ")
        if report.get("mode") == "whole_file_recovery":
            records = report.get("datasets")
            if not isinstance(records, list) or not records or len(records) > 10000:
                raise ResultUnsupported("whole-file report has no bounded dataset record list")
            paths = set()
            reports = []
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("report"), dict):
                    raise ResultMismatch("whole-file dataset record is invalid")
                selected = record["report"]
                descriptor = selected.get("dataset")
                if (not isinstance(descriptor, dict) or record.get("path") != descriptor.get("path")
                        or record["path"] in paths):
                    raise ResultMismatch("whole-file dataset record path is wrong or duplicated")
                paths.add(record["path"])
                reports.append(selected)
            if (report.get("datasets_exported") != len(records)
                    or report.get("datasets_failed") != len(report.get("failures", []))):
                raise ResultMismatch("whole-file exported/failed totals differ from records")
        else:
            if "datasets" in report:
                raise ResultUnsupported("unknown report mode with nested datasets")
            reports = [report]
        checked, consumed = [], 0
        for selected in reports:
            result, used = _verify_dataset(output, selected, max_report_bytes,
                                           max_map_bytes - consumed, embedded_remaining)
            consumed += used
            checked.append(result)
    return {"status": "passed", "datasets_checked": len(checked), "datasets": checked,
            "status_bytes_scanned": consumed, "explicit_source_sha256_checked": source_path is not None,
            "scope": "report/map/schema consistency only; recovered values are not read or hashed; no proof of historical measurements or independent authenticity"}


def verify_result(output_path: str | Path, report_path: str | Path, *,
                  source_path: str | Path | None = None,
                  max_report_bytes: int = DEFAULT_REPORT_BYTES,
                  max_map_bytes: int = DEFAULT_MAP_BYTES,
                  max_source_bytes: int = DEFAULT_SOURCE_BYTES,
                  memory_bytes: int = DEFAULT_MEMORY_BYTES,
                  timeout_seconds: int = 45) -> dict[str, Any]:
    """Check a published artifact in a memory and time limited HDF5 worker.

    Raises ResultMismatch on inconsistency, ResultUnsupported when a route
    lacks sufficient evidence or exceeds a budget, and OSError on worker
    failure. The optional explicit source is hashed against the saved report.
    """
    from .worker_limits import run_worker

    for name, value in (("max_report_bytes", max_report_bytes), ("max_map_bytes", max_map_bytes),
                        ("max_source_bytes", max_source_bytes), ("memory_bytes", memory_bytes),
                        ("timeout_seconds", timeout_seconds)):
        _positive(value, name)
    request = {"output": str(Path(output_path).absolute()), "report": str(Path(report_path).absolute()),
               "source": str(Path(source_path).absolute()) if source_path is not None else None,
               "max_report_bytes": max_report_bytes, "max_map_bytes": max_map_bytes,
               "max_source_bytes": max_source_bytes, "memory_bytes": memory_bytes}
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-verify-") as directory:
        input_path = Path(directory) / "request.json"
        result_path = Path(directory) / "result.json"
        input_path.write_text(json.dumps(request), encoding="utf-8")
        env = dict(os.environ)
        env["HDF5_PLUGIN_PRELOAD"] = "::"
        env.pop("HDF5_PLUGIN_PATH", None)
        env.pop("HDF5_EXTFILE_PREFIX", None)
        try:
            child = run_worker([sys.executable, "-m", "h5reclaim.result_verification",
                                str(input_path), str(result_path)], env=env,
                               timeout_seconds=timeout_seconds, memory_bytes=memory_bytes)
        except subprocess.TimeoutExpired as exc:
            raise ResultUnsupported("verification exceeded the configured time budget") from exc
        if not result_path.is_file() or result_path.stat().st_size > 65536:
            raise OSError(f"verification worker stopped without a bounded response (exit {child.returncode})")
        result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("status") == "passed" and child.returncode == 0:
        return result
    if result.get("status") == "mismatch":
        raise ResultMismatch(str(result.get("reason", "output and report differ")))
    if result.get("status") == "unsupported":
        raise ResultUnsupported(str(result.get("reason", "unsupported artifact")))
    raise OSError(str(result.get("reason", "verification worker failed")))


def _worker_main() -> int:
    if len(sys.argv) != 3:
        return 2
    result_path = Path(sys.argv[2])
    try:
        with Path(sys.argv[1]).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("verification request exceeds 64 KiB")
        request = json.loads(raw)
        from .native_worker import _apply_memory_limit
        _apply_memory_limit(request["memory_bytes"])
        result = _verify_local(Path(request["output"]), Path(request["report"]),
                               source_path=Path(request["source"]) if request["source"] else None,
                               max_report_bytes=request["max_report_bytes"],
                               max_map_bytes=request["max_map_bytes"],
                               max_source_bytes=request["max_source_bytes"])
        exit_code = 0
    except ResultMismatch as exc:
        result, exit_code = {"status": "mismatch", "reason": str(exc)[:1000]}, 1
    except ResultUnsupported as exc:
        result, exit_code = {"status": "unsupported", "reason": str(exc)[:1000]}, 2
    except Exception as exc:
        result, exit_code = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:1000]}, 2
    result_path.write_text(json.dumps(result, ensure_ascii=True), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(_worker_main())
