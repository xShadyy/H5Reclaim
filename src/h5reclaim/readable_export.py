"""Copy bounded native-readable data without claiming damage recovery.

This route is intentionally separate from structural recovery. The HDF5 library
must resolve every accepted value. An unwritten or unreachable chunk is marked
unknown rather than being scored as a measurement. A successful round trip
establishes what the current input reads as, not what was measured
before a possible corruption. A private bounded snapshot keeps source reads
consistent and the caller's file remains untouched.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from dataclasses import dataclass
from math import prod
from pathlib import Path
from typing import Any, Iterator

import h5py
import numpy as np

from .hints import DatasetHints, HintsError, compare_hints, require_no_conflicts
from .metadata import UnsupportedCase
from .native_io import read_fixed_block, write_fixed_block
from .ownership_inventory import (
    OwnershipInventory, inventory_other_allocations, reject_sibling_overlap,
)
from .format import FormatError
from .recovery import (
    RecoveryError,
    VERSION,
    _validate_paths,
    _verify_source,
    _identity,
    sha256_file,
    source_snapshot,
)


MAX_DATA_BYTES = 512 * 1024 * 1024
MAX_BLOCK_BYTES = 1024 * 1024
MAX_STORED_CHUNK_BYTES = 2 * 1024 * 1024
MAX_CHUNKS = 8192
MAX_RANK = 4
MAX_PATH_BYTES = 4096
MAX_TYPE_DEPTH = 4
MAX_TYPE_MEMBERS = 64
MAX_FIXED_FIELD_BYTES = 4096
MAX_REFERENCE_ELEMENTS_PER_BLOCK = 4096
NATIVE_WORKER_SECONDS = 900
NATIVE_WORKER_MEMORY_BYTES = 3 * 1024 * 1024 * 1024
MAX_WORKER_MESSAGE_BYTES = 65536
# HDF5's standard filters and h5py's compiled-in LZF implementation.
NATIVE_FILTERS = frozenset((h5py.h5z.FILTER_DEFLATE, h5py.h5z.FILTER_SHUFFLE,
                            h5py.h5z.FILTER_FLETCHER32, h5py.h5z.FILTER_SZIP,
                            h5py.h5z.FILTER_NBIT,
                            h5py.h5z.FILTER_SCALEOFFSET, h5py.h5z.FILTER_LZF))


def _selected_dataset(handle: h5py.File, path: str) -> h5py.Dataset:
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise UnsupportedCase("select an absolute dataset path such as /group/data")
    if "\x00" in path:
        raise UnsupportedCase("selected dataset path contains a NUL byte")
    parts = path[1:].split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UnsupportedCase("selected dataset path must be canonical")
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


def _canonical_numeric(datatype: h5py.h5t.TypeID, dtype: np.dtype) -> None:
    """Accept bounded integers and ordinary IEEE floating-point storage.

    Reduced-precision integers are safely copied by native HDF5 with the
    original type retained and a bitwise readback. Their unused storage bits
    are not separately claimed as recovered measurements.
    """
    if dtype.kind not in "iuf" or dtype.subdtype is not None or dtype.itemsize not in (1, 2, 4, 8):
        raise UnsupportedCase("readable export requires a primitive fixed-width integer or IEEE float")
    klass = datatype.get_class()
    precision = datatype.get_precision()
    offset = datatype.get_offset()
    if (datatype.get_size() != dtype.itemsize or precision < 1 or offset < 0
            or precision + offset > dtype.itemsize * 8):
        raise UnsupportedCase("numeric datatype has invalid bit precision or offset")
    if datatype.get_pad() != (h5py.h5t.PAD_ZERO, h5py.h5t.PAD_ZERO):
        raise UnsupportedCase("numeric datatype has unsupported padding")
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
        if precision != dtype.itemsize * 8 or offset != 0:
            raise UnsupportedCase("floating-point datatype is not a full-width IEEE value")
        fields, bias = {
            4: ((31, 23, 8, 0, 23), 127),
            8: ((63, 52, 11, 0, 52), 1023),
        }.get(dtype.itemsize, (None, None))
        if (klass != h5py.h5t.FLOAT or datatype.get_fields() != fields
                or datatype.get_ebias() != bias or datatype.get_norm() != h5py.h5t.NORM_IMPLIED
                or datatype.get_inpad() != h5py.h5t.PAD_ZERO):
            raise UnsupportedCase("floating-point representation is not IEEE binary32 or binary64")


def _safe_fixed_type(datatype: h5py.h5t.TypeID, dtype: np.dtype, depth: int = 0) -> None:
    """Bound native representations before reading them, including nested fields.

    The original HDF5 type is copied verbatim into the output. These checks
    exclude heap allocations and references; readback verifies the native
    representation visible through h5py, including NaN bits and record fields.
    """
    if depth > MAX_TYPE_DEPTH or dtype.itemsize > MAX_BLOCK_BYTES or dtype.hasobject:
        raise UnsupportedCase("datatype is variable length, too deep, or exceeds the block limit")
    klass = datatype.get_class()
    if klass in (h5py.h5t.INTEGER, h5py.h5t.FLOAT):
        _canonical_numeric(datatype, dtype)
        return
    if klass == h5py.h5t.BITFIELD:
        if (dtype.kind != "u" or dtype.itemsize not in (1, 2, 4, 8)
                or datatype.get_size() != dtype.itemsize
                or datatype.get_order() not in (h5py.h5t.ORDER_LE, h5py.h5t.ORDER_BE,
                                                 h5py.h5t.ORDER_NONE)):
            raise UnsupportedCase("bitfield does not have bounded unsigned fixed-size storage")
        return
    if klass == h5py.h5t.ENUM:
        base = datatype.get_super()
        if datatype.get_nmembers() > MAX_TYPE_MEMBERS or datatype.get_size() != dtype.itemsize:
            raise UnsupportedCase("enum type is too large or has incompatible storage")
        # h5py represents its boolean enum as bool; the base is a byte integer.
        base_dtype = (np.dtype("i1" if base.get_sign() == h5py.h5t.SGN_2 else "u1")
                      if dtype.kind == "b" and dtype.itemsize == 1 else np.dtype(dtype.str))
        if base_dtype.kind not in "iu":
            raise UnsupportedCase("enum has no fixed-size integer base")
        _safe_fixed_type(base, base_dtype, depth + 1)
        return
    if klass == h5py.h5t.STRING:
        if (datatype.is_variable_str() or dtype.kind != "S"
                or not 0 < dtype.itemsize <= MAX_FIXED_FIELD_BYTES
                or datatype.get_size() != dtype.itemsize):
            raise UnsupportedCase("only bounded fixed-size strings can be exported")
        return
    if klass == h5py.h5t.OPAQUE:
        if dtype.kind != "V" or not 0 < dtype.itemsize <= MAX_FIXED_FIELD_BYTES:
            raise UnsupportedCase("opaque field has incompatible fixed-size storage")
        return
    if klass == h5py.h5t.ARRAY:
        if dtype.subdtype is None:
            raise UnsupportedCase("array datatype has no fixed-size element description")
        base_dtype, dimensions = dtype.subdtype
        if (tuple(datatype.get_array_dims()) != tuple(dimensions)
                or datatype.get_size() != dtype.itemsize):
            raise UnsupportedCase("array datatype dimensions disagree with native dtype")
        _safe_fixed_type(datatype.get_super(), base_dtype, depth + 1)
        return
    if klass == h5py.h5t.COMPOUND:
        count = datatype.get_nmembers()
        if count > MAX_TYPE_MEMBERS or datatype.get_size() != dtype.itemsize:
            raise UnsupportedCase("record datatype exceeds the bounded representation")
        if dtype.kind == "c":
            if count != 2 or dtype.itemsize not in (8, 16):
                raise UnsupportedCase("complex datatype is not two canonical floats")
            half = dtype.itemsize // 2
            for index, (name, position) in enumerate(((b"r", 0), (b"i", half))):
                if datatype.get_member_name(index) != name or datatype.get_member_offset(index) != position:
                    raise UnsupportedCase("complex field layout differs from the native convention")
                _safe_fixed_type(datatype.get_member_type(index),
                                 np.dtype(dtype.byteorder + ("f4" if half == 4 else "f8")), depth + 1)
            return
        if dtype.names is None or len(dtype.names) != count:
            raise UnsupportedCase("record fields disagree with native dtype")
        for index, name in enumerate(dtype.names):
            member_dtype, offset = dtype.fields[name][:2]
            if (datatype.get_member_name(index).decode("utf-8", "surrogateescape") != name
                    or datatype.get_member_offset(index) != offset):
                raise UnsupportedCase("record field name or offset disagrees with native dtype")
            _safe_fixed_type(datatype.get_member_type(index), member_dtype, depth + 1)
        return
    raise UnsupportedCase("references, variable-length values, and this datatype class are not safely exportable")


def _safe_reference_type(datatype: h5py.h5t.TypeID, dtype: np.dtype) -> str | None:
    """Recognize only top-level object and selected-dataset region references.

    A reference's stored bytes identify an object in the *source* file. They
    cannot be copied directly into a new HDF5 file. The exporter remaps only
    null and self references after checking each source referent and region.
    """
    if datatype.get_class() != h5py.h5t.REFERENCE:
        return None
    if h5py.check_dtype(ref=dtype) is h5py.Reference and datatype.equal(h5py.h5t.STD_REF_OBJ):
        return "object"
    if (h5py.check_dtype(ref=dtype) is h5py.RegionReference
            and datatype.equal(h5py.h5t.STD_REF_DSETREG)):
        return "region"
    raise UnsupportedCase("only top-level object and dataset-region references can be remapped")


def _reference_tokens(block: np.ndarray, handle: h5py.File, selected: h5py.Dataset,
                      selected_address: int, kind: str) -> tuple[bytes, int, int]:
    """Return comparable *logical* tokens; Python object pointers are not data."""
    if block.size > MAX_REFERENCE_ELEMENTS_PER_BLOCK:
        raise UnsupportedCase("reference read exceeds the per-block element limit")
    tokens = bytearray()
    null_count = self_count = 0
    for reference in block.flat:
        expected_type = h5py.Reference if kind == "object" else h5py.RegionReference
        if type(reference) is not expected_type:
            raise UnsupportedCase("native read did not return the selected reference type")
        if not reference:
            tokens.append(0)
            null_count += 1
            continue
        try:
            referent = handle[reference]
            address = int(h5py.h5o.get_info(referent.id).addr)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise UnsupportedCase("object reference target is dangling or unreadable") from exc
        if not isinstance(referent, h5py.Dataset) or address != selected_address:
            raise UnsupportedCase("object reference points outside the selected dataset")
        if kind == "region":
            try:
                space = h5py.h5r.get_region(reference, handle.id)
                if (space is None or not space.select_valid()
                        or tuple(space.get_simple_extent_dims()) != selected.shape):
                    raise UnsupportedCase("dataset-region reference has an invalid selection")
                encoded = space.encode()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise UnsupportedCase("dataset-region selection is unreadable") from exc
            if (len(encoded) > 65536 or space.get_select_npoints() > MAX_DATA_BYTES
                    or len(tokens) + len(encoded) + 5 > MAX_BLOCK_BYTES):
                raise UnsupportedCase("dataset-region selection exceeds the bounded limit")
            tokens.extend(b"\x02" + len(encoded).to_bytes(4, "little") + encoded)
        else:
            tokens.append(1)
        self_count += 1
    return bytes(tokens), null_count, self_count


@dataclass(frozen=True)
class StorageInspection:
    layout: str
    grid: tuple[int, ...]
    records: tuple[tuple[tuple[int, ...], int, int, int], ...]
    unknown: tuple[tuple[int, ...], ...]


def _safe_export_attributes(dataset: h5py.Dataset) -> tuple[tuple[tuple[str, Any], ...], tuple[str, ...]]:
    """Read only bounded inline, fixed-size attributes; enumerate omissions."""
    if len(dataset.attrs) > 64:
        return (), ("all source attributes (more than 64)",)
    copied: list[tuple[str, Any]] = []
    omitted: list[str] = []
    for name in dataset.attrs:
        if not isinstance(name, str) or len(name.encode("utf-8")) > 128:
            omitted.append(str(name)[:128])
            continue
        try:
            attr = dataset.attrs.get_id(name)
            typ = attr.get_type()
            space = attr.get_space()
            dtype = typ.dtype
            points = space.get_simple_extent_npoints()
            if (space.get_simple_extent_type() != h5py.h5s.SIMPLE
                    and space.get_simple_extent_type() != h5py.h5s.SCALAR):
                omitted.append(name)
                continue
            if (space.get_simple_extent_ndims() > 4 or points * dtype.itemsize > 4096
                    or attr.get_storage_size() > 4096):
                omitted.append(name)
                continue
            _safe_fixed_type(typ, dtype)
            copied.append((name, dataset.attrs[name]))
        except (OSError, RuntimeError, ValueError, TypeError, UnicodeError):
            omitted.append(name)
    return tuple(copied), tuple(omitted)


def _check_storage(dataset: h5py.Dataset, snapshot_size: int,
                   file_itemsize: int | None = None) -> StorageInspection:
    creation = dataset.id.get_create_plist()
    itemsize = file_itemsize or dataset.dtype.itemsize
    if creation.get_external_count() or dataset.is_virtual:
        raise UnsupportedCase("external and virtual dataset storage is not supported")
    filter_count = creation.get_nfilters()
    if filter_count > 8:
        raise UnsupportedCase("filter pipeline exceeds the eight-filter limit")
    filters = [creation.get_filter(index)[0] for index in range(filter_count)]
    if any(value not in NATIVE_FILTERS for value in filters):
        raise UnsupportedCase("filter pipeline would require a plugin or exceeds the limit")
    for value in filters:
        if (not h5py.h5z.filter_avail(value)
                or (h5py.h5z.get_filter_info(value) & (
                    h5py.h5z.FILTER_CONFIG_DECODE_ENABLED | h5py.h5z.FILTER_CONFIG_ENCODE_ENABLED
                )) != (h5py.h5z.FILTER_CONFIG_DECODE_ENABLED
                       | h5py.h5z.FILTER_CONFIG_ENCODE_ENABLED)):
            raise UnsupportedCase("a built-in filter encoder or decoder is unavailable")
    layout = creation.get_layout()
    if layout == h5py.h5d.CHUNKED:
        chunks = dataset.chunks
        assert chunks is not None
        if prod(chunks) * itemsize > MAX_BLOCK_BYTES:
            raise UnsupportedCase("decoded chunk exceeds the 1 MiB limit")
        grid = prod((length + width - 1) // width for length, width in zip(dataset.shape, chunks))
        if grid > MAX_CHUNKS:
            raise UnsupportedCase(f"dataset exceeds the {MAX_CHUNKS}-chunk limit")
        # Native reads may silently substitute fill values for missing index
        # entries. Mark these unknown, never count them as measurement values.
        ranges: list[tuple[int, int]] = []
        records: list[tuple[tuple[int, ...], int, int, int]] = []
        unknown: list[tuple[int, ...]] = []
        for indices in itertools.product(*(range(0, length, width) for length, width in zip(dataset.shape, chunks))):
            chunk = dataset.id.get_chunk_info_by_coord(indices)
            if chunk.byte_offset is None or int(chunk.size) == 0:
                unknown.append(tuple(indices))
                continue
            from .native_addresses import chunk_address
            address, length = chunk_address(dataset, chunk.byte_offset), int(chunk.size)
            if tuple(int(position) for position in chunk.chunk_offset) != tuple(indices):
                raise UnsupportedCase("a chunk record disagrees with its requested coordinate")
            if int(chunk.filter_mask) & ~((1 << filter_count) - 1):
                raise UnsupportedCase("a chunk filter mask references an absent filter")
            if address < 0 or length <= 0 or address + length > snapshot_size:
                raise UnsupportedCase("a chunk has no valid allocated payload in the source file")
            if length > MAX_STORED_CHUNK_BYTES:
                raise UnsupportedCase("a stored chunk exceeds the 2 MiB limit")
            ranges.append((address, address + length))
            records.append((tuple(indices), address, length, int(chunk.filter_mask)))
        ranges.sort()
        if any(first[1] > second[0] for first, second in zip(ranges, ranges[1:])):
            raise UnsupportedCase("different chunk records claim overlapping source bytes")
        if dataset.id.get_num_chunks() != len(records):
            raise UnsupportedCase("chunk count disagrees with reachable allocation records")
        return StorageInspection("chunked", tuple((length + width - 1) // width
                                                 for length, width in zip(dataset.shape, chunks)),
                                 tuple(records), tuple(unknown))
    if filters:
        raise UnsupportedCase("filters require chunked storage")
    if layout in (h5py.h5d.CONTIGUOUS, h5py.h5d.COMPACT):
        # For these two layouts there are no per-chunk allocation records.
        # Refuse a merely defined, unwritten dataset whose reads use fill.
        if dataset.id.get_storage_size() != prod(dataset.shape) * itemsize:
            raise UnsupportedCase("the contiguous or compact dataset is not fully allocated")
        if layout == h5py.h5d.CONTIGUOUS:
            address = dataset.id.get_offset()
            if address is None or address < 0 or address + dataset.id.get_storage_size() > snapshot_size:
                raise UnsupportedCase("contiguous payload address lies outside the source file")
        return StorageInspection("compact" if layout == h5py.h5d.COMPACT else "contiguous",
                                 (), (), ())
    raise UnsupportedCase("dataset layout is not a local compact, contiguous, or chunked layout")


def _check_competing_owners(snapshot: Path, dataset: h5py.Dataset,
                            storage: StorageInspection) -> dict[str, Any]:
    """Refuse native reads whose source bytes also belong to a rooted sibling.

    Native HDF5 accepts an index pointer redirected into another dataset's
    allocation. A successful readback of that value does not establish the
    selected dataset's ownership of those bytes. Enumeration is bounded; if
    it could not finish, the absence of a competing owner is unestablished.
    """
    selected_address = int(h5py.h5o.get_info(dataset.id).addr)
    inventory = inventory_other_allocations(snapshot, selected_address)
    if storage.layout == "chunked":
        selected = [(address, address + size, origin)
                    for origin, address, size, _mask in storage.records]
    elif storage.layout == "contiguous":
        address = dataset.id.get_offset()
        selected = [(int(address), int(address) + int(dataset.id.get_storage_size()), ())]
    else:
        selected = []
    return _require_no_competing_owner(inventory, selected)


def _require_no_competing_owner(inventory: OwnershipInventory,
                                ranges: list[tuple[int, int, tuple[int, ...]]]) -> dict[str, Any]:
    """Reject observed overlaps and incomplete native owner inventories."""
    try:
        reject_sibling_overlap(ranges, inventory)
    except FormatError as exc:
        raise UnsupportedCase(str(exc)) from exc
    if not inventory.complete:
        raise UnsupportedCase("competing-owner inventory is incomplete: "
                              + "; ".join(inventory.incomplete_reasons))
    return inventory.report()


def _blocks(shape: tuple[int, ...], itemsize: int,
            max_elements: int | None = None) -> Iterator[tuple[slice, ...]]:
    dimensions = [1] * len(shape)
    allowance = min(MAX_BLOCK_BYTES // itemsize, max_elements or MAX_BLOCK_BYTES // itemsize)
    for index in range(len(shape) - 1, -1, -1):
        dimensions[index] = min(shape[index], max(1, allowance))
        allowance = max(1, allowance // dimensions[index])
    for starts in itertools.product(*(range(0, size, step) for size, step in zip(shape, dimensions))):
        yield tuple(slice(start, min(start + step, size)) for start, step, size in zip(starts, dimensions, shape))


def _allocated_selections(storage: StorageInspection, shape: tuple[int, ...],
                          chunks: tuple[int, ...] | None, itemsize: int,
                          max_elements: int | None = None) -> Iterator[tuple[slice, ...]]:
    if storage.layout != "chunked":
        yield from _blocks(shape, itemsize, max_elements)
        return
    assert chunks is not None
    for origin, _, _, _ in storage.records:
        yield tuple(slice(start, min(start + step, length))
                    for start, step, length in zip(origin, chunks, shape))


def _create_matching_dataset(target: h5py.File, source: h5py.Dataset, path: str, *,
                             preserve_filters: bool = True) -> h5py.Dataset:
    """Keep the selected type, extent limits, fill rules, layout and filter order."""
    parts = path[1:].split("/")
    parent = target["/"]
    for part in parts[:-1]:
        parent = parent.require_group(part)
    creation = source.id.get_create_plist().copy()
    creation.set_attr_creation_order(h5py.h5p.CRT_ORDER_TRACKED | h5py.h5p.CRT_ORDER_INDEXED)
    if not preserve_filters:
        for identifier, *_ in [creation.get_filter(i) for i in range(creation.get_nfilters())]:
            creation.remove_filter(identifier)
    pipeline = [creation.get_filter(i) for i in range(creation.get_nfilters())]
    if any(item[0] == 32008 for item in pipeline):
        # Bitshuffle prepends its version and record width in set_local.
        # Replaying persisted parameters would prepend them a second time,
        # changing the compression options and making the output unreadable.
        for identifier, *_ in pipeline:
            creation.remove_filter(identifier)
        for identifier, flags, values, _name in pipeline:
            if identifier == 32008:
                if len(values) not in (5, 6) or values[2] != source.id.get_type().get_size():
                    raise UnsupportedCase("Bitshuffle persisted parameters disagree with the dataset type")
                values = values[3:]
            creation.set_filter(identifier, flags, values)
    typ = source.id.get_type().copy()
    if source.id.get_type().committed():
        named_path = h5py.h5i.get_name(source.id.get_type())
        if named_path:
            type_parent, type_name = named_path.rsplit(b'/', 1)
            group = target.require_group(type_parent or b'/')
            if type_name not in group:
                from .output_annotations import commit_type
                commit_type(group, type_name, typ)
            else:
                typ = group[type_name].id
    created = h5py.h5d.create(parent.id, parts[-1].encode("utf-8"),
                              typ, source.id.get_space().copy(),
                              dcpl=creation)
    created.close()
    result = target[path]
    source_creation = source.id.get_create_plist()
    output_creation = result.id.get_create_plist()
    if (result.shape != source.shape
            or result.maxshape != source.maxshape or result.chunks != source.chunks
            or not result.id.get_type().equal(source.id.get_type())
            or output_creation.get_layout() != source_creation.get_layout()
            or (preserve_filters and (output_creation.get_nfilters() != source_creation.get_nfilters()
            or any(output_creation.get_filter(i) != source_creation.get_filter(i)
                   for i in range(source_creation.get_nfilters()))))):
        raise RecoveryError("output dataset schema differs from selected source metadata")
    return result


def _source_records(snapshot: Path, storage: StorageInspection) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with snapshot.open("rb") as handle:
        for origin, address, length, mask in storage.records:
            handle.seek(address)
            raw = handle.read(length)
            if len(raw) != length:
                raise RecoveryError("a source chunk could not be read at its reported address")
            records.append({
                "origin": list(origin), "address": address, "stored_bytes": length,
                "filter_mask": mask, "raw_sha256": hashlib.sha256(raw).hexdigest(),
            })
    return records


def _range_sha256(snapshot: Path, offset: int, size: int) -> str:
    digest = hashlib.sha256()
    with snapshot.open("rb") as handle:
        handle.seek(offset)
        remaining = size
        while remaining:
            block = handle.read(min(remaining, MAX_BLOCK_BYTES))
            if not block:
                raise RecoveryError("contiguous source payload ended before its declared length")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def _export_readable_local(source: str | Path, dataset_path: str, output: str | Path,
                           report_path: str | Path, *, hints: DatasetHints | None = None,
                           published_output: Path | None = None,
                           worker_budget: dict[str, Any] | None = None,
                           output_dataset_path: str | None = None) -> dict[str, Any]:
    """Export one bounded local dataset, marking missing chunk values unknown.

    This uses only the damaged/current file as input. It never reconstructs an
    index or verifies earlier scientific truth. Destinations must not exist.
    """
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    output_dataset_path = output_dataset_path or dataset_path
    if (not isinstance(output_dataset_path, str) or not output_dataset_path.startswith("/")
            or output_dataset_path == "/" or len(output_dataset_path.encode("utf-8")) > MAX_PATH_BYTES
            or output_dataset_path.split("/")[1] == "_h5reclaim"
            or len(output_dataset_path.split("/")) > 65
            or any(part in ("", ".", "..") for part in output_dataset_path[1:].split("/"))
            or any(ord(character) < 32 or ord(character) == 127 for character in output_dataset_path)):
        raise UnsupportedCase("output dataset path must be bounded, absolute, and canonical")
    with source_snapshot(source) as (snapshot, digest, identity, source_size):
        try:
            with h5py.File(snapshot, "r") as original:
                selected = _selected_dataset(original, dataset_path)
                dtype = selected.dtype
                reference_kind = _safe_reference_type(selected.id.get_type(), dtype)
                if reference_kind:
                    # A non-null fill reference could point to an object that
                    # will not exist in the one-dataset output.
                    fill = selected.fillvalue
                    if fill is not None and bool(fill):
                        raise UnsupportedCase("non-null reference fill value cannot be remapped")
                else:
                    _safe_fixed_type(selected.id.get_type(), dtype)
                shape = tuple(int(value) for value in selected.shape)
                if len(shape) > MAX_RANK or any(length <= 0 for length in shape):
                    raise UnsupportedCase("expected a scalar or nonempty dataset with rank at most four")
                file_itemsize = selected.id.get_type().get_size()
                logical_bytes = prod(shape) * file_itemsize
                if logical_bytes > MAX_DATA_BYTES:
                    raise UnsupportedCase("selected logical data exceeds the 512 MiB limit")
                storage = _check_storage(selected, source_size, file_itemsize)
                ownership_inventory = _check_competing_owners(snapshot, selected, storage)
                if (reference_kind and storage.layout == "chunked" and selected.chunks is not None
                        and prod(selected.chunks) > MAX_REFERENCE_ELEMENTS_PER_BLOCK):
                    raise UnsupportedCase("reference chunk exceeds the per-block element limit")
                reference_block_limit = MAX_REFERENCE_ELEMENTS_PER_BLOCK if reference_kind else None
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
                attributes, initially_omitted = _safe_export_attributes(selected)
                omitted = list(initially_omitted)
                chunk_records = _source_records(snapshot, storage)
                object_address = int(h5py.h5o.get_info(selected.id).addr)
                contiguous_address = selected.id.get_offset() if storage.layout == "contiguous" else None
                contiguous_raw_sha256 = (_range_sha256(snapshot, int(contiguous_address), logical_bytes)
                                         if contiguous_address is not None else None)
                source_values = hashlib.sha256()
                block_count = 0
                accepted_elements = 0
                null_references = self_references = 0
                with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=output.parent) as out_dir:
                    with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=report_path.parent) as rep_dir:
                        output_temp = Path(out_dir) / "output.h5"
                        report_temp = Path(rep_dir) / "report.json"
                        with h5py.File(output_temp, "x") as target:
                            exported = _create_matching_dataset(target, selected, output_dataset_path)
                            for selection in _allocated_selections(
                                    storage, shape, selected.chunks, dtype.itemsize,
                                    reference_block_limit):
                                block = (np.asarray(selected[selection]) if reference_kind else
                                         read_fixed_block(selected, selection))
                                expected_dtype = dtype if reference_kind else np.dtype(f"V{file_itemsize}")
                                if block.dtype != expected_dtype or block.nbytes > MAX_BLOCK_BYTES:
                                    raise RecoveryError("native read returned an unexpected bounded block")
                                if reference_kind:
                                    tokens, nulls, selves = _reference_tokens(
                                        block, original, selected, object_address, reference_kind)
                                    mapped = np.empty(block.shape, dtype=dtype)
                                    for index, reference in np.ndenumerate(block):
                                        if reference_kind == "region":
                                            mapped[index] = (
                                                h5py.h5r.create(exported.id, b".",
                                                                 h5py.h5r.DATASET_REGION,
                                                                 h5py.h5r.get_region(reference, original.id))
                                                if reference else h5py.RegionReference()
                                            )
                                        else:
                                            mapped[index] = exported.ref if reference else h5py.Reference()
                                    exported[selection] = mapped
                                    source_values.update(tokens)
                                    null_references += nulls
                                    self_references += selves
                                else:
                                    write_fixed_block(exported, selection, block)
                                    source_values.update(block.tobytes(order="C"))
                                block_count += 1
                                accepted_elements += block.size
                            copied_attributes: list[str] = []
                            for name, value in attributes:
                                exported.attrs[name] = value
                                old = np.asarray(value)
                                new = np.asarray(exported.attrs[name])
                                if (old.dtype != new.dtype or old.shape != new.shape
                                        or old.tobytes() != new.tobytes()
                                        or not exported.attrs.get_id(name).get_type().equal(
                                            selected.attrs.get_id(name).get_type())):
                                    del exported.attrs[name]
                                    omitted.append(name)
                                else:
                                    copied_attributes.append(name)
                            if storage.layout == "chunked":
                                validity = np.zeros(storage.grid, dtype="u1")
                                assert selected.chunks is not None
                                for origin, _, _, _ in storage.records:
                                    validity[tuple(start // width for start, width in zip(origin, selected.chunks))] = 1
                                target.require_group("/_h5reclaim").create_dataset(
                                    "validity", data=validity, dtype="u1")
                            target.flush()

                        output_values = hashlib.sha256()
                        with h5py.File(output_temp, "r") as target:
                            exported = target[output_dataset_path]
                            output_address = int(h5py.h5o.get_info(exported.id).addr)
                            for selection in _allocated_selections(
                                    storage, shape, selected.chunks, dtype.itemsize,
                                    reference_block_limit):
                                block = (np.asarray(exported[selection]) if reference_kind else
                                         read_fixed_block(exported, selection))
                                if reference_kind:
                                    tokens, _, _ = _reference_tokens(
                                        block, target, exported, output_address, reference_kind)
                                    output_values.update(tokens)
                                else:
                                    output_values.update(block.tobytes(order="C"))
                        if output_values.digest() != source_values.digest():
                            raise RecoveryError("output values or reference targets differ from the source snapshot")

                        report: dict[str, Any] = {
                            "schema_version": 1,
                            "tool": "h5reclaim",
                            "tool_version": VERSION,
                            "mode": "readable_export",
                            "outcome": "partial" if storage.unknown else "complete",
                            "source": {
                                "path": str(source), "size_bytes": source_size,
                                "sha256_before": digest, "sha256_after": digest,
                            },
                            "dataset": {
                                "path": output_dataset_path, "source_path": dataset_path,
                                "shape": list(shape), "dtype": dtype.descr if dtype.fields else dtype.str,
                                "maxshape": [item for item in selected.maxshape],
                                "chunks": list(selected.chunks) if selected.chunks else None,
                                "filters_in_order": list(observed_filters),
                                "layout": storage.layout,
                                "source_object_header_address": object_address,
                                "source_contiguous_byte_range": {
                                    "start": int(contiguous_address),
                                    "end_exclusive": int(contiguous_address) + logical_bytes,
                                } if contiguous_address is not None else None,
                                "source_contiguous_raw_sha256": contiguous_raw_sha256,
                                "allocated_chunks_checked": len(storage.records),
                                "unknown_chunk_origins": [list(origin) for origin in storage.unknown],
                                "source_chunk_records": chunk_records,
                                "logical_bytes": logical_bytes, "attributes_copied": copied_attributes,
                                "attributes_omitted": omitted,
                                "reference_mapping": {
                                    "kind": "top_level_" + reference_kind,
                                    "allowed_target": dataset_path,
                                    "null_count": null_references, "selected_dataset_count": self_references,
                                    "selections_may_include_unknown_values": bool(storage.unknown),
                                    "comparison": (
                                        "logical null/self target and encoded region selection sequence, "
                                        "not raw source reference bytes"
                                    ),
                                } if reference_kind else None,
                                "filter_semantics": (
                                    "SCALEOFFSET may be lossy; only the currently readable decoded values "
                                    "are checked against the output"
                                ) if h5py.h5z.FILTER_SCALEOFFSET in observed_filters else None,
                            },
                            "ownership_inventory": ownership_inventory,
                            "accepted_elements": accepted_elements,
                            "unknown_elements": prod(shape) - accepted_elements,
                            "validity_map": "/_h5reclaim/validity" if storage.layout == "chunked" else None,
                            "validity_codes": {"0": "unknown source chunk; ignore output values at these coordinates",
                                               "1": "allocated chunk with native read and bitwise output readback"}
                            if storage.layout == "chunked" else None,
                            "output_path": str(published_output or output),
                            "blocks_verified": block_count,
                            "native_value_sha256": source_values.hexdigest(),
                            "operator_hints": {
                                "trust_level": hints.trust_level,
                                "note": hints.note,
                                "comparisons": [vars(comparison) for comparison in comparisons],
                                "warning": "Matching hints do not prove the origin or historical value of measurements.",
                            } if hints is not None else None,
                            "verification": (
                                "all accepted object references resolve to the selected dataset or null; "
                                "output references were remapped and checked against the logical source sequence"
                                if reference_kind else
                                "all accepted values read through native HDF5 and output read back bitwise equal"
                            ),
                            "native_worker": worker_budget,
                            "limits": (
                                "This copies current native-readable values, not structurally recovered bytes. "
                                "Values at unknown chunks are not accepted measurements and must be ignored. "
                                "Allocation and round-trip checks cannot establish earlier scientific truth. "
                                "Other objects, links, dimension scales, and omitted attributes are not copied."
                            ),
                        }
                        report_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
                        with h5py.File(output_temp, "r+") as target:
                            meta = target.require_group("/_h5reclaim")
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


def _worker_message(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_WORKER_MESSAGE_BYTES:
            raise RecoveryError("native worker returned an oversized response")
        message = json.loads(path.read_bytes())
        if not isinstance(message, dict) or message.get("status") not in ("ok", "error"):
            raise ValueError("invalid worker response")
        return message
    except (OSError, ValueError, UnicodeError) as exc:
        raise RecoveryError("native worker did not return a valid bounded response") from exc


def export_readable(source: str | Path, dataset_path: str, output: str | Path,
                    report_path: str | Path, *, hints: DatasetHints | None = None,
                    timeout_seconds: float = NATIVE_WORKER_SECONDS,
                    memory_bytes: int = NATIVE_WORKER_MEMORY_BYTES) -> dict[str, Any]:
    """Copy a local dataset in a time-bounded process, publishing only after verification.

    The subprocess isolates native HDF5 crashes and hangs from the caller. On
    Linux it also limits virtual address space. On macOS the parent monitors
    process-group resident memory. On Windows a Job Object caps
    committed memory and owns the child process tree. Only a successful staged
    result is linked to the requested output and report destinations.
    """
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    if not 0 < timeout_seconds <= 24 * 3600 or not 256 * 1024 * 1024 <= memory_bytes <= 16 * 1024**3:
        raise UnsupportedCase("native worker timeout or memory budget is outside the safe range")
    if not source.is_file():
        raise RecoveryError(f"input is not a regular file: {source}")
    identity = _identity(source.stat())
    digest = sha256_file(source)
    _verify_source(source, identity, digest)

    with tempfile.TemporaryDirectory(prefix=".h5reclaim-worker-", dir=output.parent) as out_dir:
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-worker-", dir=report_path.parent) as rep_dir:
            staged_output = Path(out_dir) / "output.h5"
            staged_report = Path(rep_dir) / "report.json"
            request_path = Path(out_dir) / "request.json"
            response_path = Path(out_dir) / "response.json"
            request = {
                "source": str(source.absolute()), "dataset": dataset_path,
                "output": str(staged_output), "report": str(staged_report),
                "published_output": str(output), "memory_bytes": memory_bytes,
                "timeout_seconds": timeout_seconds,
                "hints": asdict(hints) if hints is not None else None,
            }
            request_bytes = json.dumps(request, ensure_ascii=True).encode("utf-8")
            if len(request_bytes) > MAX_WORKER_MESSAGE_BYTES:
                raise HintsError("native worker request exceeds the bounded message limit")
            request_path.write_bytes(request_bytes)
            environment = os.environ.copy()
            # HDF5's special :: value disables all dynamically loaded plugins,
            # including filters and VFDs. Built-in filters still work.
            environment["HDF5_PLUGIN_PRELOAD"] = "::"
            environment.pop("HDF5_PLUGIN_PATH", None)
            command = [sys.executable, "-m", "h5reclaim.native_worker",
                       str(request_path), str(response_path)]
            try:
                from .worker_limits import run_worker
                completed = run_worker(command, env=environment,
                                       timeout_seconds=timeout_seconds, memory_bytes=memory_bytes)
            except subprocess.TimeoutExpired as exc:
                raise RecoveryError("native HDF5 worker exceeded its time limit") from exc
            message = _worker_message(response_path) if response_path.exists() else None
            if message is None:
                raise RecoveryError("native HDF5 worker exited without a result (crash or resource limit)")
            if completed.returncode != (0 if message["status"] == "ok" else 2):
                raise RecoveryError("native HDF5 worker exited unexpectedly (crash or resource limit)")
            if message["status"] == "error":
                detail = str(message.get("detail", "native worker failed"))[:300]
                kind = message.get("kind")
                if kind == "UnsupportedCase":
                    raise UnsupportedCase(detail)
                if kind == "HintsError":
                    raise HintsError(detail)
                raise RecoveryError(detail)
            if not staged_output.is_file() or not staged_report.is_file():
                raise RecoveryError("native worker reported success without staged outputs")
            try:
                report_bytes = staged_report.read_bytes()
                if len(report_bytes) > 8 * 1024 * 1024:
                    raise RecoveryError("native export report exceeds the 8 MiB publication limit")
                result = json.loads(report_bytes)
                if (result["source"]["sha256_before"] != digest
                        or result["source"]["sha256_after"] != digest
                        or result["output_path"] != str(output)
                        or result["dataset"]["path"] != dataset_path):
                    raise RecoveryError("native worker result does not match the requested source and dataset")
            except (ValueError, KeyError, TypeError) as exc:
                raise RecoveryError("native worker report is invalid") from exc
            _verify_source(source, identity, digest)
            _validate_paths(source, output, report_path)
            published = False
            try:
                os.link(staged_output, output)
                published = True
                os.link(staged_report, report_path)
            except OSError as exc:
                if published:
                    output.unlink(missing_ok=True)
                raise RecoveryError(f"native HDF5 readable export publication failed: {exc}") from exc
            return result
