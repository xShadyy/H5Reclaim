"""Deeper v1 trees bridge one complete, two-sided internal subtree or refuse."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import H5File
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.recovery import recover
from h5reclaim.survey import survey


class DeepTreeTests(unittest.TestCase):
    @staticmethod
    def _fixture(directory: Path) -> tuple[Path, np.ndarray, int, int, int]:
        original = directory / "pristine.h5"
        values = np.random.default_rng(714).integers(
            0, 2**32, size=(64, 64), dtype=np.uint32,
        ).astype("<u4")
        with h5py.File(original, "x", libver=("earliest", "v108")) as handle:
            handle.create_dataset("measurements", data=values, chunks=(1, 1))
        with h5py.File(original, "r") as handle, H5File(original) as reader:
            address = int(h5py.h5o.get_info(handle["measurements"].id).addr)
            root = reader.read_tree(reader.read_dataset_layout(address).root_address)
            if root.level != 2:
                raise AssertionError("test fixture must have a level-two root")
            walk = reader.walk_tree(root.address)
            parent = next(node for node in walk.nodes if node.level == 1 and len(node.entries) >= 3)
            missing_leaf = parent.entries[1]
            if missing_leaf.address is None:
                raise AssertionError("test fixture leaf is unexpectedly missing")
            left = reader.read_tree(parent.entries[0].address)
            right = reader.read_tree(parent.entries[2].address)
            candidate = reader.read_tree(missing_leaf.address)
            if not (left.right_sibling == candidate.address == right.left_sibling
                    and candidate.left_sibling == left.address
                    and candidate.right_sibling == right.address):
                raise AssertionError("test fixture has no two-sided leaf bridge")
            return original, values, missing_leaf.pointer_offset, root.entries[0].pointer_offset, reader.superblock.offset_size

    @staticmethod
    def _internal_fixture(directory: Path) -> tuple[Path, np.ndarray, int, int, int, int]:
        original = directory / "pristine.h5"
        values = np.random.default_rng(416).integers(
            0, 2**32, size=(64, 128), dtype=np.uint32,
        ).astype("<u4")
        with h5py.File(original, "x", libver=("earliest", "v108")) as handle:
            handle.create_dataset("measurements", data=values, chunks=(1, 1))
        with h5py.File(original, "r") as handle, H5File(original) as reader:
            address = int(h5py.h5o.get_info(handle["measurements"].id).addr)
            root = reader.read_tree(reader.read_dataset_layout(address).root_address)
            if root.level != 2 or len(root.entries) < 3:
                raise AssertionError("test requires three rooted level-one children")
            left, middle, right = (
                reader.read_tree(root.entries[i].address) for i in range(3)
            )
            if not (left.right_sibling == middle.address == right.left_sibling
                    and middle.left_sibling == left.address
                    and middle.right_sibling == right.address):
                raise AssertionError("test fixture has no two-sided internal bridge")
            return (original, values, root.entries[1].pointer_offset,
                    right.address, middle.address, reader.superblock.offset_size)

    def test_deeper_tree_one_lost_leaf_is_exactly_attributed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original, values, leaf_pointer, _root_pointer, width = self._fixture(base)
            damaged = base / "damaged.h5"
            shutil.copyfile(original, damaged)
            before_original = hashlib.sha256(original.read_bytes()).hexdigest()
            with damaged.open("r+b") as handle:
                handle.seek(leaf_pointer)
                handle.write(b"\xff" * width)
            before_damaged = hashlib.sha256(damaged.read_bytes()).hexdigest()
            selected = next(item for item in survey(damaged)["datasets"]
                            if item["selected_path"] == "/measurements")
            self.assertEqual(selected["support"]["status"], "candidate")
            output, report_path = base / "recovered.h5", base / "report.json"
            result = recover(damaged, "/measurements", output, report_path)
            self.assertTrue(result["complete"])
            self.assertEqual(result["index"]["root_level"], 2)
            self.assertEqual(result["counts"]["recovered"], 4096)
            self.assertGreater(result["reconstructed_chunks"], 0)
            for mapping in result["mappings"]:
                self.assertEqual(mapping["evidence"]["root_level"], 2)
            with h5py.File(output, "r") as recovered:
                np.testing.assert_array_equal(recovered["/measurements"][...], values)
                np.testing.assert_array_equal(
                    recovered["/_h5reclaim/chunk_status"][...], np.ones((64, 64), dtype="u1")
                )
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), before_original)
            self.assertEqual(hashlib.sha256(damaged.read_bytes()).hexdigest(), before_damaged)

    def test_missing_internal_subtree_refuses_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original, _values, _leaf_pointer, root_pointer, width = self._fixture(base)
            damaged = base / "damaged.h5"
            shutil.copyfile(original, damaged)
            with damaged.open("r+b") as handle:
                handle.seek(root_pointer)
                handle.write(b"\xff" * width)
            output, report_path = base / "recovered.h5", base / "report.json"
            # Native HDF5 may refuse the selected object's metadata before
            # our raw-tree guard sees a destroyed root-to-internal link.
            with self.assertRaises(UnsupportedCase):
                recover(damaged, "/measurements", output, report_path)
            self.assertFalse(output.exists())
            self.assertFalse(report_path.exists())

    def test_middle_internal_subtree_is_rooted_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original, values, pointer, _right, _middle, width = self._internal_fixture(base)
            damaged = base / "damaged.h5"
            shutil.copyfile(original, damaged)
            healthy_hash = hashlib.sha256(original.read_bytes()).hexdigest()
            with damaged.open("r+b") as handle:
                handle.seek(pointer)
                handle.write(b"\xff" * width)
            damaged_hash = hashlib.sha256(damaged.read_bytes()).hexdigest()
            selected = next(item for item in survey(damaged)["datasets"]
                            if item["selected_path"] == "/measurements")
            self.assertEqual(selected["support"]["status"], "candidate")
            output, report_path = base / "recovered.h5", base / "report.json"
            result = recover(damaged, "/measurements", output, report_path)
            self.assertTrue(result["complete"])
            self.assertEqual(result["counts"]["recovered"], 8192)
            self.assertEqual(result["index"]["reconstructed_internal_subtrees"], 1)
            self.assertGreater(result["reconstructed_chunks"], 0)
            self.assertEqual(sum(item["kind"] == "bridged_index"
                                 for item in result["evidence_ledger"]["links"]), 1)
            bridged = [item for item in result["mappings"]
                       if item["route"] == "reconstructed_link"]
            self.assertEqual(len(bridged), result["reconstructed_chunks"])
            self.assertTrue(all(item["evidence"]["subtree_root_address"] == _middle
                                for item in bridged))
            with h5py.File(output, "r") as recovered:
                np.testing.assert_array_equal(recovered["/measurements"][...], values)
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), healthy_hash)
            self.assertEqual(hashlib.sha256(damaged.read_bytes()).hexdigest(), damaged_hash)

    def test_internal_bridge_without_reciprocal_sibling_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original, _values, pointer, right, _middle, width = self._internal_fixture(base)
            damaged = base / "damaged.h5"
            shutil.copyfile(original, damaged)
            with damaged.open("r+b") as handle:
                handle.seek(pointer)
                handle.write(b"\xff" * width)
                handle.seek(right + 8)  # right neighbor's left-sibling field
                handle.write(b"\xff" * width)
            output, report_path = base / "recovered.h5", base / "report.json"
            with self.assertRaisesRegex(UnsupportedCase, "no unique two-sided rooted bridge"):
                recover(damaged, "/measurements", output, report_path)
            self.assertFalse(output.exists())
            self.assertFalse(report_path.exists())

    def test_internal_bridge_with_second_broken_child_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original, _values, pointer, _right, middle, width = self._internal_fixture(base)
            damaged = base / "damaged.h5"
            shutil.copyfile(original, damaged)
            with H5File(damaged) as reader:
                descendant_pointer = reader.read_tree(middle).entries[0].pointer_offset
            with damaged.open("r+b") as handle:
                for target in (pointer, descendant_pointer):
                    handle.seek(target)
                    handle.write(b"\xff" * width)
            output, report_path = base / "recovered.h5", base / "report.json"
            with self.assertRaisesRegex(UnsupportedCase, "another broken child link"):
                recover(damaged, "/measurements", output, report_path)
            self.assertFalse(output.exists())
            self.assertFalse(report_path.exists())

    def test_structural_chunk_limit_remains_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "too_many.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset("measurements", shape=(64, 129), dtype="<u4",
                                      chunks=(1, 1))
            output, report_path = base / "recovered.h5", base / "report.json"
            with self.assertRaisesRegex(UnsupportedCase, "8192-chunk limit"):
                recover(source, "/measurements", output, report_path)
            self.assertFalse(output.exists())
            self.assertFalse(report_path.exists())


if __name__ == "__main__":
    unittest.main()
