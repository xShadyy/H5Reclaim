"""Structural recovery must not claim checksum evidence it does not possess."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import H5File
from h5reclaim.recovery import STATUS_CODES, recover
from tools.make_broken_link_fixture import make_damage


class IntegrityLimitsTest(unittest.TestCase):
    def test_changed_payload_is_exported_with_explicit_unchecked_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pristine, damaged = root / "clean.h5", root / "damaged.h5"
            with h5py.File(pristine, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset(
                    "measurements",
                    data=np.arange(144, dtype="<u4").reshape(12, 12),
                    chunks=(1, 1),
                )
            manifest = make_damage(pristine, damaged, root / "truth.json", "/measurements")
            with H5File(damaged) as reader:
                leaf = reader.read_tree(manifest["affected_leaf_address"])
                entry = leaf.entries[0]
                absolute = reader.absolute(entry.address)
                row, col = entry.key.offsets[:2]
            changed = bytearray(damaged.read_bytes())
            changed[absolute] ^= 0x01
            damaged.write_bytes(changed)

            output, report_path = root / "out.h5", root / "report.json"
            report = recover(damaged, "/measurements", output, report_path)
            with h5py.File(pristine, "r") as reference, h5py.File(output, "r") as result:
                embedded = json.loads(result["/_h5reclaim/report_json"][()])
                self.assertEqual(embedded, report)
                self.assertNotEqual(
                    int(reference["/measurements"][row, col]),
                    int(result["/measurements"][row, col]),
                )
                self.assertEqual(
                    int(result["/_h5reclaim/chunk_status"][row, col]),
                    STATUS_CODES["recovered"],
                )
            self.assertTrue(report["complete"])
            self.assertIn("historical measurement integrity is not established", report["integrity_note"])
            mapping = next(item for item in report["mappings"] if item["coordinate"] == [row, col])
            self.assertEqual(mapping["integrity"], "not_checked_no_checksum")


if __name__ == "__main__":
    unittest.main()
