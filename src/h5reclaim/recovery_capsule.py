"""Prospective selected-dataset schema and physical-range recovery capsule.

The operator retains the resulting ZIP and its SHA-256 independently before
damage. A later restore needs neither the damaged HDF5 root nor its indices:
the capsule records an exact HDF5 dataset type, space, creation property list,
selected attributes, and coordinate-specific physical byte ranges and hashes.
Only unchanged bytes are published as measurements. A capsule is an assertion
about the captured source, not a historical proof or a replacement for it.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import shutil
import tempfile
import zipfile
from math import prod
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .metadata import UnsupportedCase
from .output_annotations import add_output_annotations
from .ownership_inventory import inventory_other_allocations, reject_sibling_overlap
from .readable_export import (
    MAX_BLOCK_BYTES, MAX_DATA_BYTES, MAX_STORED_CHUNK_BYTES, _check_competing_owners,
    _check_storage, _safe_fixed_type, _selected_dataset,
)
from .recovery import (
    MAX_SOURCE_BYTES, RecoveryError, VERSION, _identity, _validate_paths,
    _verify_source, sha256_file, source_snapshot,
)


MAX_CAPSULE_BYTES = 48 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_TEMPLATE_BYTES = 16 * 1024 * 1024
MAX_CHUNKS = 8192
MAX_BLOCK_HASHES = 262144
MAX_ELEMENT_VALIDITY = 16 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024
BLOCK_TARGET_BYTES = 4096
_MANIFEST = "manifest.json"
_TEMPLATE = "schema.h5"


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise RecoveryError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError(f"duplicate capsule key: {key}")
        result[key] = value
    return result


def _serialize(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def _block_size(width: int) -> int:
    return max(width, BLOCK_TARGET_BYTES // width * width)


def _blocks(data: bytes, width: int) -> list[str]:
    size = _block_size(width)
    return [hashlib.sha256(data[start:start + size]).hexdigest()
            for start in range(0, len(data), size)]


def _schema(dataset: h5py.Dataset) -> dict[str, Any]:
    creation = dataset.id.get_create_plist()
    return {
        "path": dataset.name,
        "shape": list(dataset.shape),
        "maxshape": list(dataset.maxshape),
        "chunks": list(dataset.chunks or ()),
        "dtype": dataset.dtype.str if dataset.dtype.fields is None else json.loads(json.dumps(dataset.dtype.descr)),
        "itemsize": dataset.dtype.itemsize,
        "filter_pipeline": [
            {"id": int(item[0]), "flags": int(item[1]), "values": list(item[2])}
            for item in (creation.get_filter(i) for i in range(creation.get_nfilters()))
        ],
        "attribute_names": sorted(dataset.attrs),
    }


def _copy_dataset_schema(source: h5py.Dataset, destination: Path) -> None:
    """Store a header-only HDF5 dataset; H5Dcreate retains H5T/H5S/DCPL."""
    with h5py.File(destination, "x") as target:
        components = source.name[1:].split("/")
        parent = target.require_group("/" + "/".join(components[:-1])) if len(components) > 1 else target["/"]
        created_id = h5py.h5d.create(
            parent.id, components[-1].encode("utf-8"), source.id.get_type(),
            source.id.get_space(), dcpl=source.id.get_create_plist(),
        )
        created = h5py.Dataset(created_id)
        for name in source.attrs:
            original_id = source.attrs.get_id(name)
            if original_id.get_storage_size() > 4096:
                raise UnsupportedCase("selected dataset has an attribute exceeding the capsule limit")
            original_type = original_id.get_type()
            value = np.asarray(source.attrs[name])
            if value.dtype.hasobject or value.nbytes > 4096:
                raise UnsupportedCase("selected dataset has an attribute requiring variable-length storage")
            created.attrs.create(name, value, dtype=original_id.dtype)
            if (not created.attrs.get_id(name).get_type().equal(original_type)
                    or np.asarray(created.attrs[name]).tobytes() != value.tobytes()):
                raise UnsupportedCase("selected dataset attribute cannot be copied exactly")
        if created.id.get_num_chunks() != 0:
            raise UnsupportedCase("schema template preallocates raw chunks")
        if (_schema(source) != _schema(created)
                or not source.id.get_type().equal(created.id.get_type())):
            raise UnsupportedCase("schema template changed selected datatype or layout")
        target.flush()
    if destination.stat().st_size > MAX_TEMPLATE_BYTES:
        raise UnsupportedCase("schema template exceeds the 16 MiB capsule limit")


def _aliases(inputs: list[Path], targets: list[Path]) -> None:
    resolved = [path.resolve(strict=True) for path in inputs]
    for index, path in enumerate(resolved):
        if any(path.samefile(other) for other in resolved[index + 1:]):
            raise RecoveryError("capsule and damaged source must be separate files")
    for target in targets:
        if target.resolve(strict=False) in resolved:
            raise RecoveryError("capsule output aliases an input")


def capture_recovery_capsule(
    source: str | Path, dataset_path: str, destination: str | Path,
) -> dict[str, Any]:
    """Capture trusted pre-incident ranges without embedding measurement bytes."""
    source, destination = Path(source), Path(destination)
    if not destination.parent.is_dir() or destination.exists() or destination.is_symlink():
        raise RecoveryError("capsule destination parent must exist and destination must be new")
    _aliases([source], [destination])
    with source_snapshot(source) as (snapshot, source_sha, identity, source_size):
        with h5py.File(snapshot, "r") as handle:
            selected = _selected_dataset(handle, dataset_path)
            _safe_fixed_type(selected.id.get_type(), selected.dtype)
            if selected.chunks is None or selected.ndim < 1 or not all(selected.shape):
                raise UnsupportedCase("capsule requires a nonempty local chunked dataset")
            if prod(selected.shape) * selected.dtype.itemsize > MAX_DATA_BYTES:
                raise UnsupportedCase("selected dataset exceeds the 512 MiB logical limit")
            inspection = _check_storage(selected, source_size)
            if inspection.layout != "chunked" or prod(inspection.grid) > MAX_CHUNKS:
                raise UnsupportedCase("capsule supports at most 8192 selected chunks")
            _check_competing_owners(snapshot, selected, inspection)
            schema = _schema(selected)
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-capsule-", dir=destination.parent) as temporary:
                template = Path(temporary) / _TEMPLATE
                _copy_dataset_schema(selected, template)
                records: list[dict[str, Any]] = []
                total_blocks = 0
                with snapshot.open("rb") as stream:
                    for coordinate, offset, length, mask in inspection.records:
                        if length > MAX_STORED_CHUNK_BYTES:
                            raise UnsupportedCase("capsule chunk exceeds the stored-size limit")
                        stream.seek(offset)
                        raw = stream.read(length)
                        native_mask, native_raw = selected.id.read_direct_chunk(coordinate)
                        if len(raw) != length or raw != native_raw or native_mask != mask:
                            raise RecoveryError("native chunk disagrees with its captured physical extent")
                        if any(mask & (1 << position) and not item["flags"] & h5py.h5z.FLAG_OPTIONAL
                               for position, item in enumerate(schema["filter_pipeline"])):
                            raise RecoveryError("captured chunk skips a mandatory filter")
                        visible = tuple(slice(origin, min(origin + step, size))
                                        for origin, step, size in zip(coordinate, selected.chunks, selected.shape))
                        decoded = np.asarray(selected[visible])
                        if decoded.dtype != selected.dtype or decoded.nbytes > MAX_BLOCK_BYTES:
                            raise UnsupportedCase("native chunk read changed selected dtype or exceeded its limit")
                        hashes = _blocks(raw, selected.dtype.itemsize) if not schema["filter_pipeline"] else []
                        total_blocks += len(hashes)
                        if total_blocks > MAX_BLOCK_HASHES:
                            raise UnsupportedCase("capsule exceeds the block-digest limit")
                        records.append({
                            "coordinate": list(coordinate), "offset": offset, "length": length,
                            "filter_mask": mask, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                            "native_logical_sha256": hashlib.sha256(decoded.tobytes(order="C")).hexdigest(),
                            "block_sha256": hashes,
                        })
                manifest = {
                    "schema_version": 1, "kind": "prospective_physical_recovery_capsule",
                    "source_sha256": source_sha, "source_size_bytes": source_size,
                    "schema": schema, "template_sha256": sha256_file(template),
                    "block_bytes": _block_size(selected.dtype.itemsize),
                    "records": records,
                    "trust_note": (
                        "This separately retained capture records observed source bytes and layout. "
                        "It does not authenticate its capture date or prove scientific correctness."
                    ),
                }
                manifest_bytes = _serialize(manifest)
                if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                    raise UnsupportedCase("capsule manifest exceeds its 32 MiB limit")
                staged = Path(temporary) / "capsule.zip"
                with zipfile.ZipFile(staged, "x", compression=zipfile.ZIP_STORED) as archive:
                    archive.writestr(_MANIFEST, manifest_bytes, compress_type=zipfile.ZIP_STORED)
                    archive.write(template, _TEMPLATE, compress_type=zipfile.ZIP_STORED)
                if staged.stat().st_size > MAX_CAPSULE_BYTES:
                    raise UnsupportedCase("capsule archive exceeds its 48 MiB limit")
                _verify_source(source, identity, source_sha)
                if sha256_file(snapshot) != source_sha:
                    raise RecoveryError("source snapshot changed during capsule capture")
                if destination.exists() or destination.is_symlink():
                    raise RecoveryError("capsule destination already exists")
                os.link(staged, destination)
                return {**manifest, "archive_sha256": sha256_file(destination)}


def _load_capsule(path: Path, expected: str, template_path: Path) -> tuple[dict[str, Any], tuple[int, int, int, int, int]]:
    expected = _digest(expected, "capsule SHA-256")
    if not path.is_file() or path.stat().st_size > MAX_CAPSULE_BYTES:
        raise RecoveryError("capsule is missing or exceeds the size limit")
    identity = _identity(path.stat())
    if sha256_file(path) != expected:
        raise RecoveryError("capsule differs from the independently retained SHA-256")
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.namelist() != [_MANIFEST, _TEMPLATE]:
                raise RecoveryError("capsule needs exactly the manifest and schema template")
            entries = [archive.getinfo(item) for item in (_MANIFEST, _TEMPLATE)]
            if (any(entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits & 1 for entry in entries)
                    or entries[0].file_size > MAX_MANIFEST_BYTES
                    or entries[1].file_size > MAX_TEMPLATE_BYTES):
                raise RecoveryError("capsule entries exceed limits or use compression/encryption")
            manifest = json.loads(archive.read(_MANIFEST).decode("utf-8"), object_pairs_hook=_unique)
            with archive.open(_TEMPLATE) as source, template_path.open("xb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
    except RecoveryError:
        raise
    except (OSError, zipfile.BadZipFile, UnicodeError, ValueError, RuntimeError) as exc:
        raise RecoveryError(f"capsule cannot be read: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "kind", "source_sha256", "source_size_bytes", "schema",
        "template_sha256", "block_bytes", "records", "trust_note",
    } or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 \
            or manifest["kind"] != "prospective_physical_recovery_capsule":
        raise RecoveryError("capsule manifest schema is unsupported")
    _digest(manifest["source_sha256"], "captured source SHA-256")
    if (type(manifest["source_size_bytes"]) is not int
            or not 0 < manifest["source_size_bytes"] <= MAX_SOURCE_BYTES
            or sha256_file(template_path) != _digest(manifest["template_sha256"], "template SHA-256")):
        raise RecoveryError("capsule source size or template hash is invalid")
    if sha256_file(path) != expected or _identity(path.stat()) != identity:
        raise RecoveryError("capsule changed while it was read")
    return manifest, identity


def _validate_records(manifest: dict[str, Any], selected: h5py.Dataset) -> list[dict[str, Any]]:
    schema = manifest["schema"]
    if not isinstance(schema, dict) or _schema(selected) != schema:
        raise RecoveryError("capsule template contradicts its selected dataset schema")
    if (selected.id.get_num_chunks() != 0 or selected.chunks is None or selected.dtype.hasobject
            or selected.ndim < 1 or not all(selected.shape)):
        raise RecoveryError("capsule template is allocated or has an unsafe selected type")
    _safe_fixed_type(selected.id.get_type(), selected.dtype)
    if prod(selected.shape) * selected.dtype.itemsize > MAX_DATA_BYTES:
        raise RecoveryError("capsule selected dataset exceeds the logical size bound")
    width = selected.dtype.itemsize
    if manifest["block_bytes"] != _block_size(width):
        raise RecoveryError("capsule block size contradicts its dataset type")
    rows = manifest["records"]
    if not isinstance(rows, list) or len(rows) > MAX_CHUNKS:
        raise RecoveryError("capsule has an invalid record count")
    seen: set[tuple[int, ...]] = set()
    extents: list[tuple[int, int]] = []
    block_count = 0
    max_grid = prod((size + step - 1) // step for size, step in zip(selected.shape, selected.chunks))
    if max_grid > MAX_CHUNKS:
        raise RecoveryError("capsule chunk grid exceeds the limit")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "coordinate", "offset", "length", "filter_mask", "raw_sha256",
            "native_logical_sha256", "block_sha256",
        }:
            raise RecoveryError("capsule chunk record has invalid fields")
        coordinate = row["coordinate"]
        if (not isinstance(coordinate, list) or len(coordinate) != selected.ndim
                or any(type(x) is not int or x < 0 or x >= size or x % step
                       for x, size, step in zip(coordinate, selected.shape, selected.chunks))):
            raise RecoveryError("capsule chunk coordinate is invalid")
        key = tuple(coordinate)
        if key in seen:
            raise RecoveryError("capsule assigns a chunk coordinate twice")
        seen.add(key)
        offset, length, mask = row["offset"], row["length"], row["filter_mask"]
        if (type(offset) is not int or type(length) is not int or type(mask) is not int
                or offset < 0 or not 0 < length <= MAX_STORED_CHUNK_BYTES
                or length > manifest["source_size_bytes"] - offset
                or mask < 0 or mask >> len(schema["filter_pipeline"])):
            raise RecoveryError("capsule physical range or filter mask is invalid")
        if any(mask & (1 << position) and not item["flags"] & h5py.h5z.FLAG_OPTIONAL
               for position, item in enumerate(schema["filter_pipeline"])):
            raise RecoveryError("capsule skips a mandatory filter")
        _digest(row["raw_sha256"], "captured raw chunk digest")
        _digest(row["native_logical_sha256"], "captured decoded chunk digest")
        blocks = row["block_sha256"]
        if not isinstance(blocks, list):
            raise RecoveryError("capsule block digests must be a list")
        if not schema["filter_pipeline"]:
            if length != prod(selected.chunks) * width \
                    or len(blocks) != (length + manifest["block_bytes"] - 1) // manifest["block_bytes"]:
                raise RecoveryError("unfiltered chunk length or block count contradicts the schema")
            for digest in blocks:
                _digest(digest, "captured block digest")
        elif blocks:
            raise RecoveryError("filtered chunks cannot have direct unfiltered block digests")
        block_count += len(blocks)
        extents.append((offset, offset + length))
    if block_count > MAX_BLOCK_HASHES:
        raise RecoveryError("capsule block digest count exceeds the bound")
    extents.sort()
    if any(left[1] > right[0] for left, right in zip(extents, extents[1:])):
        raise RecoveryError("capsule physical ranges overlap")
    return rows


def _raw_chunk(stream: Any, row: dict[str, Any], source_size: int) -> bytes | None:
    offset, length = row["offset"], row["length"]
    if length > source_size - offset:
        return None
    stream.seek(offset)
    data = stream.read(length)
    return data if len(data) == length else None


def _check_current_metadata(
    snapshot: Path, path: str, schema: dict[str, Any], rows: list[dict[str, Any]],
    expected_type_encoding: bytes,
) -> dict[str, Any]:
    """Reject positive contradictions when the damaged namespace still opens.

    An inaccessible or missing root/index is the very failure this route
    addresses, so missing observations cannot veto the prior, independently
    pinned capture. A readable link to a *different* range can veto it.
    """
    try:
        handle = h5py.File(snapshot, "r")
    except (OSError, RuntimeError, ValueError):
        return {"state": "damaged_namespace_unreadable", "checked_chunk_links": 0}
    with handle:
        try:
            selected = _selected_dataset(handle, path)
        except (OSError, RuntimeError, ValueError, KeyError, UnsupportedCase):
            return {"state": "selected_path_unreadable", "checked_chunk_links": 0}
        if (_schema(selected) != schema or not selected.id.get_type().equal(
                h5py.h5t.decode(expected_type_encoding))):
            raise RecoveryError("current rooted selected dataset schema contradicts the capsule")
        observed = 0
        for row in rows:
            try:
                info = selected.id.get_chunk_info_by_coord(tuple(row["coordinate"]))
            except (OSError, RuntimeError, ValueError):
                continue
            if info.byte_offset is None or int(info.size) == 0:
                continue
            observed += 1
            if (int(info.byte_offset), int(info.size), int(info.filter_mask)) != (
                row["offset"], row["length"], row["filter_mask"]
            ):
                raise RecoveryError("current rooted chunk link contradicts the captured physical extent")
        inventory = inventory_other_allocations(
            snapshot, int(h5py.h5o.get_info(selected.id).addr),
        )
        try:
            reject_sibling_overlap(
                [(row["offset"], row["offset"] + row["length"], tuple(row["coordinate"]))
                 for row in rows], inventory,
            )
        except ValueError as exc:
            raise RecoveryError(f"current sibling allocation contradicts the capsule: {exc}") from exc
        return {"state": "selected_path_readable", "checked_chunk_links": observed,
                "sibling_inventory": inventory.report()}


def restore_from_capsule(
    damaged: str | Path, capsule_path: str | Path, capsule_sha256: str,
    output: str | Path, report_path: str | Path, *, dataset_path: str | None = None,
) -> dict[str, Any]:
    """Restore exactly matching physical bytes without opening damaged HDF5 metadata.

    On an unfiltered chunk with changed bytes, unaffected blocks can be kept.
    The per-element validity map distinguishes accepted bytes from zeros used
    solely to construct a readable partial output. Filtered chunks need their
    entire raw stream to match; digest evidence cannot decode missing bytes.
    """
    damaged, capsule_path = Path(damaged), Path(capsule_path)
    output, report_path = Path(output), Path(report_path)
    _validate_paths(damaged, output, report_path)
    _aliases([damaged, capsule_path], [output, report_path])
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-capsule-", dir=output.parent) as work:
        root = Path(work)
        template = root / _TEMPLATE
        manifest, capsule_identity = _load_capsule(capsule_path, capsule_sha256, template)
        path = manifest["schema"].get("path") if isinstance(manifest["schema"], dict) else None
        if not isinstance(path, str) or dataset_path is not None and path != dataset_path:
            raise RecoveryError("requested dataset contradicts capsule path")
        try:
            with h5py.File(template, "r") as file:
                selected = _selected_dataset(file, path)
                rows = _validate_records(manifest, selected)
                expected_type_encoding = selected.id.get_type().encode()
                shape, chunks, width = selected.shape, selected.chunks, selected.dtype.itemsize
                assert chunks is not None
                grid = tuple((size + step - 1) // step for size, step in zip(shape, chunks))
                use_elements = prod(shape) <= MAX_ELEMENT_VALIDITY
        except (OSError, RuntimeError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, RecoveryError):
                raise
            raise RecoveryError(f"capsule schema template cannot be read: {exc}") from exc
        # Snapshot guarantees that all byte-range reads refer to one immutable
        # analysis image even if an outside process writes the source in place.
        with source_snapshot(damaged) as (snapshot, damaged_sha, identity, damaged_size):
            current_metadata = _check_current_metadata(
                snapshot, path, manifest["schema"], rows, expected_type_encoding,
            )
            staged = root / "output.h5"
            shutil.copyfile(template, staged)
            status = np.full(grid, 2, dtype="u1")
            row_by_coordinate = {tuple(row["coordinate"]): row for row in rows}
            mappings: list[dict[str, Any]] = []
            unknown: list[dict[str, Any]] = []
            full_chunks = partial_chunks = accepted_elements = 0
            with snapshot.open("rb") as stream, h5py.File(staged, "r+") as destination:
                selected = destination[path]
                validity = destination.require_group("/_h5reclaim")
                chunk_validity = validity.create_dataset("chunk_status", data=status, dtype="u1")
                chunk_validity.attrs["codes_json"] = json.dumps({"recovered": 1, "allocation_unknown": 2, "unavailable": 4})
                element_validity = (validity.create_dataset(
                    "element_status", shape=shape, dtype="u1", chunks=chunks, fillvalue=0,
                ) if use_elements else None)
                for indices in itertools.product(*(range(count) for count in grid)):
                    coordinate = tuple(i * step for i, step in zip(indices, chunks))
                    row = row_by_coordinate.get(coordinate)
                    if row is None:
                        unknown.append({"coordinate": list(coordinate), "reason": "no allocation at capture"})
                        continue
                    raw = _raw_chunk(stream, row, damaged_size)
                    if raw is None:
                        status[indices] = 4
                        unknown.append({"coordinate": list(coordinate), "reason": "captured extent absent or truncated"})
                        continue
                    matches = hashlib.sha256(raw).hexdigest() == row["raw_sha256"]
                    valid_blocks: list[int] = []
                    if matches:
                        output_raw = raw
                        full_chunks += 1
                        status[indices] = 1
                    elif row["block_sha256"] and element_validity is not None:
                        size = manifest["block_bytes"]
                        repaired = bytearray(len(raw))
                        for block_index, expected in enumerate(row["block_sha256"]):
                            start = block_index * size
                            part = raw[start:start + size]
                            if hashlib.sha256(part).hexdigest() == expected:
                                repaired[start:start + len(part)] = part
                                valid_blocks.append(block_index)
                        if not valid_blocks:
                            status[indices] = 4
                            unknown.append({"coordinate": list(coordinate), "reason": "all captured blocks differ"})
                            continue
                        output_raw = bytes(repaired)
                        partial_chunks += 1
                        status[indices] = 4
                    else:
                        status[indices] = 4
                        unknown.append({"coordinate": list(coordinate), "reason": "stored bytes differ from capsule hash"})
                        continue
                    selected.id.write_direct_chunk(coordinate, output_raw, filter_mask=row["filter_mask"])
                    result_mask, result_raw = selected.id.read_direct_chunk(coordinate)
                    if result_mask != row["filter_mask"] or result_raw != output_raw:
                        raise RecoveryError("output direct chunk changed during publication")
                    if matches:
                        visible = tuple(slice(origin, min(origin + step, size))
                                        for origin, step, size in zip(coordinate, chunks, shape))
                        native_output = np.asarray(selected[visible])
                        if hashlib.sha256(native_output.tobytes(order="C")).hexdigest() != row["native_logical_sha256"]:
                            raise RecoveryError("output native decoded values differ from captured values")
                    if element_validity is not None:
                        local = np.zeros(chunks, dtype="u1")
                        if matches:
                            local[...] = 1
                        else:
                            flat = local.reshape(-1)
                            step = manifest["block_bytes"] // width
                            for block in valid_blocks:
                                flat[block * step:(block + 1) * step] = 1
                        visible = tuple(slice(0, min(step, size - origin))
                                        for origin, step, size in zip(coordinate, chunks, shape))
                        global_slice = tuple(slice(origin, origin + local_slice.stop)
                                             for origin, local_slice in zip(coordinate, visible))
                        element_validity[global_slice] = local[visible]
                        accepted_elements += int(np.count_nonzero(local[visible]))
                    else:
                        if matches:
                            accepted_elements += prod(min(step, size - origin)
                                                      for origin, step, size in zip(coordinate, chunks, shape))
                    mappings.append({
                        "coordinate": list(coordinate), "source_absolute_offset": row["offset"],
                        "stored_bytes": row["length"], "filter_mask": row["filter_mask"],
                        "captured_raw_sha256": row["raw_sha256"],
                        "verified_whole_raw_chunk": matches,
                        "verified_block_indices": valid_blocks if not matches else None,
                    })
                chunk_validity[...] = status
                complete = accepted_elements == prod(shape)
                annotation_values = {
                    "h5reclaim_chunk_status": "/_h5reclaim/chunk_status",
                    "h5reclaim_complete": complete,
                    "h5reclaim_warning": (
                        "Unknown positions may read as zeros or captured fill values; consult validity."
                    ),
                }
                if use_elements:
                    annotation_values["h5reclaim_element_status"] = "/_h5reclaim/element_status"
                annotation_collisions = sorted(name for name in annotation_values if name in selected.attrs)
                report: dict[str, Any] = {
                    "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                    "operation": "prospective_recovery_capsule", "execution_state": "finished",
                    "outcome": "complete" if complete else "partial", "complete": complete,
                    "source": {"path": str(damaged), "size_bytes": damaged_size,
                               "sha256_before": damaged_sha, "sha256_after": damaged_sha},
                    "capsule": {"path": str(capsule_path), "sha256": capsule_sha256,
                                "captured_source_sha256": manifest["source_sha256"],
                                "captured_source_size_bytes": manifest["source_size_bytes"],
                                "template_sha256": manifest["template_sha256"]},
                    "dataset": manifest["schema"], "recorded_chunks": len(rows),
                    "current_metadata_observation": current_metadata,
                    "full_chunks": full_chunks, "partial_chunks": partial_chunks,
                    "accepted_elements": accepted_elements,
                    "unknown_elements": prod(shape) - accepted_elements,
                    "selected_annotation_collisions": annotation_collisions,
                    "validity": {"chunk_status": "/_h5reclaim/chunk_status",
                                 "element_status": "/_h5reclaim/element_status" if use_elements else None,
                                 "codes": {"1": "whole raw chunk matches capture", "2": "not allocated at capture",
                                           "4": "absent, changed, or only partially verified"},
                                 "element_codes": {"0": "unknown", "1": "exact stored bytes match capture"}
                                 if use_elements else None},
                    "mappings": mappings, "unresolved_chunks": unknown,
                    "trust_note": (
                        "A separately retained SHA-256 authenticates this operator-supplied capsule, "
                        "not when it was captured. Whole stored chunks and unchanged unfiltered blocks "
                        "are accepted only at their captured physical offsets. Changed and missing bytes "
                        "remain unknown. The damaged file's namespace is not read; a captured extent "
                        "alone cannot detect later legitimate relocation or a fabricated capture. "
                        "Only one selected dataset and its captured attributes are exported."
                    ),
                }
                report_bytes = _serialize(report)
                if len(report_bytes) > MAX_REPORT_BYTES:
                    raise UnsupportedCase("capsule recovery report exceeds the limit")
                validity.create_dataset("report_json", data=report_bytes.decode("utf-8"),
                                        dtype=h5py.string_dtype("utf-8"))
                if add_output_annotations(selected, annotation_values) != annotation_collisions:
                    raise RecoveryError("selected attributes changed during capsule publication")
                validity.attrs["source_sha256"] = damaged_sha
                validity.attrs["report_schema_version"] = 1
                destination.flush()
            staged_report = root / "report.json"
            staged_report.write_bytes(report_bytes)
            _verify_source(damaged, identity, damaged_sha)
            _verify_source(capsule_path, capsule_identity, capsule_sha256)
            _validate_paths(damaged, output, report_path)
            _aliases([damaged, capsule_path], [output, report_path])
            published = False
            try:
                os.link(staged, output)
                published = True
                os.link(staged_report, report_path)
            except Exception:
                if published:
                    output.unlink(missing_ok=True)
                raise
            return report
