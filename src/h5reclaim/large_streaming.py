"""Quota-bound, sparse-aware export of large *currently readable* datasets.

This route deliberately does not repair a damaged chunk index. Native HDF5
must enumerate every accepted allocation; structural recovery has independent
and much smaller metadata limits. A sparse source snapshot avoids materializing
holes in a large container, and the output stores validity and physical-range
evidence as bounded datasets instead of a potentially enormous JSON ledger.
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import h5py
import numpy as np

from .metadata import UnsupportedCase
from .ownership_inventory import inventory_other_allocations
from .readable_export import (
    NATIVE_FILTERS, _create_matching_dataset, _require_no_competing_owner,
    _safe_fixed_type, _selected_dataset,
)
from .recovery import (
    RecoveryError, VERSION, _handle_matches_path, _identity, _validate_paths,
    _verify_source, sha256_file,
)


MIB = 1024 ** 2
GIB = 1024 ** 3
MAX_SOURCE_BYTES = 64 * GIB
MAX_COPIED_BYTES = 8 * GIB
MAX_LOGICAL_BYTES = 16 * GIB
MAX_OUTPUT_BYTES = 18 * GIB
MAX_CHUNKS = 65536
MAX_GRID = 1_048_576
MAX_CHUNK_BYTES = 8 * MIB
MAX_SECONDS = 850
BLOCK_BYTES = MIB
DISK_RESERVE_BYTES = 64 * MIB


@dataclass(frozen=True)
class LargeBudget:
    max_source_bytes: int = MAX_SOURCE_BYTES
    max_copied_bytes: int = MAX_COPIED_BYTES
    max_logical_bytes: int = MAX_LOGICAL_BYTES
    max_output_bytes: int = MAX_OUTPUT_BYTES
    max_chunks: int = MAX_CHUNKS
    max_grid: int = MAX_GRID
    max_seconds: float = MAX_SECONDS
    block_bytes: int = BLOCK_BYTES
    disk_reserve_bytes: int = DISK_RESERVE_BYTES

    def __post_init__(self) -> None:
        if (not 0 < self.max_source_bytes <= 256 * GIB
                or not 0 < self.max_copied_bytes <= self.max_source_bytes
                or not 0 < self.max_logical_bytes <= 64 * GIB
                or not 0 < self.max_output_bytes <= 128 * GIB
                or not 0 < self.max_chunks <= 262144
                or not 0 < self.max_grid <= 8_388_608
                or not 0 < self.max_seconds <= 24 * 3600
                or not 0 < self.block_bytes <= 4 * MIB
                or not 0 <= self.disk_reserve_bytes <= GIB):
            raise ValueError("invalid large export resource budget")


def _deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise UnsupportedCase("large export exceeded its elapsed-time quota")


def _hash_zeros(digest: "hashlib._Hash", count: int, block_bytes: int,
                deadline: float) -> None:
    zeros = bytes(block_bytes)
    while count:
        _deadline(deadline)
        length = min(count, block_bytes)
        digest.update(zeros[:length])
        count -= length


def _copy_dense(source, target, *, size: int, parent: Path,
                budget: LargeBudget, deadline: float) -> tuple[str, int]:
    """Bounded fallback on systems without sparse extent enumeration.

    Windows users can process inputs above the regular 4 GiB limit when they
    explicitly have enough disk for the entire source. The copy quota and
    real free space are enforced; this never silently expands a huge hole.
    """
    if size > budget.max_copied_bytes or shutil.disk_usage(parent).free < size + budget.disk_reserve_bytes:
        raise UnsupportedCase("a full private copy exceeds the copy or free-disk quota")
    source.seek(0)
    target.seek(0)
    digest = hashlib.sha256()
    total = 0
    while total < size:
        _deadline(deadline)
        block = source.read(min(budget.block_bytes, size - total))
        if not block:
            raise RecoveryError("source ended before its declared size")
        target.write(block)
        digest.update(block)
        total += len(block)
    target.flush()
    return digest.hexdigest(), total


def _has_sparse_extents() -> bool:
    return hasattr(os, "SEEK_DATA") and hasattr(os, "SEEK_HOLE")


def _copy_sparse(source, target, *, size: int, parent: Path,
                 budget: LargeBudget, deadline: float) -> tuple[str, int]:
    """Preserve SEEK_HOLE ranges; hash their logical zero bytes as well.

    SEEK_DATA and SEEK_HOLE are filesystem contracts, not content inference.
    If either is unavailable, a full copy is allowed only under a separate
    explicit byte and free-space quota.
    """
    if not _has_sparse_extents():
        return _copy_dense(source, target, size=size, parent=parent,
                           budget=budget, deadline=deadline)
    digest = hashlib.sha256()
    total_written = 0
    cursor = 0
    target.truncate(size)
    while cursor < size:
        _deadline(deadline)
        try:
            data_start = os.lseek(source.fileno(), cursor, os.SEEK_DATA)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                _hash_zeros(digest, size - cursor, budget.block_bytes, deadline)
                break
            if cursor == 0 and exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
                return _copy_dense(source, target, size=size, parent=parent,
                                   budget=budget, deadline=deadline)
            raise UnsupportedCase("sparse extent lookup failed; input was not copied") from exc
        if not cursor <= data_start <= size:
            raise RecoveryError("filesystem returned an invalid sparse data extent")
        _hash_zeros(digest, data_start - cursor, budget.block_bytes, deadline)
        try:
            hole_start = os.lseek(source.fileno(), data_start, os.SEEK_HOLE)
        except OSError as exc:
            raise UnsupportedCase("sparse hole lookup failed; input was not copied") from exc
        if not data_start < hole_start <= size:
            raise RecoveryError("filesystem returned an invalid sparse hole extent")
        source.seek(data_start)
        target.seek(data_start)
        remaining = hole_start - data_start
        while remaining:
            _deadline(deadline)
            length = min(budget.block_bytes, remaining)
            if (total_written + length > budget.max_copied_bytes
                    or shutil.disk_usage(parent).free < length + budget.disk_reserve_bytes):
                raise UnsupportedCase("sparse snapshot exceeded its copy or free-disk quota")
            block = source.read(length)
            if len(block) != length:
                raise RecoveryError("source ended inside a reported data extent")
            target.write(block)
            digest.update(block)
            total_written += length
            remaining -= length
        cursor = hole_start
    target.flush()
    return digest.hexdigest(), total_written


@contextmanager
def sparse_snapshot(source: str | Path, *, budget: LargeBudget | None = None
                    ) -> Iterator[tuple[Path, str, tuple[int, int, int, int, int], int, int]]:
    """Yield one read-only private sparse image and its source SHA-256.

    The caller must verify the image and pathname again before publication.
    Sparse holes are logical zeros, and physical data extents are fully copied.
    """
    budget = budget or LargeBudget()
    source = Path(source)
    if not source.is_file():
        raise RecoveryError(f"input is not a regular file: {source}")
    path_info = source.stat()
    if path_info.st_size > budget.max_source_bytes:
        raise UnsupportedCase("source exceeds the large sparse snapshot size quota")
    deadline = time.monotonic() + budget.max_seconds
    with source.open("rb") as opened:
        file_info = os.fstat(opened.fileno())
        identity = _identity(path_info)
        if (not stat.S_ISREG(file_info.st_mode)
                or _identity(source.stat()) != identity
                or not _handle_matches_path(file_info, path_info)):
            raise RecoveryError("input changed while it was being opened")
        with tempfile.TemporaryDirectory(prefix="h5reclaim-large-") as directory:
            image = Path(directory) / "source.h5"
            with image.open("xb") as target:
                digest, copied = _copy_sparse(
                    opened, target, size=file_info.st_size, parent=image.parent,
                    budget=budget, deadline=deadline,
                )
            if (_identity(os.fstat(opened.fileno())) != _identity(file_info)
                    or _identity(source.stat()) != identity):
                raise RecoveryError("input changed while making a sparse snapshot")
            if sha256_file(image) != digest:
                raise RecoveryError("sparse snapshot bytes disagree with captured source")
            # The caller performs the full pathname rehash immediately before
            # publishing. A second source rehash here would scan large holes
            # without changing that final publication guarantee.
            yield image, digest, identity, file_info.st_size, copied


def _hash_range(handle, offset: int, size: int, *, deadline: float) -> str:
    handle.seek(offset)
    digest = hashlib.sha256()
    while size:
        _deadline(deadline)
        block = handle.read(min(size, BLOCK_BYTES))
        if not block:
            raise RecoveryError("physical chunk range ended before its declared length")
        digest.update(block)
        size -= len(block)
    return digest.hexdigest()


def export_large_readable(source: str | Path, dataset_path: str, output: str | Path,
                          report_path: str | Path, *,
                          published_output: Path | None = None,
                          budget: LargeBudget | None = None) -> dict:
    """Export bounded 1D numeric data with sparse validity and range evidence.

    This is a native-readable export, not a repair route. A corrupt or missing
    selected index refuses, even if orphaned raw payload bytes remain present.
    """
    source, output, report_path = Path(source), Path(output), Path(report_path)
    budget = budget or LargeBudget()
    _validate_paths(source, output, report_path)
    deadline = time.monotonic() + budget.max_seconds
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, copied):
        with h5py.File(image, "r") as opened:
            selected = _selected_dataset(opened, dataset_path)
            dtype = selected.dtype
            _safe_fixed_type(selected.id.get_type(), dtype)
            if (dtype.kind not in "iuf" or dtype.itemsize not in (1, 2, 4, 8)
                    or dtype.subdtype is not None or len(selected.shape) != 1
                    or selected.shape[0] < 1):
                raise UnsupportedCase("large export requires a nonempty 1D primitive numeric dataset")
            logical_bytes = int(selected.shape[0]) * dtype.itemsize
            if logical_bytes > budget.max_logical_bytes:
                raise UnsupportedCase("logical dataset exceeds the large export byte quota")
            creation = selected.id.get_create_plist()
            if creation.get_external_count() or selected.is_virtual:
                raise UnsupportedCase("large export does not follow external or virtual storage")
            filter_count = creation.get_nfilters()
            if filter_count > 8:
                raise UnsupportedCase("filter pipeline exceeds the eight-filter bound")
            filters = tuple(int(creation.get_filter(i)[0]) for i in range(filter_count))
            for value in filters:
                if (value not in NATIVE_FILTERS or not h5py.h5z.filter_avail(value)
                        or (h5py.h5z.get_filter_info(value) & 3) != 3):
                    raise UnsupportedCase("large export requires built-in encoders and decoders")
            layout = creation.get_layout()
            records: list[tuple[int, int, int, int]] = []
            if layout == h5py.h5d.CHUNKED:
                chunks = selected.chunks
                assert chunks is not None
                width = int(chunks[0])
                if width * dtype.itemsize > budget.max_logical_bytes or width * dtype.itemsize > MAX_CHUNK_BYTES:
                    raise UnsupportedCase("decoded chunk exceeds the 8 MiB bound")
                grid = (selected.shape[0] + width - 1) // width
                if grid > budget.max_grid:
                    raise UnsupportedCase("chunk grid exceeds the sparse validity map bound")
                count = int(selected.id.get_num_chunks())
                if count > budget.max_chunks:
                    raise UnsupportedCase("allocated chunks exceed the large export record quota")
                seen: set[int] = set()
                for index in range(count):
                    _deadline(deadline)
                    info = selected.id.get_chunk_info(index)
                    origin = int(info.chunk_offset[0])
                    address, length, mask = int(info.byte_offset), int(info.size), int(info.filter_mask)
                    if (origin < 0 or origin >= selected.shape[0] or origin % width
                            or origin in seen or address < 0 or length <= 0
                            or length > MAX_CHUNK_BYTES or address > size or length > size - address
                            or mask & ~((1 << filter_count) - 1)):
                        raise UnsupportedCase("chunk index has an invalid coordinate, range, size or filter mask")
                    by_coord = selected.id.get_chunk_info_by_coord((origin,))
                    if (by_coord.byte_offset is None
                            or tuple(by_coord.chunk_offset) != (origin,)
                            or int(by_coord.byte_offset) != address
                            or int(by_coord.size) != length
                            or int(by_coord.filter_mask) != mask):
                        raise UnsupportedCase("chunk index enumeration and coordinate lookup disagree")
                    seen.add(origin)
                    records.append((origin, address, length, mask))
                ranges = sorted((address, address + length) for _, address, length, _ in records)
                if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
                    raise UnsupportedCase("selected chunks claim overlapping physical ranges")
                records.sort()
                if selected.id.get_num_chunks() != len(records):
                    raise UnsupportedCase("chunk allocation count changed while enumerating")
                ownership_ranges = [(address, address + length, (origin,))
                                    for origin, address, length, _ in records]
            elif layout == h5py.h5d.CONTIGUOUS:
                if filters or int(selected.id.get_storage_size()) != logical_bytes:
                    raise UnsupportedCase("contiguous values are not fully allocated")
                address = selected.id.get_offset()
                if address is None or address < 0 or address > size or logical_bytes > size - address:
                    raise UnsupportedCase("contiguous source bytes are missing")
                grid = 0
                ownership_ranges = [(int(address), int(address) + logical_bytes, ())]
            else:
                raise UnsupportedCase("large export accepts only chunked or contiguous storage")
            inventory = inventory_other_allocations(
                image, int(h5py.h5o.get_info(selected.id).addr),
                max_allocations=max(budget.max_chunks, 65536), max_seconds=120,
            )
            owners = _require_no_competing_owner(inventory, ownership_ranges)
            source_values = hashlib.sha256()
            evidence_dtype = np.dtype([
                ("origin", "<u8"), ("source_offset", "<u8"), ("stored_bytes", "<u8"),
                ("filter_mask", "<u4"), ("raw_sha256", "S64"),
                ("decoded_sha256", "S64"),
            ])
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-large-", dir=output.parent) as out_dir:
                with tempfile.TemporaryDirectory(prefix=".h5reclaim-large-", dir=report_path.parent) as rep_dir:
                    out_temp = Path(out_dir) / "output.h5"
                    rep_temp = Path(rep_dir) / "report.json"
                    with h5py.File(out_temp, "x") as result, image.open("rb") as raw:
                        target = _create_matching_dataset(result, selected, dataset_path)
                        meta = result.require_group("/_h5reclaim")
                        evidence = meta.create_dataset("physical_evidence", shape=(len(records),),
                                                       dtype=evidence_dtype,
                                                       chunks=(min(len(records), 1024),)
                                                       if records else None)
                        if layout == h5py.h5d.CHUNKED:
                            validity = meta.create_dataset("validity", shape=(grid,), dtype="u1",
                                                           fillvalue=0, chunks=(min(grid, 4096),))
                            iterator = ((origin, min(origin + width, selected.shape[0]), address,
                                         length, mask) for origin, address, length, mask in records)
                        else:
                            iterator = ((start, min(start + max(1, BLOCK_BYTES // dtype.itemsize),
                                                     selected.shape[0]), -1, 0, 0)
                                        for start in range(0, selected.shape[0],
                                                           max(1, BLOCK_BYTES // dtype.itemsize)))
                        count = 0
                        for start, end, address, length, mask in iterator:
                            _deadline(deadline)
                            values = np.asarray(selected[start:end])
                            if values.dtype != dtype or values.nbytes > MAX_CHUNK_BYTES:
                                raise RecoveryError("native read exceeded bounded numeric block")
                            target[start:end] = values
                            encoded = values.tobytes(order="C")
                            source_values.update(encoded)
                            if layout == h5py.h5d.CHUNKED:
                                validity[start // width] = 1
                                evidence[count] = (start, address, length, mask,
                                                   _hash_range(raw, address, length, deadline=deadline),
                                                   hashlib.sha256(encoded).hexdigest())
                            count += 1
                            result.flush()
                            if (out_temp.stat().st_size > budget.max_output_bytes
                                    or shutil.disk_usage(out_temp.parent).free < budget.disk_reserve_bytes):
                                raise UnsupportedCase("output exceeded its size or free-disk quota")
                    output_values = hashlib.sha256()
                    with h5py.File(out_temp, "r") as result:
                        target = result[dataset_path]
                        if layout == h5py.h5d.CHUNKED:
                            selections = ((start, min(start + width, selected.shape[0]))
                                          for start, _, _, _ in records)
                        else:
                            step = max(1, BLOCK_BYTES // dtype.itemsize)
                            selections = ((start, min(start + step, selected.shape[0]))
                                          for start in range(0, selected.shape[0], step))
                        for start, end in selections:
                            _deadline(deadline)
                            output_values.update(np.asarray(target[start:end]).tobytes(order="C"))
                    if output_values.digest() != source_values.digest():
                        raise RecoveryError("output numeric values differ from the source snapshot")
                    accepted = (sum(min(width, selected.shape[0] - start)
                                    for start, _, _, _ in records)
                                if layout == h5py.h5d.CHUNKED else selected.shape[0])
                    report = {
                        "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                        "mode": "large_native_readable_export",
                        "outcome": "partial" if accepted < selected.shape[0] else "complete",
                        "source": {"path": str(source), "size_bytes": size,
                                   "sha256_before": digest, "sha256_after": digest,
                                   "snapshot_physical_data_bytes_copied": copied},
                        "dataset": {"path": dataset_path, "shape": list(selected.shape),
                                    "dtype": dtype.str, "maxshape": list(selected.maxshape),
                                    "chunks": list(selected.chunks) if selected.chunks else None,
                                    "layout": "chunked" if records or layout == h5py.h5d.CHUNKED else "contiguous",
                                    "filters_in_order": list(filters),
                                    "source_object_header_address": int(h5py.h5o.get_info(selected.id).addr),
                                    "source_contiguous_byte_range": (
                                        {"start": int(address), "end_exclusive": int(address) + logical_bytes}
                                        if layout == h5py.h5d.CONTIGUOUS else None)},
                        "allocated_chunks_checked": len(records), "accepted_elements": int(accepted),
                        "unknown_elements": int(selected.shape[0] - accepted),
                        "validity_map": "/_h5reclaim/validity" if layout == h5py.h5d.CHUNKED else None,
                        "physical_evidence": "/_h5reclaim/physical_evidence",
                        "physical_evidence_scope": "native-enumerated physical chunk ranges and current raw/decoded SHA-256",
                        "validity_codes": {"0": "unallocated or unknown; ignore fill values",
                                           "1": "native-allocated, decoded and read back bitwise equal"}
                                          if layout == h5py.h5d.CHUNKED else None,
                        "ownership_inventory": owners,
                        "native_value_sha256": source_values.hexdigest(),
                        "output_path": str(published_output or output),
                        "limits": (
                            "Current native-readable values only. This route cannot reconstruct damaged "
                            "indexes, attest historical measurement bytes, or infer unallocated chunks. "
                            "Only one-dimensional canonical primitive numeric local storage is supported. "
                            "Other objects, attributes, links and dimension scales are not copied."
                        ),
                    }
                    report_text = json.dumps(report, sort_keys=True, indent=2) + "\n"
                    with h5py.File(out_temp, "r+") as result:
                        meta = result["/_h5reclaim"]
                        meta.create_dataset("report_json", data=report_text,
                                            dtype=h5py.string_dtype(encoding="utf-8"))
                        meta.attrs["source_sha256"] = digest
                        meta.attrs["report_schema_version"] = 1
                    if out_temp.stat().st_size > budget.max_output_bytes:
                        raise UnsupportedCase("output exceeds the size quota")
                    rep_temp.write_text(report_text, encoding="utf-8")
                    if sha256_file(image) != digest:
                        raise RecoveryError("private snapshot changed during large export")
                    _verify_source(source, identity, digest)
                    _validate_paths(source, output, report_path)
                    published = False
                    try:
                        os.link(out_temp, output)
                        published = True
                        os.link(rep_temp, report_path)
                    except Exception:
                        if published:
                            output.unlink(missing_ok=True)
                        raise
                    return report
