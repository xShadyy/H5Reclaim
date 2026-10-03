"""Regressions for wheel layouts, native handle lifetime and consumer errors."""

import ctypes
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import h5py

from h5reclaim import native_bindings
from h5reclaim.bundle_stream import opened_bundle
from h5reclaim.large_streaming import LargeBudget
from h5reclaim.object_discovery import detached_view, open_by_address


class PlatformIOTests(unittest.TestCase):
    def test_public_apis_load_from_both_windows_wheel_layouts(self):
        for layout in ("h5py", "h5py.libs"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = root / "h5py"
                library_dir = root / layout
                package.mkdir()
                library_dir.mkdir(exist_ok=True)
                dll = library_dir / "hdf5.dll"
                dll.touch()
                exports = SimpleNamespace(**{name: Mock() for name in (
                    "H5Oopen_by_addr", "H5Tcommit2", "H5Pset_fill_value")})

                def load(path):
                    if Path(path) == dll:
                        return exports
                    raise OSError("extension does not export HDF5 APIs")

                native_bindings._library_for.cache_clear()
                try:
                    with patch.object(h5py, "__file__", str(package / "__init__.py")), \
                            patch.object(native_bindings.ctypes, "CDLL", side_effect=load):
                        for name in vars(exports):
                            function = native_bindings.public_function(name, [ctypes.c_int64])
                            self.assertIs(function, getattr(exports, name))
                finally:
                    native_bindings._library_for.cache_clear()

    def test_open_by_address_uses_same_native_ids_as_h5py(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.h5"
            with h5py.File(source, "w") as file:
                original = file.create_dataset("science", data=[3, 7, 11])
                address = h5py.h5o.get_info(original.id).addr
                selected = open_by_address(file, address)
                try:
                    self.assertEqual(selected[:].tolist(), [3, 7, 11])
                finally:
                    selected.id.close()

    def test_detached_view_propagates_consumer_error_without_retrying(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.h5"
            with h5py.File(source, "w"):
                pass
            with patch("h5reclaim.object_discovery._discovery_reader") as fallback:
                with self.assertRaisesRegex(RuntimeError, "consumer failure"):
                    with detached_view(source):
                        raise RuntimeError("consumer failure")
                fallback.assert_not_called()

    def test_bundle_closes_hdf5_before_temporary_members_are_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stem = root / "instrument"
            with h5py.File(stem, "w", driver="split", meta_ext=b"-m.h5", raw_ext=b"-r.h5") as file:
                file.create_dataset("science", data=[3, 7, 11])
            manifest = {"schema_version": 1, "driver": "split", "members": [
                {"role": role, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for role, path in (("metadata", root / "instrument-m.h5"), ("raw", root / "instrument-r.h5"))
            ]}
            real_temporary = tempfile.TemporaryDirectory
            handles = []
            checked = []
            case = self

            class GuardedDirectory:
                def __init__(self, *args, **kwargs):
                    self.bundle = kwargs.get("prefix") == "h5reclaim-bundle-"
                    self.temporary = real_temporary(*args, **kwargs)

                def __enter__(self):
                    return self.temporary.__enter__()

                def __exit__(self, *args):
                    if self.bundle:
                        checked.append(True)
                        case.assertFalse(handles[0].id.valid, "bundle member is still held open")
                    return self.temporary.__exit__(*args)

            with patch("h5reclaim.bundle_stream.tempfile.TemporaryDirectory", GuardedDirectory):
                with opened_bundle("split", manifest, LargeBudget()) as (file, *_):
                    handles.append(file)
                    self.assertEqual(file["science"][:].tolist(), [3, 7, 11])
            self.assertEqual(checked, [True])


if __name__ == "__main__":
    unittest.main()
