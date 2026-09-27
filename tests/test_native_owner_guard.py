"""Native reads must not bless a selected pointer into a sibling allocation."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import H5File
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.readable_export import export_readable
from h5reclaim.vds_export import export_vds


def _redirect_first_chunk(source: Path) -> None:
    with h5py.File(source, "r") as handle, H5File(source) as raw:
        selected_address = int(h5py.h5o.get_info(handle["selected"].id).addr)
        index = raw.read_tree(raw.read_dataset_layout(selected_address).root_address)
        assert index.level == 0
        pointer_offset = index.entries[0].pointer_offset
        sibling_address = int(handle["sibling"].id.get_chunk_info_by_coord((0, 0)).byte_offset)
        offset_size = raw.superblock.offset_size
    image = bytearray(source.read_bytes())
    image[pointer_offset:pointer_offset + offset_size] = sibling_address.to_bytes(offset_size, "little")
    source.write_bytes(image)


class NativeOwnerGuardTests(unittest.TestCase):
    def test_readable_copy_refuses_redirected_chunk_even_when_native_read_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, output, report = (folder / name for name in ("source.h5", "output.h5", "report.json"))
            with h5py.File(source, "w", libver=("earliest", "v108")) as handle:
                handle.create_dataset("selected", data=np.arange(16, dtype="<u4").reshape(4, 4), chunks=(2, 2))
                handle.create_dataset("sibling", data=np.arange(1000, 1016, dtype="<u4").reshape(4, 4), chunks=(2, 2))
            _redirect_first_chunk(source)
            before = source.read_bytes()
            with h5py.File(source, "r") as handle:
                self.assertEqual(handle["selected"][0, 0], 1000)  # Native HDF5 follows the wrong pointer.
            with self.assertRaisesRegex(UnsupportedCase, "overlaps rooted sibling dataset /sibling"):
                export_readable(source, "/selected", output, report)
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_vds_refuses_source_chunk_redirected_into_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, virtual, output, report = (folder / name for name in
                                               ("source.h5", "virtual.h5", "output.h5", "report.json"))
            with h5py.File(source, "w", libver=("earliest", "v108")) as handle:
                handle.create_dataset("selected", data=np.arange(16, dtype="<u4").reshape(4, 4), chunks=(2, 2))
                handle.create_dataset("sibling", data=np.arange(1000, 1016, dtype="<u4").reshape(4, 4), chunks=(2, 2))
            layout = h5py.VirtualLayout(shape=(4, 4), dtype="<u4")
            layout[:] = h5py.VirtualSource("source.h5", "/selected", shape=(4, 4))
            with h5py.File(virtual, "w") as handle:
                handle.create_virtual_dataset("observations", layout)
            _redirect_first_chunk(source)
            manifest = {"schema_version": 1, "files": [{
                "declared_name": "source.h5", "path": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }]}
            with self.assertRaisesRegex(UnsupportedCase, "overlaps rooted sibling dataset /sibling"):
                export_vds(virtual, "/observations", output, report, manifest)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
