"""Bounded, read-only materialization of explicitly supplied VDS sources.

This is a native-readable dependency route, not damaged-index repair. The
virtual dataset itself is *never* read: HDF5 may substitute its fill value
when a source is absent. We inspect its mappings, open only operator-pinned
private source snapshots, and keep an element-level validity bitmap.

HDF5 documents the mapping introspection APIs at
https://support.hdfgroup.org/documentation/hdf5/latest/group___d_c_p_l.html
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import itertools
import json
import os
from math import prod
from pathlib import Path
import tempfile
from typing import Any, Mapping

import h5py
import numpy as np

from .dependency_routes import load_dependency_manifest
from .metadata import UnsupportedCase
from .readable_export import _canonical_numeric, _check_storage, _range_sha256, _selected_dataset
from .recovery import (
    RecoveryError, VERSION, _validate_paths, _verify_source, sha256_file,
    source_snapshot,
)


MAX_MAPPINGS = 64
MAX_ELEMENTS = 65_536
MAX_DATA_BYTES = 8 * 1024 * 1024
MAX_RELATED_BYTES = 4 * 1024 * 1024 * 1024
MAX_REPORT_BYTES = 8 * 1024 * 1024


def _exact_string(value: str | bytes, *, label: str) -> str:
    try:
        result = value.decode("utf-8", "strict") if isinstance(value, bytes) else value
    except UnicodeError as exc:
        raise UnsupportedCase(f"{label} is not exactly representable in UTF-8") from exc
    if not isinstance(result, str) or not result or len(result) > 512 or "\x00" in result:
        raise UnsupportedCase(f"{label} is missing or exceeds the bounded format")
    return result


def _selected_coordinates(space: h5py.h5s.SpaceID, shape: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    """Enumerate simple ALL or regular hyperslabs in HDF5 C coordinate order.

    Coordinates are sorted per axis before their Cartesian product. Iterating
    hyperslab blocks directly would change the point pairing in multi-axis
    block selections. Irregular/point/unlimited selections are refused.
    """
    if space.get_simple_extent_ndims() != len(shape) or not 1 <= len(shape) <= 4:
        raise UnsupportedCase("VDS selection rank does not match its dataset")
    selection_type = space.get_select_type()
    if selection_type == h5py.h5s.SEL_ALL:
        axes = [range(int(length)) for length in space.get_simple_extent_dims()]
    elif selection_type == h5py.h5s.SEL_HYPERSLABS:
        try:
            start, stride, count, block = space.get_regular_hyperslab()
        except (RuntimeError, ValueError) as exc:
            raise UnsupportedCase("irregular VDS hyperslab selection is unsupported") from exc
        axes = []
        for origin, step, repetitions, width in zip(start, stride, count, block):
            if (origin < 0 or step <= 0 or repetitions <= 0 or width <= 0
                    or repetitions > MAX_ELEMENTS or width > MAX_ELEMENTS
                    or repetitions * width > MAX_ELEMENTS or width > step and repetitions > 1):
                raise UnsupportedCase("VDS hyperslab is unbounded or self-overlapping")
            axes.append(tuple(origin + repeat * step + within
                              for repeat in range(repetitions) for within in range(width)))
    else:
        raise UnsupportedCase("only ALL and regular VDS hyperslab selections are supported")
    if prod(map(len, axes)) > MAX_ELEMENTS:
        raise UnsupportedCase("VDS selection exceeds the element limit")
    if any(axis and (axis[0] < 0 or axis[-1] >= extent)
           for axis, extent in zip(axes, shape)):
        raise UnsupportedCase("VDS selection refers outside the current dataset extent")
    points = tuple(itertools.product(*axes))
    if len(points) != int(space.get_select_npoints()) or len(set(points)) != len(points):
        raise UnsupportedCase("VDS selection has duplicate or inconsistent points")
    return points


def _raw_mapping(creation: h5py.h5p.PropDCID, index: int,
                 virtual_shape: tuple[int, ...]) -> dict[str, Any]:
    name = _exact_string(creation.get_virtual_filename(index), label="VDS source name")
    object_path = _exact_string(creation.get_virtual_dsetname(index), label="VDS source dataset path")
    if name == "." or "%" in name or "$" in name:
        raise UnsupportedCase("self-references, filename patterns, and environment substitutions are unsupported")
    virtual_space = creation.get_virtual_vspace(index)
    virtual = _selected_coordinates(virtual_space, virtual_shape)
    source_space = creation.get_virtual_srcspace(index)
    if any(dimension == h5py.h5s.UNLIMITED for dimension in source_space.get_simple_extent_dims(True)):
        raise UnsupportedCase("unlimited VDS source selections are unsupported")
    # The current source extent is checked against the actual pinned dataset
    # after it is opened; the declared selection's extent can differ from it.
    source = _selected_coordinates(source_space, tuple(source_space.get_simple_extent_dims()))
    if len(virtual) != len(source):
        raise UnsupportedCase("virtual and source selections have unequal point counts")
    return {"index": index, "declared_name": name, "object_path": object_path,
            "virtual": virtual, "source": source,
            "virtual_selection": _selection_description(virtual_space),
            "source_selection": _selection_description(source_space)}


def _selection_description(space: h5py.h5s.SpaceID) -> dict[str, Any]:
    if space.get_select_type() == h5py.h5s.SEL_ALL:
        return {"type": "all", "extent": list(space.get_simple_extent_dims())}
    start, stride, count, block = space.get_regular_hyperslab()
    return {"type": "regular_hyperslab", "extent": list(space.get_simple_extent_dims()),
            "start": list(start), "stride": list(stride), "count": list(count),
            "block": list(block)}


def _manifest_entries(manifest: Mapping[str, Any] | str | Path) -> dict[str, dict[str, str]]:
    if isinstance(manifest, (str, Path)):
        manifest = load_dependency_manifest(manifest)
    if not isinstance(manifest, Mapping) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise UnsupportedCase("related-file manifest version must be 1")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > MAX_MAPPINGS:
        raise UnsupportedCase("related-file manifest has too many entries")
    entries: dict[str, dict[str, str]] = {}
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"declared_name", "path", "sha256"}:
            raise UnsupportedCase("invalid related-file manifest entry")
        name, path, digest = entry["declared_name"], entry["path"], entry["sha256"]
        if (not isinstance(name, str) or not name or len(name) > 512 or "\x00" in name
                or name in entries or not isinstance(path, str) or not Path(path).is_absolute()
                or len(path) > 4096 or "\x00" in path or not isinstance(digest, str)
                or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise UnsupportedCase("invalid, duplicate, or unpinned related-file entry")
        entries[name] = entry
    return entries


def export_vds(source: str | Path, dataset_path: str, output: str | Path,
               report_path: str | Path,
               manifest: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Materialize a selected bounded VDS using only explicitly pinned files.

    The output's 0/1 ``/_h5reclaim/validity`` dataset is authoritative:
    zeros include absent sources, unmapped areas, and unallocated chunks.
    The output is a separate ordinary HDF5 dataset with the original HDF5
    datatype and shape, not a copy of the VDS layout or all source metadata.
    """
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    entries = _manifest_entries(manifest)
    with ExitStack() as stack:
        image, digest, identity, _ = stack.enter_context(source_snapshot(source))
        try:
            with h5py.File(image, "r") as selected_file:
                selected = _selected_dataset(selected_file, dataset_path)
                if not selected.is_virtual:
                    raise UnsupportedCase("selected dataset is not virtual")
                shape = tuple(int(axis) for axis in selected.shape)
                dtype = selected.dtype
                if (not 1 <= len(shape) <= 4 or any(axis <= 0 for axis in shape)
                        or prod(shape) > MAX_ELEMENTS or prod(shape) * dtype.itemsize > MAX_DATA_BYTES):
                    raise UnsupportedCase("VDS shape exceeds bounded rank, element, or byte limits")
                _canonical_numeric(selected.id.get_type(), dtype)
                creation = selected.id.get_create_plist()
                count = int(creation.get_virtual_count())
                if not 1 <= count <= MAX_MAPPINGS:
                    raise UnsupportedCase("VDS mapping count exceeds the supported range")
                mappings = [_raw_mapping(creation, i, shape) for i in range(count)]
                occupied: set[tuple[int, ...]] = set()
                for mapping in mappings:
                    coords = set(mapping["virtual"])
                    if occupied.intersection(coords):
                        raise UnsupportedCase("overlapping VDS mappings have ambiguous precedence")
                    occupied.update(coords)

                related: dict[str, dict[str, Any]] = {}
                related_total = 0
                for name in sorted({mapping["declared_name"] for mapping in mappings}):
                    if name not in entries:
                        related[name] = {"status": "not_supplied"}
                        continue
                    entry = entries[name]
                    path = Path(entry["path"])
                    if not path.is_file():
                        related[name] = {"status": "file_unavailable", "path": str(path)}
                        continue
                    file_size = path.stat().st_size
                    if file_size > MAX_RELATED_BYTES - related_total:
                        raise UnsupportedCase("related files exceed the aggregate snapshot limit")
                    snapshot, observed, related_identity, copied_size = stack.enter_context(
                        source_snapshot(path, max_source_bytes=MAX_RELATED_BYTES - related_total))
                    related_total += copied_size
                    if observed != entry["sha256"]:
                        related[name] = {"status": "hash_mismatch", "path": str(path),
                                         "expected_sha256": entry["sha256"], "observed_sha256": observed,
                                         "identity": related_identity, "snapshot": snapshot}
                    else:
                        related[name] = {"status": "hash_matched", "path": str(path),
                                         "sha256": observed, "identity": related_identity,
                                         "snapshot": snapshot, "size": copied_size}

                values = np.zeros(shape, dtype=dtype)
                validity = np.zeros(shape, dtype="u1")
                evidence: list[dict[str, Any]] = []
                snapshots: dict[str, h5py.File] = {}
                for mapping in mappings:
                    name = mapping["declared_name"]
                    item = related[name]
                    record = {"mapping_index": mapping["index"], "declared_name": name,
                              "object_path": mapping["object_path"], "mapped_elements": len(mapping["virtual"]),
                              "virtual_selection": mapping["virtual_selection"],
                              "source_selection": mapping["source_selection"],
                              "coordinate_pairing": "C-order selection iteration",
                              "accepted_elements": 0, "status": item["status"]}
                    evidence.append(record)
                    if item["status"] != "hash_matched":
                        continue
                    if name not in snapshots:
                        snapshots[name] = stack.enter_context(h5py.File(item["snapshot"], "r"))
                    try:
                        data = _selected_dataset(snapshots[name], mapping["object_path"])
                        if data.is_virtual or data.id.get_create_plist().get_external_count():
                            raise UnsupportedCase("transitive source dependency is unsupported")
                        if not data.id.get_type().equal(selected.id.get_type()):
                            raise UnsupportedCase("VDS source datatype differs from the virtual dataset")
                        actual_shape = tuple(int(length) for length in data.shape)
                        if any(any(point[axis] >= extent for axis, extent in enumerate(actual_shape))
                               for point in mapping["source"]):
                            raise UnsupportedCase("source selection exceeds the pinned dataset's current extent")
                        storage = _check_storage(data, item["size"])
                        record["source_object_header_address"] = int(h5py.h5o.get_info(data.id).addr)
                        record["storage_layout"] = storage.layout
                        record["source_sha256"] = item["sha256"]
                        selected_origins: set[tuple[int, ...]] = set()
                        # The storage inspector checks every reachable allocation
                        # record. Only selected allocated coordinates are read.
                        unknown = set(storage.unknown)
                        for virtual_coord, source_coord in zip(mapping["virtual"], mapping["source"]):
                            if storage.layout == "chunked":
                                assert data.chunks is not None
                                origin = tuple((index // width) * width
                                               for index, width in zip(source_coord, data.chunks))
                                if origin in unknown:
                                    continue
                                selected_origins.add(origin)
                            values[virtual_coord] = data[source_coord]
                            validity[virtual_coord] = 1
                            record["accepted_elements"] += 1
                        if storage.layout == "chunked":
                            by_origin = {origin: (address, size, mask)
                                         for origin, address, size, mask in storage.records}
                            record["source_chunks"] = [
                                {"origin": list(origin), "physical_offset": by_origin[origin][0],
                                 "stored_bytes": by_origin[origin][1], "filter_mask": by_origin[origin][2],
                                 "raw_sha256": _range_sha256(item["snapshot"],
                                                             by_origin[origin][0], by_origin[origin][1])}
                                for origin in sorted(selected_origins)
                            ]
                        elif storage.layout == "contiguous":
                            offset = data.id.get_offset()
                            record["source_contiguous_extent"] = {
                                "physical_offset": int(offset),
                                "size_bytes": prod(actual_shape) * dtype.itemsize,
                                "raw_sha256": _range_sha256(item["snapshot"], int(offset),
                                                            prod(actual_shape) * dtype.itemsize),
                            }
                        else:
                            record["source_compact_extent"] = "inside selected object header; no separate raw extent"
                        record["status"] = "partial" if record["accepted_elements"] != record["mapped_elements"] else "accepted"
                    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                        if isinstance(exc, UnsupportedCase):
                            raise
                        # A broken source cannot justify accepted values from
                        # this mapping, even if some point reads succeeded.
                        for coord in mapping["virtual"]:
                            values[coord] = 0
                            validity[coord] = 0
                        record["accepted_elements"] = 0
                        record["status"] = "source_unreadable"
                        record["reason"] = type(exc).__name__

                accepted = int(np.count_nonzero(validity))
                report = {
                    "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                    "mode": "vds_materialized_export", "outcome": "complete" if accepted == prod(shape) else "partial",
                    "source": {"path": str(source), "sha256_before": digest, "sha256_after": digest},
                    "dataset": {"path": dataset_path, "shape": list(shape), "dtype": dtype.str,
                                "layout": "virtual", "output_layout": "materialized"},
                    "mappings": evidence, "related_files": [
                        {key: value for key, value in ({"declared_name": name} | item).items()
                         if key not in ("snapshot", "identity")}
                        for name, item in sorted(related.items())],
                    "accepted_elements": accepted, "unknown_elements": prod(shape) - accepted,
                    "validity_map": "/_h5reclaim/validity",
                    "validity_codes": {"0": "unknown; output cell is not a measurement",
                                       "1": "mapped to allocated native-readable pinned source"},
                    "output_path": str(output),
                    "verification": "accepted native values read back bitwise from separate output",
                    "limits": "Pinned hashes establish the supplied current files, not historical scientific truth. "
                              "Missing or unreadable values remain unknown. Attributes, links and virtual "
                              "layout are not copied; no damaged VDS metadata is reconstructed.",
                }
                report_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
                if len(report_text.encode("utf-8")) > MAX_REPORT_BYTES:
                    raise UnsupportedCase("VDS evidence report exceeds the publication limit")
                with tempfile.TemporaryDirectory(prefix=".h5reclaim-vds-", dir=output.parent) as out_dir:
                    with tempfile.TemporaryDirectory(prefix=".h5reclaim-vds-", dir=report_path.parent) as rep_dir:
                        staged_output = Path(out_dir) / "output.h5"
                        staged_report = Path(rep_dir) / "report.json"
                        with h5py.File(staged_output, "x") as target:
                            parts = dataset_path[1:].split("/")
                            parent = target["/"]
                            for part in parts[:-1]:
                                parent = parent.create_group(part)
                            space = h5py.h5s.create_simple(shape)
                            created = h5py.h5d.create(parent.id, parts[-1].encode("utf-8"),
                                                       selected.id.get_type().copy(), space)
                            created.close()
                            target[dataset_path][...] = values
                            metadata = target.create_group("/_h5reclaim")
                            metadata.create_dataset("validity", data=validity, dtype="u1")
                            metadata.create_dataset("report_json", data=report_text,
                                                    dtype=h5py.string_dtype(encoding="utf-8"))
                            metadata.attrs["source_sha256"] = digest
                        with h5py.File(staged_output, "r") as verified:
                            exported = verified[dataset_path]
                            if (not exported.id.get_type().equal(selected.id.get_type())
                                    or not np.array_equal(verified["/_h5reclaim/validity"][...], validity)
                                    or np.asarray(exported[...]).tobytes() != values.tobytes()):
                                raise RecoveryError("VDS output readback differs from selected source values")
                        staged_report.write_text(report_text, encoding="utf-8")
                        if sha256_file(image) != digest:
                            raise RecoveryError("private VDS snapshot changed during export")
                        _verify_source(source, identity, digest)
                        for name, item in related.items():
                            if item["status"] in ("hash_matched", "hash_mismatch"):
                                if sha256_file(item["snapshot"]) != item.get("sha256", item.get("observed_sha256")):
                                    raise RecoveryError("private related-file snapshot changed during export")
                                _verify_source(Path(item["path"]), item["identity"],
                                               item.get("sha256", item.get("observed_sha256")))
                        _validate_paths(source, output, report_path)
                        published = False
                        try:
                            os.link(staged_output, output)
                            published = True
                            os.link(staged_report, report_path)
                        except OSError:
                            if published:
                                output.unlink(missing_ok=True)
                            raise
                        return report
        except (UnsupportedCase, RecoveryError):
            raise
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            raise RecoveryError(f"native VDS export failed: {type(exc).__name__}: {exc}") from exc
