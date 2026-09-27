"""Supported dataset metadata, obtained without reading its chunk payloads."""

from __future__ import annotations

from dataclasses import dataclass
from math import prod
from pathlib import Path
from typing import Any

import h5py
import numpy as np


class UnsupportedCase(ValueError):
    """The selected file or dataset falls outside the declared support envelope."""


@dataclass(frozen=True)
class DatasetSpec:
    path: str
    object_address: int
    shape: tuple[int, ...]
    chunks: tuple[int, ...]
    dtype: str = "<u4"
    filters: tuple[int, ...] = ()
    attributes: tuple[tuple[str, Any], ...] = ()
    omitted_attributes: tuple[str, ...] = ()

    @property
    def chunk_grid(self) -> tuple[int, ...]:
        return tuple(length // chunk for length, chunk in zip(self.shape, self.chunks))

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
        if not isinstance(name, str) or len(name.encode("utf-8")) > 128 or name.startswith("h5reclaim_"):
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


def read_dataset_spec(source: Path, dataset_path: str) -> DatasetSpec:
    if not dataset_path.startswith("/"):
        raise UnsupportedCase("dataset path must be absolute, for example /measurements")
    try:
        with h5py.File(source, "r") as handle:
            selected = handle.get(dataset_path)
            if not isinstance(selected, h5py.Dataset):
                raise UnsupportedCase(f"dataset {dataset_path!r} was not found")
            if selected.name == "/_h5reclaim" or selected.name.startswith("/_h5reclaim/"):
                raise UnsupportedCase("the /_h5reclaim output namespace is reserved")
            if not Path(selected.file.filename).samefile(source):
                raise UnsupportedCase("external dataset links are not supported")

            shape = selected.shape
            chunks = selected.chunks
            if len(shape) not in (1, 2) or chunks is None or len(chunks) != len(shape):
                raise UnsupportedCase("expected a rank-one or rank-two chunked dataset")
            if selected.maxshape != shape:
                raise UnsupportedCase("extendible datasets are not supported")
            if any(length <= 0 or length % chunk for length, chunk in zip(shape, chunks)):
                raise UnsupportedCase("dimensions must be positive and divisible by chunks")
            if prod(shape) > 1_048_576:
                raise UnsupportedCase("dataset exceeds the current 1,048,576-element limit")

            datatype = selected.id.get_type()
            # Recovery copies on-disk bytes directly into a canonical <u4
            # output. A four-byte HDF5 integer can still have fewer than 32
            # significant bits, a shifted bit field, or nonstandard padding.
            # Those representations must not be interpreted as plain uint32.
            if len(shape) == 2:
                if not _canonical_uint32(datatype):
                    raise UnsupportedCase("expected canonical little-endian unsigned 32-bit integers")
                dtype = "<u4"
            else:
                if not _canonical_float64(datatype):
                    raise UnsupportedCase("expected canonical little-endian IEEE binary64 floats")
                dtype = "<f8"

            if prod(chunks) * np.dtype(dtype).itemsize > 1_048_576:
                raise UnsupportedCase("chunk exceeds the current 1 MiB limit")

            creation = selected.id.get_create_plist()
            filters = tuple(creation.get_filter(i) for i in range(creation.get_nfilters()))
            if len(shape) == 2:
                if filters:
                    raise UnsupportedCase("filtered or compressed rank-two chunks are not supported")
            elif (
                len(filters) != 2
                or filters[0][0] != h5py.h5z.FILTER_FLETCHER32
                or filters[0][1] != 0
                or filters[0][2] != ()
                or filters[1][0] != h5py.h5z.FILTER_DEFLATE
                or filters[1][1] != h5py.h5z.FLAG_OPTIONAL
                or len(filters[1][2]) != 1
                or not 0 <= filters[1][2][0] <= 9
            ):
                raise UnsupportedCase("rank-one floats require exactly Fletcher32 followed by deflate")
            if creation.get_external_count() != 0 or selected.is_virtual:
                raise UnsupportedCase("external and virtual storage are not supported")

            attributes, omitted_attributes = (
                _safe_scalar_attributes(selected) if len(shape) == 1 else ((), ())
            )

            return DatasetSpec(
                path=selected.name,
                object_address=int(h5py.h5o.get_info(selected.id).addr),
                shape=tuple(int(length) for length in shape),
                chunks=tuple(int(length) for length in chunks),
                dtype=dtype,
                filters=tuple(int(item[0]) for item in filters),
                attributes=attributes,
                omitted_attributes=omitted_attributes,
            )
    except UnsupportedCase:
        raise
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise UnsupportedCase(f"HDF5 could not open selected dataset metadata: {exc}") from exc
