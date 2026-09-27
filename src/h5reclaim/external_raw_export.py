"""Bounded, evidence-based export of explicitly supplied external raw storage.

HDF5 concatenates the selected dataset's external segments into one logical
byte stream. A native read can return zeros for bytes beyond a physical source
EOF. This route instead accepts an element only if *all* of its bytes exist in
SHA-256-pinned, private snapshots. It never resolves a declared filename as a
path, reads a VDS, or treats a historical measurement as verified by a hash
computed after the fact.

Format/API references:
https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
https://support.hdfgroup.org/documentation/hdf5/latest/_h5_d__u_g.html
"""

from __future__ import annotations

import itertools
import json
import os
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from math import prod
from pathlib import Path
from typing import Any, Iterator, Mapping

import h5py
import numpy as np

from .dependency_routes import (
    DependencyError, MAX_BUNDLE_BYTES, inspect_dependencies,
    load_dependency_manifest, validate_dependency_manifest,
)
from .metadata import UnsupportedCase
from .readable_export import (
    MAX_BLOCK_BYTES, MAX_DATA_BYTES, MAX_RANK, _safe_export_attributes,
    _safe_fixed_type, _selected_dataset, _range_sha256,
)
from .recovery import (
    RecoveryError, VERSION, _validate_paths, _verify_source, sha256_file,
    source_snapshot,
)


MAX_ELEMENTS = 1_048_576
MAX_REPORT_BYTES = 8 * 1024 * 1024
UNLIMITED_EXTERNAL_SIZE = (1 << 64) - 1


@dataclass(frozen=True)
class _Segment:
    declared_name: str
    logical_start: int
    logical_end: int
    raw_offset: int
    declared_size: int
    snapshot: Path | None
    snapshot_size: int
    sha256: str | None


