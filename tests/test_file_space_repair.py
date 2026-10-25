"""Rooted persistent file-space repair must retain independent section evidence."""

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.file_space_repair import inspect_file_space, repair_file_space
from h5reclaim.format import FormatError
from h5reclaim.repair_plan import plan_repair, apply_repair
from h5reclaim.preflight import inspect_metadata_local


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
        preflight = inspect_metadata_local(self.source)
        self.assertEqual(preflight["decision"], "accept")
        self.assertEqual(preflight["file_space"]["manager_count"], len(inventory["managers"]))

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

    def test_checked_header_repair_is_planned_and_applied_to_new_copy(self):
        manager = inspect_file_space(self.source)["managers"][0]
        self.damage(manager["address"] + 6)
        before = self.source.read_bytes()
        preflight = inspect_metadata_local(self.source)
        self.assertEqual(preflight["decision"], "reject_corrupt")
        self.assertTrue(any(finding["code"] == "CORRUPT_FILE_SPACE" for finding in preflight["findings"]))
        plan_path, output, audit = (self.root / name for name in ("plan.json", "output.h5", "audit.jsonl"))
        plan = plan_repair(self.source, plan_path)
        self.assertTrue(plan["executable"], plan)
        self.assertTrue(any("restore_file_space_header_field" in action["repairs"]
                            for action in plan["actions"]))
        apply_repair(self.source, plan_path, output, audit)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertGreater(len(inspect_file_space(output)["managers"]), 0)
        self.assertEqual(inspect_metadata_local(output)["decision"], "accept")
        with h5py.File(output, "r") as handle:
            np.testing.assert_array_equal(handle["kept"][:], np.arange(50, dtype="i4"))
        self.assertIn("repair_verified", audit.read_text())

    def test_checksum_reseal_needs_opt_in_and_intact_sections(self):
        manager = inspect_file_space(self.source)["managers"][0]
        self.damage(manager["metadata_ranges"][0][1] - 1)
        self.assertFalse(plan_repair(self.source)["executable"])
        planned = plan_repair(self.source, allow_reseal=True)
        self.assertTrue(planned["executable"], planned)
        self.assertTrue(any("reseal_file_space_header" in action["repairs"]
                            for action in planned["actions"]))

    def test_original_section_checksum_repairs_detached_section_field(self):
        manager = inspect_file_space(self.source)["managers"][0]
        section = manager["section_list_address"]
        pristine = self.source.read_bytes()
        self.damage(section + 13)
        self.assertEqual(inspect_metadata_local(self.source)["decision"], "reject_corrupt")
        plan_path, output, audit = (self.root / name for name in ("sections.json", "sections.h5", "sections.jsonl"))
        plan = plan_repair(self.source, plan_path)
        self.assertTrue(plan["executable"], plan)
        self.assertTrue(any("restore_file_space_section_field" in action["repairs"]
                            for action in plan["actions"]))
        apply_repair(self.source, plan_path, output, audit)
        self.assertEqual(output.read_bytes(), pristine)

    def test_section_checksum_reseal_requires_opt_in(self):
        manager = inspect_file_space(self.source)["managers"][0]
        section_end = manager["metadata_ranges"][1][1]
        self.damage(section_end - 1)
        self.assertFalse(plan_repair(self.source)["executable"])
        planned = plan_repair(self.source, allow_reseal=True)
        self.assertTrue(planned["executable"], planned)
        self.assertTrue(any("reseal_file_space_section_list" in action["repairs"]
                            for action in planned["actions"]))

    def test_bad_section_list_cannot_be_published_by_resealing_header(self):
        manager = inspect_file_space(self.source)["managers"][0]
        start = manager["metadata_ranges"][1][0]
        self.damage(start)
        self.damage(start + 1)
        with self.assertRaises(FormatError):
            inspect_file_space(self.source)
        with self.assertRaises(FormatError):
            repair_file_space(self.source, [], allow_reseal=True)
        self.assertFalse(plan_repair(self.source, allow_reseal=True)["executable"])

    def test_absent_persistent_managers_are_empty_inventory(self):
        basic = self.root / "default.h5"
        with h5py.File(basic, "w", libver="latest") as handle:
            handle.create_dataset("data", data=np.arange(8))
        self.assertEqual(inspect_file_space(basic)["managers"], [])


if __name__ == "__main__":
    unittest.main()
