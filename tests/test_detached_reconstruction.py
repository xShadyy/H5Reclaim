"""Checked group-internal names survive a missing parent group link."""

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.detached_reconstruction import detached_hierarchies
from h5reclaim.metadata_fallback import _messages
from h5reclaim.modern_indexes import ModernH5File
from h5reclaim.object_discovery import _validate_candidates, scan_local
from h5reclaim.large_streaming import LargeBudget
from h5reclaim.whole_file import rescue_all


class DetachedHierarchyTests(unittest.TestCase):
    def _damaged_group_link(self, folder):
        source = folder / "damaged.h5"
        with h5py.File(source, "w", libver="latest") as handle:
            group = handle.create_group("experiment")
            group.create_dataset("sensor", data=np.arange(42, dtype="<i4"),
                                 chunks=(7,), compression="gzip")
            group.create_dataset("calibration", data=np.arange(8, dtype="<i2"),
                                 chunks=(4,), compression="gzip")
            group_address = int(h5py.h5o.get_info(group.id).addr)
        with ModernH5File(source) as reader:
            messages = [item for item in _messages(reader, reader.superblock.root_object_address)
                        if item.kind == 6]
            self.assertEqual(len(messages), 1)
            # Alter only the root link to the group. Its original object
            # header checksum becomes invalid, while the detached group and
            # both dataset object headers still check independently.
            offset = messages[0].absolute_offset + len(messages[0].data) - reader.superblock.offset_size
        with source.open("r+b") as stream:
            stream.seek(offset)
            value = stream.read(1)
            stream.seek(offset)
            stream.write(bytes([value[0] ^ 1]))
        return source, group_address

    def test_two_dataset_links_reconstructed_under_synthetic_detached_group(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, address = self._damaged_group_link(folder)
            original_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            candidates = _validate_candidates(source, scan_local(source), LargeBudget())
            groups = {item["address"]: item for item in candidates["groups"]}
            self.assertEqual({item["name"] for item in groups[address]["links"]},
                             {"sensor", "calibration"})
            output, report_path = folder / "output.h5", folder / "report.json"
            report = rescue_all(source, output, report_path)
            prefix = f"/recovered/group_{address:x}"
            with h5py.File(output) as handle:
                np.testing.assert_array_equal(handle[prefix + "/sensor"][:],
                                              np.arange(42, dtype="<i4"))
                np.testing.assert_array_equal(handle[prefix + "/calibration"][:],
                                              np.arange(8, dtype="<i2"))
                self.assertNotIn("/experiment", handle)
            self.assertEqual(report["datasets_exported"], 2)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_digest)

    def test_ambiguous_group_owners_do_not_assign_a_child_path(self):
        discovery = {"root_object_address": 1,
            "datasets": [{"address": 200}],
            "groups": [
                {"address": 50, "links": [{"name": "a", "address": 200}]},
                {"address": 70, "links": [{"name": "b", "address": 200}]},
            ]}
        result = detached_hierarchies(discovery, set(), set(), max_objects=10, max_links=10)
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["dataset_paths"], {})

    def test_dangling_link_disqualifies_entire_group_candidate(self):
        discovery = {"root_object_address": 1,
            "datasets": [{"address": 200}],
            "groups": [{"address": 50, "links": [{"name": "a", "address": 200},
                                                 {"name": "missing", "address": 777}]}]}
        result = detached_hierarchies(discovery, set(), set(), max_objects=10, max_links=10)
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["dataset_paths"], {})


if __name__ == "__main__":
    unittest.main()
