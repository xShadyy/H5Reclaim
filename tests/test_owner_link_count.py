"""A redirected selected hard link must not hide a competing object owner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError, H5File
from h5reclaim.metadata_fallback import read_dataset_spec_fallback
from h5reclaim.ownership_inventory import inventory_other_allocations
from h5reclaim.readable_export import export_readable
from h5reclaim.recovery import RecoveryError


class RootedLinkCountTests(unittest.TestCase):
    def test_redirected_selected_link_into_aliased_sibling_refuses_native_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, report = root / "damaged.h5", root / "out.h5", root / "out.json"
            with h5py.File(source, "w", libver="earliest") as file:
                lab = file.create_group("lab")
                lab.create_dataset("science", data=np.arange(16, dtype="<u4"), chunks=(4,))
                distractor = lab.create_dataset("distractor", data=np.full(16, 99, dtype="<u4"),
                                                chunks=(4,))
                file.create_group("elsewhere")["alias"] = distractor
                distractor_address = int(h5py.h5o.get_info(distractor.id).addr)
            selected = read_dataset_spec_fallback(source, "/lab/science")
            step = selected.link_chain[-1]
            with H5File(source) as reader:
                osize, lsize = reader.superblock.offset_size, reader.superblock.length_size
            raw = bytearray(source.read_bytes())
            start = step.link_message_offset + lsize
            raw[start:start + osize] = distractor_address.to_bytes(osize, "little")
            source.write_bytes(raw)
            with h5py.File(source, "r") as file:
                self.assertTrue(np.all(file["/lab/science"][:] == 99))
            with self.assertRaisesRegex(FormatError, "hard links.*exceed"):
                inventory_other_allocations(source, distractor_address)
            with self.assertRaisesRegex(RecoveryError, "hard links.*exceed"):
                export_readable(source, "/lab/science", output, report)
            self.assertEqual(source.read_bytes(), raw)
            self.assertFalse(output.exists() or report.exists())

    def test_valid_hard_link_aliases_remain_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, report = root / "source.h5", root / "out.h5", root / "out.json"
            with h5py.File(source, "w", libver="earliest") as file:
                data = file.create_dataset("science", data=np.arange(8, dtype="<i4"))
                file["alias"] = data
            with h5py.File(source, "r") as file:
                address = int(h5py.h5o.get_info(file["science"].id).addr)
            inventory = inventory_other_allocations(source, address)
            self.assertTrue(inventory.complete)
            self.assertGreaterEqual(inventory.link_targets_checked, 1)
            found = export_readable(source, "/science", output, report)
            self.assertEqual(found["accepted_elements"], 8)
            with h5py.File(output, "r") as file:
                np.testing.assert_array_equal(file["science"][:], np.arange(8, dtype="<i4"))


if __name__ == "__main__":
    unittest.main()
