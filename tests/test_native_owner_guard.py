"""Native reads must not bless a selected pointer into a sibling allocation."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import H5File
from h5reclaim.family_bundle import export_family
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.readable_export import export_readable
from h5reclaim.split_bundle import export_split
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


def _redirect_v1_tree_pointer(members: list[Path], selected_address: int,
                              sibling_address: int) -> None:
    old = selected_address.to_bytes(8, "little")
    new = sibling_address.to_bytes(8, "little")
    positions = []
    for path in members:
        image = path.read_bytes()
        for index in range(len(image)):
            if image.startswith(b"TREE", index) and image[index + 56:index + 64] == old:
                positions.append((path, index + 56))
    if len(positions) != 1:
        raise AssertionError(f"expected one selected leaf pointer, found {positions}")
    path, offset = positions[0]
    image = bytearray(path.read_bytes())
    image[offset:offset + 8] = new
    path.write_bytes(image)


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

    def test_family_refuses_redirected_chunk_in_virtual_address_space(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            template = str(folder / "run%03d.h5")
            with h5py.File(template, "w", driver="family", memb_size=1024,
                           libver="earliest") as handle:
                handle.create_dataset("selected", data=np.arange(16, dtype="<u4").reshape(4, 4), chunks=(2, 2))
                handle.create_dataset("sibling", data=np.arange(1000, 1016, dtype="<u4").reshape(4, 4), chunks=(2, 2))
            with h5py.File(template, "r", driver="family", memb_size=1024) as handle:
                selected = int(handle["selected"].id.get_chunk_info_by_coord((0, 0)).byte_offset)
                sibling = int(handle["sibling"].id.get_chunk_info_by_coord((0, 0)).byte_offset)
            members = sorted(folder.glob("run[0-9][0-9][0-9].h5"))
            _redirect_v1_tree_pointer(members, selected, sibling)
            manifest = {"schema_version": 1, "member_size": 1024, "members": [
                {"index": index, "path": str(member),
                 "sha256": hashlib.sha256(member.read_bytes()).hexdigest()}
                for index, member in enumerate(members)
            ]}
            output, report = folder / "output.h5", folder / "report.json"
            with self.assertRaisesRegex(UnsupportedCase, "overlaps rooted sibling dataset /sibling"):
                export_family(manifest, "/selected", output, report)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_split_refuses_redirected_chunk_in_virtual_address_space(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            stem = folder / "instrument"
            metadata, raw = folder / "instrument-m.h5", folder / "instrument-r.h5"
            with h5py.File(stem, "w", driver="split", meta_ext=b"-m.h5", raw_ext=b"-r.h5",
                           libver="earliest") as handle:
                handle.create_dataset("selected", data=np.arange(16, dtype="<u4").reshape(4, 4), chunks=(2, 2))
                handle.create_dataset("sibling", data=np.arange(1000, 1016, dtype="<u4").reshape(4, 4), chunks=(2, 2))
            with h5py.File(stem, "r", driver="split", meta_ext=b"-m.h5", raw_ext=b"-r.h5") as handle:
                selected = int(handle["selected"].id.get_chunk_info_by_coord((0, 0)).byte_offset)
                sibling = int(handle["sibling"].id.get_chunk_info_by_coord((0, 0)).byte_offset)
            _redirect_v1_tree_pointer([metadata], selected, sibling)
            manifest = {"schema_version": 1, "driver": "split", "members": [
                {"role": role, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for role, path in (("metadata", metadata), ("raw", raw))
            ]}
            output, report = folder / "output.h5", folder / "report.json"
            with self.assertRaisesRegex(UnsupportedCase, "overlaps rooted sibling dataset /sibling"):
                export_split(manifest, "/selected", output, report)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
