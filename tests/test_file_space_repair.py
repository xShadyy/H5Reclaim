"""Rooted persistent file-space repair must retain independent section evidence."""

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.file_space_repair import inspect_file_space, repair_file_space
from h5reclaim.format import FormatError


class FileSpaceRepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "space.h5"
        with h5py.File(self.source, "w", libver="latest", fs_strategy="fsm", fs_persist=True) as handle:
            handle.create_dataset("deleted", data=np.arange(5000, dtype="i4"), chunks=(50,), compression="gzip")
            handle.create_dataset("kept", data=np.arange(50, dtype="i4"))
            del handle["deleted"]

    def damage(self, at):
        with self.source.open("r+b") as stream:
            stream.seek(at)
            value = stream.read(1)
            stream.seek(at)
            stream.write(bytes([value[0] ^ 0x40]))

    def test_rooted_file_space_sections_are_checked(self):
        inventory = inspect_file_space(self.source)
        self.assertGreater(len(inventory["managers"]), 0)
        self.assertTrue(all(item["serialized_sections"] > 0 for item in inventory["managers"]))
        self.assertTrue(any("section list" in item[2] for item in inventory["metadata_ranges"]))

    def test_original_checksum_restores_one_header_field(self):
        manager = inspect_file_space(self.source)["managers"][0]
        pristine = self.source.read_bytes()
        self.damage(manager["address"] + 6)
        with self.assertRaises(FormatError):
            inspect_file_space(self.source)
        touches = []
        repair_file_space(self.source, touches)
        self.assertEqual(self.source.read_bytes(), pristine)
        self.assertEqual(touches[0][2:], ("restore_file_space_header_field", "original checksum restored"))

    def test_bad_section_list_cannot_be_published_by_resealing_header(self):
        manager = inspect_file_space(self.source)["managers"][0]
        start = manager["metadata_ranges"][1][0]
        self.damage(start + 15)
        with self.assertRaises(FormatError):
            inspect_file_space(self.source)
        with self.assertRaises(FormatError):
            repair_file_space(self.source, [], allow_reseal=True)

    def test_absent_persistent_managers_are_empty_inventory(self):
        basic = self.root / "default.h5"
        with h5py.File(basic, "w", libver="latest") as handle:
            handle.create_dataset("data", data=np.arange(8))
        self.assertEqual(inspect_file_space(basic)["managers"], [])


if __name__ == "__main__":
    unittest.main()
