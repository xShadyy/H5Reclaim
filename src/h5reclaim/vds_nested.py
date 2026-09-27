"""Resolve one nested virtual-dataset hop from independently pinned files.

This module never reads a virtual dataset's values.  It pairs its declared
finite selections, then reads only allocated local datasets in pinned private
snapshots.  Missing dependencies and unallocated storage remain unknown.
"""

from __future__ import annotations

from math import prod
from typing import Any

import h5py

from .metadata import UnsupportedCase
from .readable_export import _check_competing_owners, _check_storage, _range_sha256, _selected_dataset
from .vds_export import MAX_ELEMENTS, MAX_MAPPINGS, _raw_mapping, _selected_coordinates


def resolve_nested(
    virtual: h5py.Dataset,
    requested: tuple[tuple[int, ...], ...],
    related: dict[str, dict[str, Any]],
    snapshots: dict[str, h5py.File],
    stack: Any,
    expected_type: h5py.h5t.TypeID,
    budget: dict[str, int],
) -> tuple[dict[tuple[int, ...], Any], list[dict[str, Any]]]:
    """Return justified point values and physical-source evidence.

    One layer of VDS nesting is accepted.  A further virtual or external
    dependency refuses rather than falling through to HDF5's fill semantics.
    All declared nested mappings are checked, including unrequested ones,
    because their precedence could affect selected points.
    """
    if not virtual.is_virtual:
        raise UnsupportedCase("nested resolver requires a virtual source")
    shape = tuple(int(axis) for axis in virtual.shape)
    if (not 1 <= len(shape) <= 4 or any(axis <= 0 for axis in shape)
            or prod(shape) > MAX_ELEMENTS or not virtual.id.get_type().equal(expected_type)):
        raise UnsupportedCase("nested VDS schema exceeds limits or differs from selected datatype")
    if any(len(point) != len(shape) or any(i < 0 or i >= size for i, size in zip(point, shape))
           for point in requested):
        raise UnsupportedCase("nested source selection exceeds current extent")
    creation = virtual.id.get_create_plist()
    count = int(creation.get_virtual_count())
    if not 1 <= count <= MAX_MAPPINGS or count > MAX_MAPPINGS - budget["mappings"]:
        raise UnsupportedCase("transitive VDS mappings exceed the aggregate limit")
    budget["mappings"] += count
    mappings = [_raw_mapping(creation, i, shape) for i in range(count)]
    budget["points"] += sum(len(mapping["virtual"]) for mapping in mappings)
    if budget["points"] > MAX_ELEMENTS * 4:
        raise UnsupportedCase("aggregate transitive VDS selection exceeds the point budget")
    occupied: set[tuple[int, ...]] = set()
    for mapping in mappings:
        coords = set(mapping["virtual"])
        if occupied.intersection(coords):
            raise UnsupportedCase("overlapping nested VDS mappings have ambiguous precedence")
        occupied.update(coords)
    needed = set(requested)
    values: dict[tuple[int, ...], Any] = {}
    evidence: list[dict[str, Any]] = []
    for mapping in mappings:
        name = mapping["declared_name"]
        item = related.get(name, {"status": "not_supplied"})
        record: dict[str, Any] = {
            "mapping_index": mapping["index"], "declared_name": name,
            "object_path": mapping["object_path"],
            "virtual_selection": mapping["virtual_selection"],
            "source_selection": mapping["source_selection"],
            "mapped_elements": len(mapping["virtual"]),
            "accepted_requested_elements": 0, "status": item["status"],
        }
        evidence.append(record)
        if not needed.intersection(mapping["virtual"]):
            continue
        if item["status"] != "hash_matched":
            continue
        if name not in snapshots:
            snapshots[name] = stack.enter_context(h5py.File(item["snapshot"], "r"))
        leaf = _selected_dataset(snapshots[name], mapping["object_path"])
        if leaf.is_virtual or leaf.id.get_create_plist().get_external_count():
            raise UnsupportedCase("a third VDS layer or external raw storage is unsupported")
        if not leaf.id.get_type().equal(expected_type):
            raise UnsupportedCase("nested source HDF5 datatype differs from selected datatype")
        actual_shape = tuple(int(axis) for axis in leaf.shape)
        if not 1 <= len(actual_shape) <= 4 or any(axis <= 0 for axis in actual_shape):
            raise UnsupportedCase("nested leaf must have nonempty rank one through four")
        source_coords = mapping["source"]
        if source_coords is None:
            if prod(actual_shape) != len(mapping["virtual"]):
                raise UnsupportedCase("nested full source extent differs from mapped point count")
            source_coords = _selected_coordinates(leaf.id.get_space(), actual_shape)
            record["source_selection"]["resolved_extent"] = list(actual_shape)
        if any(any(index >= extent for index, extent in zip(point, actual_shape))
               for point in source_coords):
            raise UnsupportedCase("nested source selection exceeds pinned current extent")
        pairs = [(v, s) for v, s in zip(mapping["virtual"], source_coords) if v in needed]
        storage = _check_storage(leaf, item["size"])
        record["ownership_inventory"] = _check_competing_owners(item["snapshot"], leaf, storage)
        record["source_object_header_address"] = int(h5py.h5o.get_info(leaf.id).addr)
        record["storage_layout"] = storage.layout
        record["source_sha256"] = item["sha256"]
        record["source_file"] = item["path"]
        unknown = set(storage.unknown)
        origins: set[tuple[int, ...]] = set()
        for virtual_coord, source_coord in pairs:
            if storage.layout == "chunked":
                assert leaf.chunks is not None
                origin = tuple((index // width) * width
                               for index, width in zip(source_coord, leaf.chunks))
                if origin in unknown:
                    continue
                origins.add(origin)
            values[virtual_coord] = leaf[source_coord]
            record["accepted_requested_elements"] += 1
        if storage.layout == "chunked":
            by_origin = {origin: (address, size, mask)
                         for origin, address, size, mask in storage.records}
            record["source_chunks"] = [
                {"origin": list(origin), "physical_offset": by_origin[origin][0],
                 "stored_bytes": by_origin[origin][1], "filter_mask": by_origin[origin][2],
                 "raw_sha256": _range_sha256(item["snapshot"], by_origin[origin][0],
                                             by_origin[origin][1])}
                for origin in sorted(origins)
            ]
        elif storage.layout == "contiguous":
            offset = int(leaf.id.get_offset())
            record["source_contiguous_extent"] = {
                "physical_offset": offset,
                "size_bytes": prod(actual_shape) * leaf.dtype.itemsize,
                "raw_sha256": _range_sha256(item["snapshot"], offset,
                                            prod(actual_shape) * leaf.dtype.itemsize),
            }
        else:
            record["source_compact_extent"] = "inside selected leaf object header"
        record["status"] = ("accepted" if record["accepted_requested_elements"] == len(pairs)
                            else "partial")
    return values, evidence
