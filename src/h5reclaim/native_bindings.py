"""Access public APIs in the HDF5 library already linked by installed h5py."""

from __future__ import annotations

import ctypes
from functools import lru_cache
from pathlib import Path

import h5py

from .metadata import UnsupportedCase


@lru_cache(maxsize=None)
def _library_for(name):
    paths = [Path(h5py.h5o.__file__), Path(h5py.h5p.__file__)]
    paths.extend((Path(h5py.__file__).parent.parent / 'h5py.libs').glob('*hdf5*.dll'))
    for path in paths:
        try:
            library = ctypes.CDLL(str(path))
            getattr(library, name)
            return library
        except (OSError, AttributeError):
            continue
    raise UnsupportedCase(f'the installed HDF5 library does not expose {name}')


def public_function(name, argtypes, restype=ctypes.c_int):
    function = getattr(_library_for(name), name)
    function.argtypes, function.restype = argtypes, restype
    return function
