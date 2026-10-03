"""Access public APIs in the HDF5 library already linked by installed h5py."""

from __future__ import annotations

import ctypes
from functools import lru_cache
from pathlib import Path

import h5py

from .metadata import UnsupportedCase


@lru_cache(maxsize=None)
def _library_for(name):
    package = Path(h5py.__file__).parent
    paths = [Path(h5py.h5o.__file__), Path(h5py.h5p.__file__)]
    # Windows wheels ship hdf5.dll beside the extension modules. Older
    # wheels use h5py.libs; Unix wheels may use a private .libs directory.
    # Stay within h5py's installation so IDs never cross HDF5 installations.
    for directory in (package, package / '.libs', package.parent / 'h5py.libs'):
        for pattern in ('*hdf5*.dll', '*hdf5*.so*', '*hdf5*.dylib'):
            paths.extend(sorted(directory.glob(pattern)))
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
