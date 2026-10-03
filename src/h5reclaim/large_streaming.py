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
from itertools import product
from math import isfinite, prod
from pathlib import Path
from typing import Iterator

import h5py
import numpy as np

from .metadata import UnsupportedCase
from .native_io import read_fixed_block, write_fixed_block
from .native_addresses import chunk_address
from .ownership_inventory import inventory_other_allocations
from .sparse_io import has_sparse_extents, prepare_sparse_file, sparse_extents
from .readable_export import (
    NATIVE_FILTERS, _create_matching_dataset, _require_no_competing_owner,
    _safe_export_attributes, _safe_fixed_type, _selected_dataset,
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
MAX_GRID = 64 * 1024**2
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
    max_metadata_bytes: int = 64 * MIB
    max_objects: int = 100_000
    max_links: int = 500_000
    max_chunk_bytes: int = 64 * MIB
    logical_batch_records: int = 256
    max_type_depth: int = 64
    max_type_members: int = 65536
    worker_memory_bytes: int = 3 * GIB

    def __post_init__(self) -> None:
        integer_fields = (self.max_source_bytes, self.max_copied_bytes, self.max_logical_bytes,
                          self.max_output_bytes, self.max_chunks, self.max_grid,
                          self.block_bytes, self.disk_reserve_bytes, self.max_metadata_bytes,
                          self.max_objects, self.max_links, self.max_chunk_bytes,
                          self.logical_batch_records, self.max_type_depth, self.max_type_members, self.worker_memory_bytes)
        if (any(type(value) is not int for value in integer_fields)
                or type(self.max_seconds) not in (int, float) or not isfinite(self.max_seconds)):
            raise ValueError("large export budget requires integer counts and a finite duration")
        if (not 0 < self.max_source_bytes < 1 << 63
                or not 0 < self.max_copied_bytes <= self.max_source_bytes
                or not 0 < self.max_logical_bytes < 1 << 63
                or not 0 < self.max_output_bytes < 1 << 63
                or not 0 < self.max_chunks < 1 << 63
                or not 0 < self.max_grid < 1 << 63
                or not 0 < self.max_seconds
                or not 0 < self.block_bytes <= 64 * MIB
                or not 0 <= self.disk_reserve_bytes < 1 << 63
                or not 0 < self.max_metadata_bytes < 1 << 63
                or not 0 < self.max_objects < 1 << 63
                or not 0 < self.max_links < 1 << 63
                or not self.block_bytes <= self.max_chunk_bytes < 1 << 63
                or not 0 < self.logical_batch_records < 1 << 63
                or not 0 < self.max_type_depth <= 256
                or not 0 < self.max_type_members < 1 << 63
                or not 0 < self.worker_memory_bytes < 1 << 63):
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

    Users of filesystems without allocation enumeration can process large
    inputs when they have enough disk for the entire source. The copy quota and
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
    return has_sparse_extents()


def _copy_sparse(source, target, *, size: int, parent: Path,
                 budget: LargeBudget, deadline: float) -> tuple[str, int]:
    """Preserve filesystem-reported holes and hash their logical zero bytes.

    POSIX SEEK_DATA/HOLE and Windows allocated ranges are OS contracts.
    If unavailable, a full copy is allowed only under a separate
    explicit byte and free-space quota.
    """
    if not _has_sparse_extents():
        return _copy_dense(source, target, size=size, parent=parent,
                           budget=budget, deadline=deadline)
    digest = hashlib.sha256()
    total_written = 0
    cursor = 0
    extents = iter(sparse_extents(source.fileno(), size))
    try:
        first = next(extents, None)
        prepare_sparse_file(target.fileno())
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
            return _copy_dense(source, target, size=size, parent=parent,
                               budget=budget, deadline=deadline)
        raise UnsupportedCase("sparse extent lookup failed; input was not copied") from exc
    target.truncate(size)
    from itertools import chain
    for data_start, hole_start in chain(() if first is None else (first,), extents):
        _deadline(deadline)
        if not cursor <= data_start < hole_start <= size:
            raise RecoveryError("filesystem returned an invalid sparse data extent")
        _hash_zeros(digest, data_start - cursor, budget.block_bytes, deadline)
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
    _hash_zeros(digest, size - cursor, budget.block_bytes, deadline)
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
    from .source_session import reused_image
    shared = reused_image(source)
    if shared is not None:
        if shared["size"] > budget.max_source_bytes or shared["copied"] > budget.max_copied_bytes:
            raise UnsupportedCase("shared source image exceeds the requested snapshot budget")
        yield source, shared["sha256"], _identity(source.stat()), shared["size"], shared["copied"]
        return
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


def _hash_range(handle, offset: int, size: int, *, deadline: float,
                block_bytes: int = BLOCK_BYTES) -> str:
    handle.seek(offset)
    digest = hashlib.sha256()
    while size:
        _deadline(deadline)
        block = handle.read(min(size, block_bytes))
        if not block:
            raise RecoveryError("physical chunk range ended before its declared length")
        digest.update(block)
        size -= len(block)
    return digest.hexdigest()


def _stream_blocks(shape: tuple[int, ...], itemsize: int, block_bytes: int
                   ) -> Iterator[tuple[slice, ...]]:
    dimensions = [1] * len(shape)
    allowance = max(1, block_bytes // itemsize)
    for index in range(len(shape) - 1, -1, -1):
        dimensions[index] = min(shape[index], allowance)
        allowance = max(1, allowance // dimensions[index])
    for starts in product(*(range(0, size, step) for size, step in zip(shape, dimensions))):
        yield tuple(slice(start, min(start + step, size))
                    for start, step, size in zip(starts, dimensions, shape))


def export_large_readable(source: str | Path, dataset_path: str, output: str | Path,
                          report_path: str | Path, *,
                          published_output: Path | None = None,
                          budget: LargeBudget | None = None) -> dict:
    """Stream fixed records of any HDF5 rank with validity and range evidence.

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
            shape = tuple(int(value) for value in selected.shape)
            if not 1 <= len(shape) <= 32 or any(length < 1 for length in shape):
                raise UnsupportedCase("streaming export needs nonempty fixed records of rank 1 through 32")
            rank = len(shape)
            elements = prod(shape)
            logical_bytes = elements * dtype.itemsize
            if logical_bytes > budget.max_logical_bytes:
                raise UnsupportedCase("logical dataset exceeds the large export byte quota")
            creation = selected.id.get_create_plist()
            if creation.get_external_count() or selected.is_virtual:
                raise UnsupportedCase("large export does not follow external or virtual storage")
            filter_count = creation.get_nfilters()
            if filter_count > 8:
                raise UnsupportedCase("filter pipeline exceeds the eight-filter bound")
            filters = tuple(int(creation.get_filter(i)[0]) for i in range(filter_count))
            from .filter_registry import supported_filters
            available_filters = supported_filters()
            for value in filters:
                if (value not in available_filters or not h5py.h5z.filter_avail(value)
                        or (h5py.h5z.get_filter_info(value) & 3) != 3):
                    raise UnsupportedCase("large export requires available encoders and decoders; install h5reclaim[filters] for packaged codecs")
            layout = creation.get_layout()
            records: list[tuple[tuple[int, ...], int, int, int]] = []
            if layout == h5py.h5d.CHUNKED:
                chunks = selected.chunks
                assert chunks is not None
                chunks = tuple(int(value) for value in chunks)
                nominal_bytes = prod(chunks) * dtype.itemsize
                if nominal_bytes > budget.max_logical_bytes or nominal_bytes > budget.max_chunk_bytes:
                    raise UnsupportedCase("decoded chunk exceeds the 8 MiB bound")
                grid = tuple((length + width - 1) // width for length, width in zip(shape, chunks))
                if prod(grid) > budget.max_grid:
                    raise UnsupportedCase("chunk grid exceeds the sparse validity map bound")
                count = int(selected.id.get_num_chunks())
                if count > budget.max_chunks:
                    raise UnsupportedCase("allocated chunks exceed the large export record quota")
                seen: set[tuple[int, ...]] = set()
                for index in range(count):
                    _deadline(deadline)
                    info = selected.id.get_chunk_info(index)
                    origin = tuple(int(value) for value in info.chunk_offset)
                    address, length, mask = chunk_address(selected, info.byte_offset), int(info.size), int(info.filter_mask)
                    if (len(origin) != rank or any(value < 0 or value >= axis or value % width
                                                  for value, axis, width in zip(origin, shape, chunks))
                            or origin in seen or address < 0 or length <= 0
                            or length > 2 * budget.max_chunk_bytes or address > size or length > size - address
                            or mask & ~((1 << filter_count) - 1)):
                        raise UnsupportedCase("chunk index has an invalid coordinate, range, size or filter mask")
                    by_coord = selected.id.get_chunk_info_by_coord(origin)
                    if (by_coord.byte_offset is None
                            or tuple(by_coord.chunk_offset) != origin
                            or chunk_address(selected, by_coord.byte_offset) != address
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
                ownership_ranges = [(address, address + length, origin)
                                    for origin, address, length, _ in records]
            elif layout == h5py.h5d.CONTIGUOUS:
                if filters or int(selected.id.get_storage_size()) != logical_bytes:
                    raise UnsupportedCase("contiguous values are not fully allocated")
                address = selected.id.get_offset()
                if address is None or address < 0 or address > size or logical_bytes > size - address:
                    raise UnsupportedCase("contiguous source bytes are missing")
                contiguous_address = int(address)
                grid = ()
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
                (("origin", "<u8") if rank == 1 else ("origin", "<u8", (rank,))),
                ("source_offset", "<u8"), ("stored_bytes", "<u8"),
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
                            validity = meta.create_dataset("validity", shape=grid, dtype="u1",
                                                           fillvalue=0,
                                                           chunks=(1,) * (rank - 1) + (min(grid[-1], 4096),))
                            iterator = ((tuple(slice(start, min(start + width, axis))
                                               for start, width, axis in zip(origin, chunks, shape)),
                                         origin, address, length, mask)
                                        for origin, address, length, mask in records)
                        else:
                            iterator = ((selection, (), -1, 0, 0)
                                        for selection in _stream_blocks(shape, dtype.itemsize,
                                                                         budget.block_bytes))
                        count = 0
                        successful = []
                        failed_chunks = []
                        accepted = 0
                        for selection, origin, address, length, mask in iterator:
                            _deadline(deadline)
                            try:
                                values = read_fixed_block(selected, selection)
                            except (OSError, RuntimeError, ValueError) as exc:
                                if layout != h5py.h5d.CHUNKED:
                                    raise RecoveryError("contiguous native read failed") from exc
                                validity[tuple(start // width for start, width in zip(origin, chunks))] = 6
                                evidence[count] = (origin[0] if rank == 1 else origin, address, length, mask,
                                                   _hash_range(raw, address, length, deadline=deadline,
                                                               block_bytes=budget.block_bytes), b"")
                                failed_chunks.append({"origin": list(origin), "reason": str(exc)[:300]})
                                count += 1
                                continue
                            if values.nbytes > budget.max_chunk_bytes:
                                raise RecoveryError("native read exceeded the fixed-record block budget")
                            write_fixed_block(target, selection, values)
                            encoded = values.tobytes(order="C")
                            source_values.update(encoded)
                            if layout == h5py.h5d.CHUNKED:
                                successful.append(selection)
                            accepted += values.size
                            if layout == h5py.h5d.CHUNKED:
                                validity[tuple(start // width for start, width in zip(origin, chunks))] = 1
                                evidence[count] = (origin[0] if rank == 1 else origin, address, length, mask,
                                                   _hash_range(raw, address, length, deadline=deadline,
                                                               block_bytes=budget.block_bytes),
                                                   hashlib.sha256(encoded).hexdigest())
                            count += 1
                            result.flush()
                            if (out_temp.stat().st_size > budget.max_output_bytes
                                    or shutil.disk_usage(out_temp.parent).free < budget.disk_reserve_bytes):
                                raise UnsupportedCase("output exceeded its size or free-disk quota")
                        attributes, omitted_attributes = _safe_export_attributes(selected)
                        copied_attributes = []
                        for name, value in attributes:
                            target.attrs[name] = value
                            old = selected.attrs.get_id(name).get_type()
                            new = target.attrs.get_id(name).get_type()
                            if (not old.equal(new) or np.asarray(value).tobytes() !=
                                    np.asarray(target.attrs[name]).tobytes()):
                                del target.attrs[name]
                                omitted_attributes += (name,)
                            else:
                                copied_attributes.append(name)
                        result.flush()
                    output_values = hashlib.sha256()
                    with h5py.File(out_temp, "r") as result:
                        target = result[dataset_path]
                        checked_selections = (successful if layout == h5py.h5d.CHUNKED else
                                              _stream_blocks(shape, dtype.itemsize, budget.block_bytes))
                        for selection in checked_selections:
                            _deadline(deadline)
                            output_values.update(read_fixed_block(target, selection).tobytes(order="C"))
                    if output_values.digest() != source_values.digest():
                        raise RecoveryError("output fixed records differ from the source snapshot")
                    report = {
                        "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                        "mode": "large_native_readable_export",
                        "operation": "large_native_readable_export",
                        "outcome": "partial" if accepted < elements else "complete",
                        "source": {"path": str(source), "size_bytes": size,
                                   "sha256_before": digest, "sha256_after": digest,
                                   "snapshot_physical_data_bytes_copied": copied},
                        "dataset": {"path": dataset_path, "shape": list(selected.shape),
                                    "dtype": dtype.str, "maxshape": list(selected.maxshape),
                                    "chunks": list(selected.chunks) if selected.chunks else None,
                                    "layout": "chunked" if records or layout == h5py.h5d.CHUNKED else "contiguous",
                                    "filters_in_order": list(filters),
                                    "file_type_encoding_hex": selected.id.get_type().encode().hex(),
                                    "attributes_copied": copied_attributes,
                                    "attributes_omitted": list(omitted_attributes),
                                    "source_object_header_address": int(h5py.h5o.get_info(selected.id).addr),
                                    "source_contiguous_byte_range": (
                                        {"start": contiguous_address, "end_exclusive": contiguous_address + logical_bytes}
                                        if layout == h5py.h5d.CONTIGUOUS else None)},
                        "allocated_chunks_checked": len(records), "accepted_elements": int(accepted),
                        "unknown_elements": int(elements - accepted),
                        "failed_chunks": failed_chunks,
                        "validity_map": "/_h5reclaim/validity" if layout == h5py.h5d.CHUNKED else None,
                        "physical_evidence": "/_h5reclaim/physical_evidence",
                        "physical_evidence_scope": "native-enumerated physical chunk ranges and current raw/decoded SHA-256",
                        "validity_codes": {"0": "unallocated or unknown; ignore fill values",
                                           "1": "native-allocated, decoded and read back bitwise equal",
                                           "6": "allocated chunk could not be decoded; ignore fill values"}
                                          if layout == h5py.h5d.CHUNKED else None,
                        "ownership_inventory": owners,
                        "native_value_sha256": source_values.hexdigest(),
                        "output_path": str(published_output or output),
                        "value_evidence": "Native-allocated current fixed records, independently read back byte for byte.",
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