def _manifest(document: str | Path | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(document, (str, Path)):
        return load_dependency_manifest(document)
    if not isinstance(document, Mapping):
        raise DependencyError("supply a related-file manifest or its path")
    return document


def _create_local_dataset(
    target: h5py.File, selected: h5py.Dataset, dataset_path: str,
) -> h5py.Dataset:
    """Retain the selected datatype and dataspace, replacing external storage.

    The output's local allocation and fill rules necessarily differ. Unknown
    values are guarded by a separate element validity array, never by a fill value.
    """
    parent = target["/"]
    parts = dataset_path[1:].split("/")
    for part in parts[:-1]:
        parent = parent.create_group(part)
    dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
    if any(limit is None or limit != current for limit, current in zip(selected.maxshape, selected.shape)):
        chunk_shape = [1] * len(selected.shape)
        allowance = max(1, MAX_BLOCK_BYTES // selected.dtype.itemsize)
        for index in range(len(chunk_shape) - 1, -1, -1):
            chunk_shape[index] = min(selected.shape[index], 64, allowance)
            allowance = max(1, allowance // chunk_shape[index])
        dcpl.set_chunk(tuple(chunk_shape))
    created = h5py.h5d.create(
        parent.id, parts[-1].encode("utf-8"), selected.id.get_type().copy(),
        selected.id.get_space().copy(), dcpl=dcpl,
    )
    created.close()
    result = target[dataset_path]
    if (
        not result.id.get_type().equal(selected.id.get_type())
        or result.shape != selected.shape or result.maxshape != selected.maxshape
        or result.id.get_create_plist().get_external_count()
    ):
        raise RecoveryError("derived dataset datatype or dataspace differs from the selected source")
    return result


def _segments_from_inventory(
    dependencies: list[dict[str, Any]], snapshots: Mapping[str, tuple[Path, int, str]],
    logical_bytes: int,
) -> list[_Segment]:
    segments: list[_Segment] = []
    cursor = 0
    for index, entry in enumerate(dependencies):
        name = entry["file_name"]
        offset, size = entry["raw_offset"], entry["raw_size"]
        if (
            entry["kind"] != "external_raw_storage"
            or not entry["declared_name_exact"]
            or type(offset) is not int or type(size) is not int
            or not 0 <= offset < 1 << 64 or not 0 <= size <= UNLIMITED_EXTERNAL_SIZE
        ):
            raise UnsupportedCase("external segment has an unsupported name, offset, or size")
        declared_size = size
        if size == UNLIMITED_EXTERNAL_SIZE:
            if index != len(dependencies) - 1:
                raise UnsupportedCase("an unlimited external segment must be last")
            size = max(0, logical_bytes - cursor)
        if size == 0 and cursor < logical_bytes:
            raise UnsupportedCase("external segment has zero declared capacity")
        end = min(logical_bytes, cursor + size)
        image, source_size, digest = snapshots.get(name, (None, 0, None))
        if end > cursor:
            segments.append(_Segment(name, cursor, end, offset, declared_size, image, source_size, digest))
        cursor += size
        if cursor >= logical_bytes:
            break
    return segments


def _reject_physical_aliases(segments: list[_Segment]) -> None:
    """Refuse two logical extents backed by overlapping bytes of one file."""
    ranges: list[tuple[str, int, int]] = []
    for segment in segments:
        if segment.snapshot is None:
            continue
        # Snapshots are intentionally distinct, so compare the common
        # declared name here and original path aliases separately below.
        ranges.append((segment.declared_name, segment.raw_offset,
                       segment.raw_offset + segment.logical_end - segment.logical_start))
    by_name: dict[str, list[tuple[int, int]]] = {}
    for name, start, end in ranges:
        by_name.setdefault(name, []).append((start, end))
    for source_ranges in by_name.values():
        source_ranges.sort()
        if any(a_end > b_start for (_, a_end), (b_start, _) in zip(source_ranges, source_ranges[1:])):
            raise UnsupportedCase("external segments assign overlapping source bytes to different coordinates")


def _block_bytes(
    segments: list[_Segment], handles: Mapping[str, Any], start: int, count: int,
    itemsize: int,
) -> tuple[bytes, np.ndarray]:
    """Map one row-major block, preserving only completely present elements."""
    end = start + count * itemsize
    data = bytearray(end - start)
    available: list[tuple[int, int]] = []
    for segment in segments:
        lower = max(start, segment.logical_start)
        upper = min(end, segment.logical_end)
        if upper <= lower or segment.snapshot is None:
            continue
        physical = segment.raw_offset + lower - segment.logical_start
        present_end = min(upper, lower + max(0, segment.snapshot_size - physical))
        if present_end <= lower:
            continue
        handle = handles[segment.declared_name]
        handle.seek(physical)
        raw = handle.read(present_end - lower)
        if len(raw) != present_end - lower:
            raise RecoveryError("private external snapshot changed during a bounded read")
        data[lower - start:present_end - start] = raw
        available.append((lower - start, present_end - start))
    validity = np.zeros(count, dtype="u1")
    position = 0
    for index in range(count):
        first, last = index * itemsize, (index + 1) * itemsize
        covered = first
        while position < len(available) and available[position][1] <= covered:
            position += 1
        scan = position
        while scan < len(available) and available[scan][0] <= covered:
            covered = max(covered, available[scan][1])
            if covered >= last:
                break
            scan += 1
        if covered >= last:
            validity[index] = 1
        else:
            data[first:last] = bytes(itemsize)
    return bytes(data), validity


def _row_blocks(
    shape: tuple[int, ...], itemsize: int,
) -> Iterator[tuple[tuple[int | slice, ...], int, int]]:
    """Yield bounded C-order row selections and their logical byte offset."""
    row_length = shape[-1]
    width = max(1, MAX_BLOCK_BYTES // itemsize)
    for row_number, prefix in enumerate(itertools.product(*(range(length) for length in shape[:-1]))):
        for col in range(0, row_length, width):
            count = min(width, row_length - col)
            selection = prefix + (slice(col, col + count),)
            yield selection, (row_number * row_length + col) * itemsize, count


def _segment_evidence(part: _Segment) -> dict[str, Any]:
    """Document the physical bytes actually available for this logical span."""
    length = part.logical_end - part.logical_start
    available = min(length, max(0, part.snapshot_size - part.raw_offset)) if part.snapshot else 0
    return {
        "declared_name": part.declared_name,
        "logical_byte_range": [part.logical_start, part.logical_end],
        "present_logical_byte_range": [part.logical_start, part.logical_start + available],
        "physical_byte_range": [part.raw_offset, part.raw_offset + available] if available else None,
        "declared_size": part.declared_size,
        "snapshot_size": part.snapshot_size,
        "file_sha256": part.sha256,
        "present_raw_sha256": _range_sha256(part.snapshot, part.raw_offset, available)
        if available and part.snapshot else None,
        "status": "pinned_snapshot" if part.snapshot else "unavailable",
    }


def export_external_raw(
    source: str | Path, dataset_path: str,
    related_files: str | Path | Mapping[str, Any],
    output: str | Path, report_path: str | Path,
    *, published_output: str | Path | None = None,
) -> dict[str, Any]:
    """Export present values from one external-raw dataset and its pinned files.

    Source and related files are separately copied into private, bounded images.
    Manifest hashes identify *current* supplied files; they do not prove these
    files hold the original scientific readings. Partial elements, absent
    files, and bytes beyond an external source's EOF remain unknown.
    """
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    manifest = _manifest(related_files)
    if isinstance(related_files, (str, Path)):
        forbidden = Path(related_files).resolve(strict=True)
        if forbidden in (output.resolve(strict=False), report_path.resolve(strict=False)):
            raise RecoveryError("destination aliases the related-file manifest")
    with ExitStack() as stack:
        image, source_hash, source_identity, source_size = stack.enter_context(source_snapshot(source))
        inventory = inspect_dependencies(image, dataset_path)
        if inventory["outcome"] != "complete" or inventory["omitted"]:
            raise UnsupportedCase("selected external dependency inventory is incomplete or unreadable")
        dependencies = inventory["dependencies"]
        if not dependencies or any(item["kind"] != "external_raw_storage" for item in dependencies):
            raise UnsupportedCase("selected dataset must use external raw storage only")
        validation = validate_dependency_manifest(inventory, manifest)
        if validation["unused_manifest_names"]:
            raise DependencyError("related-file manifest contains undeclared names")
        by_name = {item["declared_name"]: item for item in manifest["files"]}
        for item in by_name.values():
            if Path(item["path"]).resolve(strict=False) in (
                output.resolve(strict=False), report_path.resolve(strict=False)
            ):
                raise RecoveryError("destination aliases an external raw source")
        for item in validation["references"]:
            if item["status"] in ("hash_mismatch", "invalid_declared_raw_range",
                                  "name_not_exactly_representable", "bundle_size_limit"):
                raise DependencyError(f"related-file manifest conflicts with {item['declared_name']}: {item['status']}")

        with h5py.File(image, "r") as main:
            selected = _selected_dataset(main, dataset_path)
            datatype, dtype = selected.id.get_type(), selected.dtype
            _safe_fixed_type(datatype, dtype)
            shape = tuple(int(value) for value in selected.shape)
            if not 1 <= len(shape) <= MAX_RANK or any(value <= 0 for value in shape):
                raise UnsupportedCase("expected a nonempty dataset of rank one through four")
            if prod(shape) > MAX_ELEMENTS or prod(shape) * dtype.itemsize > MAX_DATA_BYTES:
                raise UnsupportedCase("external dataset exceeds element or 512 MiB logical-byte limit")
            if dtype.itemsize > MAX_BLOCK_BYTES:
                raise UnsupportedCase("external dataset element exceeds the 1 MiB read limit")
            if selected.chunks is not None or selected.is_virtual:
                raise UnsupportedCase("external raw export requires a contiguous, nonvirtual dataset")
            if selected.id.get_create_plist().get_nfilters():
                raise UnsupportedCase("external raw export does not support filters")

            logical_bytes = prod(shape) * dtype.itemsize
            snapshots: dict[str, tuple[Path, int, str]] = {}
            originals: list[tuple[Path, tuple[int, int, int, int, int], str]] = []
            remaining = MAX_BUNDLE_BYTES - source_size
            for item in validation["references"]:
                if item["status"] == "not_supplied" or item["status"] == "file_unavailable":
                    continue
                if item["status"] not in ("hash_matched", "declared_raw_range_missing",
                                           "unlimited_raw_range_unverified"):
                    raise DependencyError(f"external source cannot be verified: {item['status']}")
                name = item["declared_name"]
                if name in snapshots:
                    continue
                path = Path(by_name[name]["path"])
                related_image, digest, identity, size = stack.enter_context(
                    source_snapshot(path, max_source_bytes=max(1, remaining))
                )
                if digest != by_name[name]["sha256"]:
                    raise DependencyError(f"external source hash changed during snapshot: {name}")
                remaining -= size
                if remaining < 0:
                    raise UnsupportedCase("external bundle exceeds the 4 GiB snapshot limit")
                snapshots[name] = related_image, size, digest
                originals.append((path, identity, digest))

            segments = _segments_from_inventory(dependencies, snapshots, logical_bytes)
            _reject_physical_aliases(segments)
            # Two differently named manifest entries may resolve to one file.
            # Their logical segments cannot reuse its physical bytes either.
            for i, first in enumerate(segments):
                for second in segments[i + 1:]:
                    if first.snapshot is None or second.snapshot is None or first.declared_name == second.declared_name:
                        continue
                    a = Path(by_name[first.declared_name]["path"])
                    b = Path(by_name[second.declared_name]["path"])
                    if a.samefile(b):
                        left = first.raw_offset, first.raw_offset + first.logical_end - first.logical_start
                        right = second.raw_offset, second.raw_offset + second.logical_end - second.logical_start
                        if max(left[0], right[0]) < min(left[1], right[1]):
                            raise UnsupportedCase("different external names claim the same physical source bytes")

            copied, omitted = _safe_export_attributes(selected)
            status = np.zeros(shape, dtype="u1")
            accepted = 0
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=output.parent) as out_dir:
                with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=report_path.parent) as rep_dir:
                    out_temp = Path(out_dir) / "output.h5"
                    report_temp = Path(rep_dir) / "report.json"
                    handles = {name: stack.enter_context(snapshot.open("rb"))
                               for name, (snapshot, _, _) in snapshots.items()}
                    with h5py.File(out_temp, "x") as target:
                        exported = _create_local_dataset(target, selected, dataset_path)
                        output_layout = "local_chunked" if exported.chunks else "local_contiguous"
                        for selection, logical_start, count in _row_blocks(shape, dtype.itemsize):
                            raw, valid = _block_bytes(segments, handles, logical_start, count, dtype.itemsize)
                            exported[selection] = np.frombuffer(raw, dtype=dtype)
                            status[selection] = valid
                            accepted += int(valid.sum())
                        copied_names = []
                        for name, value in copied:
                            try:
                                exported.attrs[name] = value
                                if exported.attrs.get_id(name).get_type().equal(selected.attrs.get_id(name).get_type()):
                                    copied_names.append(name)
                                else:
                                    del exported.attrs[name]
                                    omitted += (name,)
                            except (OSError, RuntimeError, ValueError, TypeError):
                                omitted += (name,)
                        meta = target.require_group("/_h5reclaim")
                        valid_ds = meta.create_dataset("validity", data=status, dtype="u1")
                        valid_ds.attrs["codes_json"] = json.dumps({"0": "unknown", "1": "all bytes present in pinned external snapshots"})
                        valid_ds.attrs["axis_meaning"] = "one status per selected dataset element"
                        target.flush()

                    # Verify the *accepted* values using a fresh HDF5 read. In
                    # particular, conversions or record padding may not retain
                    # raw bits, and cannot silently pass as exact export.
                    with h5py.File(out_temp, "r") as target:
                        exported = target[dataset_path]
                        for selection, logical_start, count in _row_blocks(shape, dtype.itemsize):
                            raw, valid = _block_bytes(segments, handles, logical_start, count, dtype.itemsize)
                            expected = np.frombuffer(raw, dtype=dtype)
                            got = np.asarray(exported[selection])
                            if (got.dtype != dtype or got.shape != expected.shape
                                    or got[valid != 0].tobytes() != expected[valid != 0].tobytes()):
                                raise RecoveryError("derived output changed accepted external raw value bits")
                        if not np.array_equal(target["/_h5reclaim/validity"][:], status):
                            raise RecoveryError("derived output validity map differs from verified source presence")
                    if accepted == 0:
                        raise UnsupportedCase("no complete, pinned external raw elements are available")

                    report: dict[str, Any] = {
                        "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                        "mode": "external_raw_export",
                        "outcome": "complete" if accepted == prod(shape) else "partial",
                        "source": {"path": str(source), "sha256_before": source_hash,
                                   "sha256_after": source_hash, "size_bytes": source_size},
                        "dataset": {"path": dataset_path, "shape": list(shape),
                                    "dtype": dtype.descr if dtype.fields else dtype.str,
                                    "maxshape": list(selected.maxshape), "logical_bytes": logical_bytes,
                                    "source_object_header_address": int(h5py.h5o.get_info(selected.id).addr),
                                    "source_layout": "external_contiguous",
                                    "output_layout": output_layout,
                                    "attributes_copied": copied_names,
                                    "attributes_omitted": list(omitted)},
                        "external_segments": [_segment_evidence(part) for part in segments],
                        "dependency_validation": validation,
                        "accepted_elements": accepted, "unknown_elements": prod(shape) - accepted,
                        "validity_map": "/_h5reclaim/validity",
                        "validity_codes": {"0": "unknown; ignore output value", "1": "complete element in pinned source"},
                        "output_path": str(published_output or output),
                        "historical_values_verified": False,
                        "verification": "complete external elements copied from pinned private snapshots and bitwise read back",
                        "limits": "A hash of current files is not evidence of historical authenticity. Missing source bytes, including portions beyond physical EOF, are unknown. The output changes external storage to local storage and omits other objects, links, dimension scales, and some attributes.",
                    }
                    report_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
                    if len(report_text.encode("utf-8")) > MAX_REPORT_BYTES:
                        raise UnsupportedCase("external evidence report exceeds the 8 MiB limit")
                    with h5py.File(out_temp, "r+") as target:
                        meta = target["/_h5reclaim"]
                        meta.create_dataset("report_json", data=report_text,
                                            dtype=h5py.string_dtype(encoding="utf-8"))
                        meta.attrs["source_sha256"] = source_hash
                        meta.attrs["report_schema_version"] = 1
                    report_temp.write_text(report_text, encoding="utf-8")
                    if sha256_file(image) != source_hash:
                        raise RecoveryError("private HDF5 source snapshot changed during export")
                    _verify_source(source, source_identity, source_hash)
                    for path, identity, digest in originals:
                        _verify_source(path, identity, digest)
                    for snapshot, _, digest in snapshots.values():
                        if sha256_file(snapshot) != digest:
                            raise RecoveryError("private related-file snapshot changed during export")
                    _validate_paths(source, output, report_path)
                    published = False
                    try:
                        os.link(out_temp, output)
                        published = True
                        os.link(report_temp, report_path)
                    except OSError as exc:
                        if published:
                            output.unlink(missing_ok=True)
                        raise RecoveryError(f"external raw output publication failed: {exc}") from exc
                    return report
