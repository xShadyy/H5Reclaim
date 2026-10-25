"""Installation and optional-reader/tool diagnostics without network requests."""

from importlib import metadata
import platform
import shutil


def doctor():
    packages = {}
    for name in ("h5reclaim", "h5py", "numpy", "hdf5plugin", "pyfive", "hdf5storage", "netCDF4", "pynwb"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    import h5py
    return {"python": platform.python_version(), "platform": platform.system(),
            "hdf5": h5py.version.hdf5_version, "packages": packages,
            "tools": {name: shutil.which(name) for name in ("h5clear", "h5repack", "h5copy", "h5debug", "h5diff", "h5check")},
            "optional_installs": {"compression_codecs": "python -m pip install 'h5reclaim[filters]'",
                                  "alternative_reader": "python -m pip install 'h5reclaim[readers]'",
                                  "application_readers": "python -m pip install 'h5reclaim[applications]'"}}
