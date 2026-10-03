"""Add tool convenience attributes without replacing source attributes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import h5py


def metadata_group_for_path(path: str) -> str:
    """Choose an annotation root which cannot contain the selected data."""
    first = path.lstrip('/').split('/', 1)[0]
    return '/_h5reclaim_metadata' if first == '_h5reclaim' else '/_h5reclaim'


def report_metadata_group(report: Mapping[str, Any]) -> str:
    return str(report.get('metadata_group', '/_h5reclaim'))


def available_metadata_group(paths, parent='/') -> str:
    """Keep all source-owned top-level names, including earlier tool outputs."""
    prefix = parent.rstrip('/') + '/'
    names = {path[len(prefix):].split('/', 1)[0] for path in paths if path.startswith(prefix)}
    name, number = '_h5reclaim', 0
    while name in names:
        number += 1
        name = f'_h5reclaim_metadata_{number}'
    return prefix + name


def ensure_group(handle, path):
    """Create groups with dense attributes and tracked creation order available."""
    group = handle['/']
    for part in path.strip('/').split('/') if path != '/' else []:
        group = group[part] if part in group else group.create_group(part, track_order=True)
        if not isinstance(group, h5py.Group):
            raise ValueError('output group path crosses a dataset or datatype')
    return group


def commit_type(group, name, datatype):
    """Commit a type with support for large, ordered attributes."""
    import ctypes
    from .native_bindings import public_function
    creation = datatype.get_create_plist()
    creation.set_attr_creation_order(h5py.h5p.CRT_ORDER_TRACKED | h5py.h5p.CRT_ORDER_INDEXED)
    function = public_function('H5Tcommit2', [ctypes.c_int64, ctypes.c_char_p, ctypes.c_int64,
                                            ctypes.c_int64, ctypes.c_int64, ctypes.c_int64])
    if function(group.id.id, name, datatype.id, 0, creation.id, 0) < 0:
        raise ValueError('could not create the recovered committed datatype')


def add_output_annotations(dataset: h5py.Dataset, values: Mapping[str, Any]) -> list[str]:
    """Write available names and return source-owned names left untouched.

    The validity datasets under ``/_h5reclaim`` remain the authoritative
    status even when a scientist already used one of these attribute names.
    Callers should include returned names in their recovery report.
    """
    collisions = sorted(name for name in values if name in dataset.attrs)
    for name, value in values.items():
        if name not in collisions:
            dataset.attrs[name] = value
    return collisions
