"""Repair only a hash-pinned input under a reproducible reviewed byte plan."""

import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.metadata import read_dataset_spec
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.preflight import preflight
from h5reclaim.repair_plan import plan_repair, apply_repair, load_plan


class RepairPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.output, self.plan, self.audit = (self.root / n for n in ("source.h5", "repaired.h5", "plan.json", "audit.jsonl"))
        self.truth = np.arange(120, dtype="i4").reshape(12, 10)
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("data", data=self.truth, chunks=(4, 5), compression="gzip", fletcher32=True)

    def damage(self, at):
        with self.source.open("r+b") as stream:
            stream.seek(at)
            old = stream.read(1)
            stream.seek(at)
            stream.write(bytes([old[0] ^ 0x40]))

    def check_output(self):
        self.assertIn(preflight(self.output)["decision"], ("accept", "accept_with_warnings"))
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["data"][:], self.truth)

    def test_index_pointer_plan_and_audited_new_copy(self):
        spec = read_dataset_spec(self.source, "/data")
        with ModernH5File(self.source) as reader:
            index = reader.read_index(spec.object_address, spec.shape, spec.chunks, 4,
                maxshape=spec.maxshape, filters=spec.filters)
        self.damage(index.data_block_pointer_offset)
        before = self.source.read_bytes()
        plan = plan_repair(self.source, self.plan)
        self.assertTrue(plan["executable"], plan)
        self.assertEqual(plan["actions"][0]["repairs"], ["fahd_to_fadb"])
        apply_repair(self.source, self.plan, self.output, self.audit)
        self.assertEqual(before, self.source.read_bytes())
        self.check_output()
        audit = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual(audit[-1]["event"], "repair_verified")

    def test_checksum_only_repair_requires_explicit_reseal_and_preserves_values(self):
        self.damage(44)
        refused = plan_repair(self.source)
        self.assertFalse(refused["executable"])
        plan = plan_repair(self.source, self.plan, allow_reseal=True)
        self.assertTrue(plan["executable"], plan)
        self.assertIn("recomputed", plan["actions"][0]["evidence"][0])
        apply_repair(self.source, self.plan, self.output, self.audit)
        self.check_output()

    def test_edited_plan_and_changed_input_are_refused(self):
        self.damage(0)
        plan_repair(self.source, self.plan)
        changed = json.loads(self.plan.read_text())
        changed["actions"][0]["after_hex"] = "00"
        self.plan.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            apply_repair(self.source, self.plan, self.output, self.audit)
        self.assertFalse(self.output.exists())

    def test_stale_flags_are_cleared_only_on_new_copy(self):
        raw = bytearray(self.source.read_bytes())
        raw[11] = 5
        raw[44:48] = lookup3(raw[:44]).to_bytes(4, "little")
        self.source.write_bytes(raw)
        plan = plan_repair(self.source, self.plan)
        self.assertTrue(plan["executable"], plan)
        apply_repair(self.source, self.plan, self.output, self.audit)
        self.assertEqual(self.source.read_bytes()[11], 5)
        self.check_output()

    def test_object_header_checksum_resealing(self):
        with h5py.File(self.source, "r") as handle:
            root = handle["/"]
            address = int(root.id.links.get_info(b"data").u)
        with ModernH5File(self.source) as reader:
            prefix = reader.read_at(address, 6)
            extra = (16 if prefix[5] & 32 else 0) + (4 if prefix[5] & 16 else 0)
            width = 1 << (prefix[5] & 3)
            size = int.from_bytes(reader.read_at(address + 6 + extra, width), "little")
            checksum = address + 6 + extra + width + size
        self.damage(checksum)
        plan = plan_repair(self.source, self.plan, allow_reseal=True)
        self.assertTrue(plan["executable"], plan)
        apply_repair(self.source, self.plan, self.output, self.audit)
        self.check_output()

    def test_layout_owned_index_header_checksum_resealing(self):
        spec = read_dataset_spec(self.source, "/data")
        with ModernH5File(self.source) as reader:
            index = reader.read_index(spec.object_address, spec.shape, spec.chunks, 4,
                maxshape=spec.maxshape, filters=spec.filters)
        self.damage(index.base_address + 24)
        plan = plan_repair(self.source, self.plan, allow_reseal=True)
        self.assertTrue(plan["executable"], plan)
        apply_repair(self.source, self.plan, self.output, self.audit)
        self.check_output()


if __name__ == "__main__":
    unittest.main()
