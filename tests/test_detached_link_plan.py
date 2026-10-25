"""A surviving checked dataset is reattached only through its original link checksum."""

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.modern_indexes import ModernH5File
from h5reclaim.preflight import inspect_metadata_local
from h5reclaim.repair_plan import (
    _object_header, _compact_root_link_fields, plan_repair, apply_repair,
)


class DetachedCompactLinkPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "damaged.h5"
        self.truth = np.arange(512, dtype="i4")
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("detached", data=self.truth, chunks=(64,), compression="gzip")
        with ModernH5File(self.source) as reader:
            start, raw = _object_header(reader, reader.superblock.root_object_address)
            fields = _compact_root_link_fields(reader, raw)
        self.assertEqual(len(fields), 8)
        self.pointer = start + fields[0]

    def damage(self, offset, difference=0x40):
        with self.source.open("r+b") as stream:
            stream.seek(offset)
            before = stream.read(1)
            stream.seek(offset)
            stream.write(bytes([before[0] ^ difference]))

    def test_one_lost_parent_link_is_restored_to_checked_detached_dataset(self):
        self.damage(self.pointer)
        original = self.source.read_bytes()
        self.assertEqual(inspect_metadata_local(self.source)["decision"], "reject_corrupt")
        planned = self.root / "repair.json"
        result = plan_repair(self.source, planned)
        self.assertTrue(result["executable"], result)
        self.assertEqual(result["actions"][0]["repairs"], ["restore_detached_compact_hard_link"])
        output, audit = self.root / "repaired.h5", self.root / "audit.jsonl"
        apply_repair(self.source, planned, output, audit)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(inspect_metadata_local(output)["decision"], "accept")
        with h5py.File(output, "r") as handle:
            np.testing.assert_array_equal(handle["/detached"][:], self.truth)

    def test_multiple_corrupted_parent_pointer_bytes_are_not_guessed(self):
        self.damage(self.pointer)
        self.damage(self.pointer + 1)
        result = plan_repair(self.source)
        self.assertFalse(result["executable"])
        self.assertNotIn("restore_detached_compact_hard_link",
                         {kind for action in result["actions"] for kind in action["repairs"]})

    def test_second_compact_link_uses_its_own_checked_target(self):
        with h5py.File(self.source, "r+", libver="latest") as handle:
            handle.create_dataset("second", data=np.arange(20, dtype="i4"))
        with ModernH5File(self.source) as reader:
            start, raw = _object_header(reader, reader.superblock.root_object_address)
            pointers = _compact_root_link_fields(reader, raw)
        self.assertEqual(len(pointers), 16)
        self.damage(start + pointers[8])
        planned = self.root / "second-plan.json"
        result = plan_repair(self.source, planned)
        self.assertTrue(result["executable"], result)
        apply_repair(self.source, planned, self.root / "second-output.h5", self.root / "second-audit.jsonl")
        with h5py.File(self.root / "second-output.h5", "r") as handle:
            np.testing.assert_array_equal(handle["detached"][:], self.truth)
            np.testing.assert_array_equal(handle["second"][:], np.arange(20, dtype="i4"))


if __name__ == "__main__":
    unittest.main()
