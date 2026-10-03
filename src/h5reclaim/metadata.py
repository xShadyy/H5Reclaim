"""Supported dataset metadata, obtained without reading its chunk payloads."""

from __future__ import annotations

from dataclasses import dataclass
from math import prod
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .format import FormatError
from .schema_codec import (
    FilterDescriptor, SchemaError, canonical_numeric_dtype, fixed_file_datatype,
    read_filter_pipeline,
)


class UnsupportedCase(ValueError):
    """The selected file or dataset falls outside the declared support envelope."""


@dataclass(frozen=True)
class DatasetSpec:
    path: str
    object_address: int
    shape: tuple[int, ...]
    chunks: tuple[int, ...]
    dtype: str | np.dtype = "<u4"
    filters: tuple[int, ...] = ()
    attributes: tuple[tuple[str, Any], ...] = ()
    omitted_attributes: tuple[str, ...] = ()
    # Appended defaults retain compatibility with existing DatasetSpec
    # construction in public tests and callers. None means an older caller
    # did not supply a maximum shape, so it is interpreted as the current one.
    filter_pipeline: tuple[FilterDescriptor, ...] = ()
    maxshape: tuple[int | None, ...] | None = None
    file_type_encoding: bytes | None = None

    @property
    def chunk_grid(self) -> tuple[int, ...]:
        return tuple((length + chunk - 1) // chunk for length, chunk in zip(self.shape, self.chunks))

    @property
    def chunk_bytes(self) -> int:
        return prod(self.chunks) * np.dtype(self.dtype).itemsize


def _canonical_uint32(datatype: h5py.h5t.TypeID) -> bool:
    return (
        datatype.get_class() == h5py.h5t.INTEGER
        and datatype.get_size() == 4
        and datatype.get_sign() == h5py.h5t.SGN_NONE
        and datatype.get_order() == h5py.h5t.ORDER_LE
        and datatype.get_precision() == 32
        and datatype.get_offset() == 0
        and datatype.get_pad() == (h5py.h5t.PAD_ZERO, h5py.h5t.PAD_ZERO)
    )


def _canonical_float64(datatype: h5py.h5t.TypeID) -> bool:
    # Equivalent to IEEE binary64 in little-endian file order, including its
    # exponent bias, sign, mantissa, and implied leading mantissa bit.
    return (
        datatype.get_class() == h5py.h5t.FLOAT
        and datatype.get_size() == 8
        and datatype.get_order() == h5py.h5t.ORDER_LE
        and datatype.get_precision() == 64
        and datatype.get_offset() == 0
        and datatype.get_pad() == (h5py.h5t.PAD_ZERO, h5py.h5t.PAD_ZERO)
        and datatype.get_fields() == (63, 52, 11, 0, 52)
        and datatype.get_ebias() == 1023
        and datatype.get_norm() == h5py.h5t.NORM_IMPLIED
    )


def _safe_scalar_attributes(dataset: h5py.Dataset) -> tuple[tuple[tuple[str, Any], ...], tuple[str, ...]]:
    """Copy only bounded primitive scalar attributes from the strain dataset.

    This is a best-effort metadata copy, not a promise to reproduce a source
    file's full hierarchy. Attributes with references, arrays, and unusual
    datatypes are omitted and explicitly listed in the report.
    """
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
            datatype = attr.get_type()
            if attr.get_space().get_simple_extent_npoints() != 1 or attr.get_space().get_simple_extent_ndims() != 0:
                omitted.append(name)
                continue
            if datatype.get_class() not in (h5py.h5t.INTEGER, h5py.h5t.FLOAT, h5py.h5t.STRING):
                omitted.append(name)
                continue
            # A variable-length string stores a heap reference in the
            # attribute record. Its reported storage size does not bound the
            # bytes h5py would allocate when dereferencing that heap object.
            if datatype.get_class() == h5py.h5t.STRING and datatype.is_variable_str():
                omitted.append(name)
                continue
            if datatype.get_size() > 4096:
                omitted.append(name)
                continue
            if attr.get_storage_size() > 4096:
                omitted.append(name)
                continue
            value = dataset.attrs[name]
            if isinstance(value, (str, bytes)):
                if len(value.encode("utf-8") if isinstance(value, str) else value) > 4096:
                    omitted.append(name)
                    continue
            elif isinstance(value, (bool, int, float, np.integer, np.floating, np.bool_)):
                if np.asarray(value).dtype.kind not in "biuf" or np.asarray(value).dtype.itemsize > 8:
                    omitted.append(name)
                    continue
            else:
                omitted.append(name)
                continue
            copied.append((name, value))
        except (OSError, RuntimeError, ValueError, TypeError):
            omitted.append(name)
    return tuple(copied), tuple(omitted)


def _selected_local_dataset(handle: h5py.File, path: str) -> h5py.Dataset:
    """Resolve only canonical, local hard-link paths before opening the object.

    `handle.get('/a/b')` can follow an external link before the caller checks
    that the final dataset belongs to the snapshot. Examine each link itself
    first, without asking HDF5 to dereference soft or external targets.
    """
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise UnsupportedCase("select an absolute dataset path such as /group/data")
    if len(path.encode("utf-8")) > 4096 or any(
        ord(character) < 32 or ord(character) == 127 for character in path
    ):
        raise UnsupportedCase("selected dataset path is too long or contains control characters")
    parts = path[1:].split("/")
    if len(parts) > 64 or any(part in ("", ".", "..") for part in parts):
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


def read_dataset_spec(source: Path, dataset_path: str) -> DatasetSpec:
    try:
        with h5py.File(source, "r") as handle:
            selected = _selected_local_dataset(handle, dataset_path)
            if not Path(selected.file.filename).samefile(source):
                raise UnsupportedCase("external dataset links are not supported")

            shape = selected.shape
            chunks = selected.chunks
            if len(shape) not in (1, 2, 3, 4) or chunks is None or len(chunks) != len(shape):
                raise UnsupportedCase("expected a rank-one through rank-four chunked dataset")
            if any(length <= 0 or chunk <= 0 for length, chunk in zip(shape, chunks)):
                raise UnsupportedCase("dimensions and chunk extents must be positive")
            if prod(shape) > 1_048_576:
                raise UnsupportedCase("dataset exceeds the current 1,048,576-element limit")

            datatype = selected.id.get_type()
            try:
                # The exact encoded H5T carries enum names, fixed string
                # padding, opaque tags, and compound layout to the export.
                dtype, type_encoding = fixed_file_datatype(datatype)
                if datatype.get_class() in (h5py.h5t.INTEGER, h5py.h5t.FLOAT):
                    dtype = canonical_numeric_dtype(datatype, selected.dtype)
                    type_encoding = None
            except SchemaError as exc:
                raise UnsupportedCase(str(exc)) from exc

            if prod(chunks) * np.dtype(dtype).itemsize > 1_048_576:
                raise UnsupportedCase("chunk exceeds the current 1 MiB limit")

            creation = selected.id.get_create_plist()
            try:
                filters = read_filter_pipeline(creation, np.dtype(dtype).itemsize)
            except SchemaError as exc:
                raise UnsupportedCase(str(exc)) from exc
            if creation.get_external_count() != 0 or selected.is_virtual:
                raise UnsupportedCase("external and virtual storage are not supported")

            attributes, omitted_attributes = _safe_scalar_attributes(selected)

            # H5Oget_info on a dataset may traverse its damaged chunk index
            # and even crash in some HDF5 builds. The canonical selected path
            # has already been verified as local hard links; the final link's
            # recorded target is the object-header address we need.
            parent_path, link_name = dataset_path.rsplit("/", 1)
            parent = handle[parent_path or "/"]
            link_info = parent.id.links.get_info(link_name.encode("utf-8"))
            if link_info.type != h5py.h5l.TYPE_HARD:
                raise UnsupportedCase("selected link is not a local hard link")
            spec = DatasetSpec(
                path=selected.name,
                object_address=int(link_info.u),
                shape=tuple(int(length) for length in shape),
                chunks=tuple(int(length) for length in chunks),
                dtype=dtype,
                filters=tuple(item.id for item in filters),
                attributes=attributes,
                omitted_attributes=omitted_attributes,
                filter_pipeline=filters,
                maxshape=tuple(selected.maxshape),
                file_type_encoding=type_encoding,
            )
            # A native open can follow a valid-looking but redirected legacy
            # symbol-table entry. Require a complete bounded census of rooted
            # hard links before trusting the selected object's address.
            from .metadata_fallback import _superblock_version, verify_old_selected_address
            if _superblock_version(source) in (0, 1):
                verify_old_selected_address(source, spec.path, spec.object_address)
            return spec
    except UnsupportedCase:
        raise
    except FormatError:
        raise
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise UnsupportedCase(f"HDF5 could not open selected dataset metadata: {exc}") from exc
