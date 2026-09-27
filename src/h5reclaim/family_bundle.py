"""Bounded native-readable export from an explicitly pinned HDF5 Family set.

Family files are one logical address space spread over several physical files.
Each supplied member is copied and hashed before the HDF5 library sees it.
Missing members and byte ranges beyond a member's physical end are unknown,
never measurements. This is a current-value export, not index reconstruction.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import tempfile
from math import prod
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .metadata import UnsupportedCase
from .readable_export import (
    MAX_BLOCK_BYTES, MAX_CHUNKS, MAX_DATA_BYTES, NATIVE_FILTERS,
    _blocks, _create_matching_dataset, _safe_fixed_type, _selected_dataset,
)
from .recovery import RecoveryError, VERSION, _verify_source, source_snapshot


MAX_MEMBERS = 64
MAX_BUNDLE_BYTES = 4 * 1024 * 1024 * 1024


def _manifest(value: str | Path | dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    if isinstance(value, (str, Path)):
        with Path(value).open("rb") as handle:
            raw = handle.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise RecoveryError("family manifest exceeds 64 KiB")
        value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != {"schema_version", "member_size", "members"}:
        raise RecoveryError("family manifest needs schema_version, member_size, and members")
    size, members = value["member_size"], value["members"]
    if value["schema_version"] != 1 or type(size) is not int or not 512 <= size <= MAX_BUNDLE_BYTES:
        raise RecoveryError("invalid family manifest version or member_size")
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_MEMBERS:
        raise RecoveryError("family manifest needs 1 through 64 members")
    paths: set[Path] = set()
    for index, member in enumerate(members):
        if not isinstance(member, dict) or set(member) != {"index", "path", "sha256"} or member["index"] != index or type(member["index"]) is not int:
            raise RecoveryError("family members must have contiguous zero-based indices")
        path, digest = member["path"], member["sha256"]
        if not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path or not Path(path).is_absolute():
            raise RecoveryError("family member paths must be explicit bounded absolute paths")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RecoveryError("family member hash must be lowercase SHA-256")
        canonical = Path(path).resolve(strict=True)
        if canonical in paths:
            raise RecoveryError("one physical file was assigned multiple family indices")
        paths.add(canonical)
    return size, members


def _extent(address: int, length: int, member_size: int, physical_sizes: list[int]) -> list[dict[str, int]]:
    if address < 0 or length < 0 or address + length > len(physical_sizes) * member_size:
        raise UnsupportedCase("family payload address is outside the supplied logical address space")
    parts = []
    cursor, remaining = address, length
    while remaining:
        index, offset = divmod(cursor, member_size)
        count = min(remaining, member_size - offset)
        if offset + count > physical_sizes[index]:
            raise UnsupportedCase("family payload crosses missing physical bytes; native fill is not evidence")
        parts.append({"member_index": index, "physical_offset": offset, "length": count})
        cursor += count
        remaining -= count
    return parts


def export_family(
    member_manifest: str | Path | dict[str, Any], dataset_path: str,
    output: str | Path, report_path: str | Path,
) -> dict[str, Any]:
    """Copy a bounded selected dataset from a complete, explicitly pinned Family bundle."""
    member_size, members = _manifest(member_manifest)
    output, report_path = Path(output), Path(report_path)
    sources = [Path(item["path"]) for item in members]
    if not output.parent.is_dir() or not report_path.parent.is_dir():
        raise RecoveryError("output and report directories must exist")
    if output.exists() or output.is_symlink() or report_path.exists() or report_path.is_symlink():
        raise RecoveryError("output and report must be new paths")
    if output.resolve(strict=False) == report_path.resolve(strict=False) or any(
        target.resolve(strict=False) == source.resolve(strict=True)
        for target in (output, report_path) for source in sources
    ):
        raise RecoveryError("destinations must differ from all evidence files and each other")
    with tempfile.TemporaryDirectory(prefix="h5reclaim-family-") as directory:
        root = Path(directory)
        template = str(root / "member%03d.h5")
        captures = []
        sizes = []
        total = 0
        for index, (entry, source) in enumerate(zip(members, sources)):
            with source_snapshot(source, max_source_bytes=MAX_BUNDLE_BYTES - total) as (snapshot, digest, identity, length):
                if digest != entry["sha256"]:
                    raise RecoveryError(f"family member {index} does not match its pinned hash")
                if not 0 < length <= member_size or (index < len(members) - 1 and length != member_size):
                    raise UnsupportedCase("non-final Family members must be physically complete")
                shutil.copyfile(snapshot, root / f"member{index:03d}.h5")
                captures.append((source, digest, identity))
                sizes.append(length)
                total += length
                if total > MAX_BUNDLE_BYTES:
                    raise UnsupportedCase("family bundle exceeds the 4 GiB limit")
        with h5py.File(template, "r", driver="family", memb_size=member_size) as handle:
            selected = _selected_dataset(handle, dataset_path)
            creation = selected.id.get_create_plist()
            if selected.is_virtual or creation.get_external_count():
                raise UnsupportedCase("nested virtual or external dataset dependencies need their own evidence route")
            _safe_fixed_type(selected.id.get_type(), selected.dtype)
            shape = tuple(int(value) for value in selected.shape)
            if not 1 <= len(shape) <= 4 or any(value <= 0 for value in shape):
                raise UnsupportedCase("Family export requires a nonempty rank-one through rank-four dataset")
            logical_size = prod(shape) * selected.dtype.itemsize
            if logical_size > MAX_DATA_BYTES:
                raise UnsupportedCase("selected dataset exceeds the 512 MiB logical limit")
            filters = [creation.get_filter(i)[0] for i in range(creation.get_nfilters())]
            if len(filters) > 8 or any(item not in NATIVE_FILTERS for item in filters):
                raise UnsupportedCase("selected dataset requires an unapproved filter decoder")
            layout = creation.get_layout()
            known = []
            unknown = []
            selections = []
            if layout == h5py.h5d.CHUNKED:
                chunks = selected.chunks
                if chunks is None or prod(chunks) * selected.dtype.itemsize > MAX_BLOCK_BYTES:
                    raise UnsupportedCase("Family chunk exceeds bounded read size")
                origins = itertools.product(*(range(0, n, width) for n, width in zip(shape, chunks)))
                for origin in origins:
                    if len(known) + len(unknown) >= MAX_CHUNKS:
                        raise UnsupportedCase("Family dataset exceeds the 8192-chunk limit")
                    info = selected.id.get_chunk_info_by_coord(origin)
                    if info.byte_offset is None or not info.size:
                        unknown.append(origin)
                        continue
                    if tuple(info.chunk_offset) != origin or info.filter_mask & ~((1 << len(filters)) - 1):
                        raise RecoveryError("Family chunk index contradicts requested coordinate or filter pipeline")
                    parts = _extent(int(info.byte_offset), int(info.size), member_size, sizes)
                    known.append({"coordinate": list(origin), "logical_address": int(info.byte_offset),
                                  "stored_bytes": int(info.size), "filter_mask": int(info.filter_mask),
                                  "physical_parts": parts})
                    selections.append(tuple(slice(start, min(start + step, extent))
                                            for start, step, extent in zip(origin, chunks, shape)))
                ranges = sorted((item["logical_address"], item["logical_address"] + item["stored_bytes"])
                                for item in known)
                if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
                    raise RecoveryError("Family chunks claim overlapping physical bytes")
                if selected.id.get_num_chunks() != len(known):
                    raise RecoveryError("Family reachable chunk count disagrees with HDF5 metadata")
            elif layout in (h5py.h5d.CONTIGUOUS, h5py.h5d.COMPACT):
                if filters or selected.id.get_storage_size() != logical_size:
                    raise UnsupportedCase("Family nonchunked storage is not fully allocated")
                if layout == h5py.h5d.CONTIGUOUS:
                    address = selected.id.get_offset()
                    if address is None:
                        raise UnsupportedCase("Family contiguous address is unavailable")
                    known.append({"logical_address": int(address), "stored_bytes": logical_size,
                                  "physical_parts": _extent(int(address), logical_size, member_size, sizes)})
                selections = list(_blocks(shape, selected.dtype.itemsize))
            else:
                raise UnsupportedCase("unsupported Family dataset layout")
            for source, digest, identity in captures:
                _verify_source(source, identity, digest)
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=output.parent) as outdir:
                with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=report_path.parent) as repdir:
                    staged_output = Path(outdir) / "output.h5"
                    staged_report = Path(repdir) / "report.json"
                    source_hash = hashlib.sha256()
                    with h5py.File(staged_output, "x") as target:
                        result = _create_matching_dataset(target, selected, dataset_path)
                        if layout == h5py.h5d.CHUNKED:
                            validity = np.zeros(tuple((n + w - 1) // w for n, w in zip(shape, selected.chunks)), dtype="u1")
                            for item in known:
                                validity[tuple(a // w for a, w in zip(item["coordinate"], selected.chunks))] = 1
                            target.create_dataset("/_h5reclaim/validity", data=validity, dtype="u1")
                        for selection in selections:
                            block = np.asarray(selected[selection])
                            if block.nbytes > MAX_BLOCK_BYTES or block.dtype != selected.dtype:
                                raise RecoveryError("Family source returned an unexpected bounded block")
                            source_hash.update(block.tobytes(order="C"))
                            result[selection] = block
                        target.flush()
                    output_hash = hashlib.sha256()
                    with h5py.File(staged_output, "r") as target:
                        for selection in selections:
                            output_hash.update(np.asarray(target[dataset_path][selection]).tobytes(order="C"))
                    if output_hash.digest() != source_hash.digest():
                        raise RecoveryError("Family output readback differs from accepted source values")
                    report = {"schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                              "mode": "family_native_export", "outcome": "partial" if unknown else "complete",
                              "dataset": {"path": dataset_path, "shape": list(shape), "dtype": selected.dtype.str,
                                          "layout": int(layout), "chunks": list(selected.chunks) if selected.chunks else None},
                              "members": [{"index": i, "path": str(source), "sha256": digest, "size_bytes": sizes[i]}
                                          for i, (source, digest, _) in enumerate(captures)],
                              "member_size": member_size, "accepted_ranges": known,
                              "unknown_chunk_origins": [list(item) for item in unknown],
                              "accepted_elements": sum(prod(s.stop - s.start for s in selection) for selection in selections),
                              "validity_map": "/_h5reclaim/validity" if layout == h5py.h5d.CHUNKED else None,
                              "note": "Native Family read of currently available values; hashes pin supplied members but do not prove earlier historical measurements."}
                    staged_report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                    for source, digest, identity in captures:
                        _verify_source(source, identity, digest)
                    if output.exists() or output.is_symlink() or report_path.exists() or report_path.is_symlink():
                        raise RecoveryError("destination appeared during Family export")
                    os.link(staged_output, output)
                    try:
                        os.link(staged_report, report_path)
                    except Exception:
                        output.unlink(missing_ok=True)
                        raise
                    return report
