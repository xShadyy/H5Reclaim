"""Checked group-internal names survive a missing parent group link."""

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.detached_reconstruction import detached_hierarchies
from h5reclaim.metadata_fallback import _messages
from h5reclaim.modern_indexes import ModernH5File, lookup3
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

    def test_two_detached_groups_preserve_shared_dataset_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "shared.h5"
            with h5py.File(source, "w", libver="latest") as handle:
                first = handle.create_group("first")
                second = handle.create_group("second")
                values = np.arange(24, dtype="<i4")
                first.create_dataset("measurements", data=values,
                                     chunks=(6,), compression="gzip")
                second["alias"] = first["measurements"]
                first_address = int(h5py.h5o.get_info(first.id).addr)
                second_address = int(h5py.h5o.get_info(second.id).addr)
            with ModernH5File(source) as reader:
                messages = [item for item in _messages(reader, reader.superblock.root_object_address)
                            if item.kind == 6]
                self.assertEqual(len(messages), 2)
                offset = messages[0].absolute_offset + len(messages[0].data) - reader.superblock.offset_size
            with source.open("r+b") as stream:
                stream.seek(offset)
                original = stream.read(1)
                stream.seek(offset)
                stream.write(bytes([original[0] ^ 1]))
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            output, report_path = folder / "shared-output.h5", folder / "shared-report.json"
            report = rescue_all(source, output, report_path)
            canonical = f"/recovered/group_{first_address:x}/measurements"
            alias = f"/recovered/group_{second_address:x}/alias"
            with h5py.File(output, "r") as handle:
                np.testing.assert_array_equal(handle[canonical][:], values)
                np.testing.assert_array_equal(handle[alias][:], values)
                self.assertEqual(h5py.h5o.get_info(handle[canonical].id).addr,
                                 h5py.h5o.get_info(handle[alias].id).addr)
            self.assertEqual(report["datasets_exported"], 1)
            self.assertEqual(report["inventory"]["detached_discovery"]["omitted_links"], [])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)

    def test_shared_detached_child_group_is_a_hard_link(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "shared-group.h5"
            with h5py.File(source, "w", libver="latest") as handle:
                first = handle.create_group("first")
                second = handle.create_group("second")
                shared = first.create_group("shared")
                shared.create_dataset("signal", data=np.arange(18, dtype="<i2"),
                                      chunks=(3,), compression="gzip")
                second["also_shared"] = shared
                first_address = int(h5py.h5o.get_info(first.id).addr)
                second_address = int(h5py.h5o.get_info(second.id).addr)
            with ModernH5File(source) as reader:
                messages = [item for item in _messages(reader, reader.superblock.root_object_address)
                            if item.kind == 6]
                offset = messages[0].absolute_offset + len(messages[0].data) - reader.superblock.offset_size
            with source.open("r+b") as stream:
                stream.seek(offset)
                original = stream.read(1)
                stream.seek(offset)
                stream.write(bytes([original[0] ^ 1]))
            output, report_path = folder / "out.h5", folder / "out.json"
            report = rescue_all(source, output, report_path)
            first_path = f"/recovered/group_{first_address:x}/shared"
            second_path = f"/recovered/group_{second_address:x}/also_shared"
            with h5py.File(output, "r") as handle:
                self.assertEqual(h5py.h5o.get_info(handle[first_path].id).addr,
                                 h5py.h5o.get_info(handle[second_path].id).addr)
                np.testing.assert_array_equal(handle[second_path + "/signal"][:],
                                              np.arange(18, dtype="<i2"))
            self.assertEqual(report["datasets_exported"], 1)

    def test_checked_dangling_edge_keeps_the_good_sibling_and_reports_omission(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "dangling.h5"
            with h5py.File(source, "w", libver="latest") as handle:
                group = handle.create_group("lost")
                group.create_dataset("good", data=np.arange(20, dtype="<i4"),
                                     chunks=(5,), compression="gzip")
                group.create_dataset("bad", data=np.arange(3, dtype="<i4"))
                group_address = int(h5py.h5o.get_info(group.id).addr)
            with ModernH5File(source) as reader:
                messages = _messages(reader, group_address)
                corrupted = [message for message in messages
                             if message.kind == 6 and b"bad" in message.data]
                self.assertEqual(len(corrupted), 1)
                header_range = next((start, stop) for start, stop, _ in reader.metadata_ranges
                                    if start == reader.absolute(group_address))
                target_offset = corrupted[0].absolute_offset + len(corrupted[0].data) - reader.superblock.offset_size
                root_links = [message for message in _messages(reader, reader.superblock.root_object_address)
                              if message.kind == 6]
                root_offset = root_links[0].absolute_offset + len(root_links[0].data) - reader.superblock.offset_size
            with source.open("r+b") as stream:
                stream.seek(header_range[0])
                raw = bytearray(stream.read(header_range[1] - header_range[0]))
                location = target_offset - header_range[0]
                raw[location:location + 8] = (group_address + 1).to_bytes(8, "little")
                raw[-4:] = lookup3(raw[:-4]).to_bytes(4, "little")
                stream.seek(header_range[0])
                stream.write(raw)
                stream.seek(root_offset)
                first = stream.read(1)
                stream.seek(root_offset)
                stream.write(bytes([first[0] ^ 1]))
            output, report_path = folder / "out.h5", folder / "out.json"
            report = rescue_all(source, output, report_path)
            prefix = f"/recovered/group_{group_address:x}"
            with h5py.File(output, "r") as handle:
                np.testing.assert_array_equal(handle[prefix + "/good"][:], np.arange(20, dtype="<i4"))
                self.assertNotIn(prefix + "/bad", handle)
            omissions = report["inventory"]["detached_discovery"]["omitted_links"]
            self.assertTrue(any(item.get("name") == "bad" for item in omissions))

    def test_two_checked_groups_link_one_dataset_without_fabricating_parent(self):
        discovery = {"root_object_address": 1,
            "datasets": [{"address": 200}],
            "groups": [
                {"address": 50, "links": [{"name": "a", "address": 200}]},
                {"address": 70, "links": [{"name": "b", "address": 200}]},
            ]}
        result = detached_hierarchies(discovery, set(), set(), max_objects=10, max_links=10)
        self.assertEqual([item["path"] for item in result["groups"]],
                         ["/recovered/group_32", "/recovered/group_46"])
        self.assertEqual(result["dataset_paths"], {200: "/recovered/group_32/a"})
        self.assertEqual(result["dataset_aliases"], {200: ["/recovered/group_46/b"]})
        self.assertEqual(result["omitted_links"], [])

    def test_dangling_link_is_reported_while_surviving_branch_is_retained(self):
        discovery = {"root_object_address": 1,
            "datasets": [{"address": 200}],
            "groups": [{"address": 50, "links": [{"name": "a", "address": 200},
                                                 {"name": "missing", "address": 777}]}]}
        result = detached_hierarchies(discovery, set(), set(), max_objects=10, max_links=10)
        self.assertEqual(result["dataset_paths"], {200: "/recovered/group_32/a"})
        self.assertEqual(result["omitted_links"][0]["target_address"], 777)

    def test_shared_child_group_is_an_alias_and_cycle_is_omitted(self):
        discovery = {"root_object_address": 1,
            "datasets": [{"address": 200}],
            "groups": [
                {"address": 50, "links": [{"name": "child", "address": 100}]},
                {"address": 70, "links": [{"name": "same", "address": 100}]},
                {"address": 100, "links": [{"name": "value", "address": 200},
                                            {"name": "loop", "address": 100}]},
            ]}
        result = detached_hierarchies(discovery, set(), set(), max_objects=10, max_links=10)
        self.assertEqual(result["dataset_paths"], {200: "/recovered/group_32/child/value"})
        self.assertEqual(result["group_aliases"],
                         [{"path": "/recovered/group_46/same", "target": "/recovered/group_32/child"}])
        self.assertEqual([item["reason"] for item in result["omitted_links"]],
                         ["cyclic group hard link"])

    def test_checked_link_to_already_known_dataset_keeps_one_object(self):
        discovery = {"root_object_address": 1, "datasets": [],
                     "groups": [{"address": 50, "links": [{"name": "alias", "address": 200}]}]}
        result = detached_hierarchies(discovery, set(), {200}, max_objects=10, max_links=10)
        self.assertEqual(result["dataset_paths"], {})
        self.assertEqual(result["dataset_aliases"], {200: ["/recovered/group_32/alias"]})
        self.assertEqual([item["path"] for item in result["groups"]], ["/recovered/group_32"])

    def test_link_budget_omits_remaining_links_without_unbounded_evidence(self):
        discovery = {"root_object_address": 1,
                     "datasets": [{"address": address} for address in range(200, 220)],
                     "groups": [{"address": 50,
                                 "links": [{"name": str(address), "address": address}
                                           for address in range(200, 220)]}]}
        result = detached_hierarchies(discovery, set(), set(), max_objects=10, max_links=2)
        self.assertEqual(len(result["dataset_paths"]), 2)
        self.assertEqual(len(result["omitted_links"]), 1)
        self.assertIn("uninspected", result["omitted_links"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
