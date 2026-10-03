"""Read and write fixed HDF5 records without changing their memory layout."""

from __future__ import annotations

import h5py
import numpy as np


def copy_fixed_fill(source_creation, target_creation, datatype):
    """Copy fixed fill bytes through the file type, including unusual widths."""
    import ctypes
    from .logical_types import contains_pointers
    from .native_bindings import public_function
    if contains_pointers(datatype):
        raise ValueError('raw fill copying cannot hold heap pointers')
    setter = public_function('H5Pset_fill_value', [ctypes.c_int64, ctypes.c_int64, ctypes.c_void_p])
    if source_creation.fill_value_defined() == h5py.h5d.FILL_VALUE_UNDEFINED:
        if setter(target_creation.id, datatype.id, None) < 0:
            raise ValueError('HDF5 could not retain undefined fill')
        return
    value = np.empty((), dtype=f'V{datatype.get_size()}')
    getter = public_function('H5Pget_fill_value', [ctypes.c_int64, ctypes.c_int64, ctypes.c_void_p])
    if getter(source_creation.id, datatype.id, value.ctypes.data) < 0 or setter(target_creation.id, datatype.id, value.ctypes.data) < 0:
        raise ValueError('HDF5 could not copy fixed fill bytes')


def _spaces(dataset: h5py.Dataset, selection: tuple[slice, ...]):
    datatype = dataset.id.get_type()
    from .logical_types import contains_pointers
    if contains_pointers(datatype):
        raise ValueError("raw fixed-record I/O cannot hold heap pointers or references")
    shape = dataset.shape
    if shape is None or len(selection) != len(shape):
        raise ValueError("block selection does not match the dataset rank")
    if not shape:
        return datatype, dataset.id.get_space(), h5py.h5s.create(h5py.h5s.SCALAR), ()
    starts, counts = [], []
    for part, size in zip(selection, shape):
        if not isinstance(part, slice) or part.step not in (None, 1):
            raise ValueError("fixed-record blocks require unit-stride slices")
        start = 0 if part.start is None else part.start
        end = size if part.stop is None else part.stop
        if not 0 <= start < end <= size:
            raise ValueError("fixed-record block lies outside the dataset")
        starts.append(start)
        counts.append(end - start)
    file_space = dataset.id.get_space()
    file_space.select_hyperslab(tuple(starts), tuple(counts))
    memory_space = h5py.h5s.create_simple(tuple(counts))
    return datatype, file_space, memory_space, tuple(counts)


def read_fixed_block(dataset: h5py.Dataset, selection: tuple[slice, ...]) -> np.ndarray:
    """Return exact fixed records, including endian order and record padding."""
    datatype, file_space, memory_space, shape = _spaces(dataset, selection)
    block = np.empty(shape, dtype=np.dtype(f"V{datatype.get_size()}"))
    dataset.id.read(memory_space, file_space, block, mtype=datatype)
    return block


def write_fixed_block(dataset: h5py.Dataset, selection: tuple[slice, ...],
                      block: np.ndarray) -> None:
    datatype, file_space, memory_space, shape = _spaces(dataset, selection)
    if (block.shape != shape or block.dtype != np.dtype(f"V{datatype.get_size()}")
            or not block.flags.c_contiguous):
        raise ValueError("fixed-record output block has a different shape or width")
    dataset.id.write(memory_space, file_space, block, mtype=datatype)
