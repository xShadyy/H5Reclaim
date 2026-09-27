"""Competing local allocations must defeat plausible selected chunk pointers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError, H5File
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.ownership_inventory import inventory_other_allocations
from h5reclaim.recovery import recover


class CrossDatasetOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        self.source = folder / "source.h5"
        self.output = folder / "result.h5"
        self.report = folder / "result.json"

    def test_v1_selected_chunk_redirected_to_sibling_is_refused(self) -> None:
        selected_data = np.arange(16, dtype="<u4").reshape(4, 4)
        sibling_data = selected_data + 1000
        with h5py.File(self.source, "w", libver=("earliest", "v108")) as handle:
            handle.create_dataset("selected", data=selected_data, chunks=(2, 2))
            handle.create_dataset("sibling", data=sibling_data, chunks=(2, 2))
        with h5py.File(self.source, "r") as handle, H5File(self.source) as raw:
            selected = handle["selected"]
            selected_obj = int(h5py.h5o.get_info(selected.id).addr)
            layout = raw.read_dataset_layout(selected_obj)
            root = raw.read_tree(layout.root_address)
            self.assertEqual(root.level, 0)
            pointer_offset = root.entries[0].pointer_offset
            sibling_addr = int(handle["sibling"].id.get_chunk_info_by_coord((0, 0)).byte_offset)
            self.assertEqual(sibling_addr, raw.absolute(sibling_addr))
            offset_size = raw.superblock.offset_size
        image = bytearray(self.source.read_bytes())
        image[pointer_offset:pointer_offset + offset_size] = sibling_addr.to_bytes(offset_size, "little")
        self.source.write_bytes(image)
        before = self.source.read_bytes()

        with self.assertRaisesRegex(FormatError, "overlaps rooted sibling dataset /sibling"):
            recover(self.source, "/selected", self.output, self.report)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_checksum_repaired_modern_slot_redirected_to_sibling_is_refused(self) -> None:
        selected_data = np.arange(96, dtype="<u4").reshape(8, 12)
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("selected", data=selected_data, chunks=(2, 3))
            handle.create_dataset("sibling", data=selected_data + 1000, chunks=(2, 3))
        with h5py.File(self.source, "r") as handle, ModernH5File(self.source) as raw:
            selected = handle["selected"]
            selected_obj = int(h5py.h5o.get_info(selected.id).addr)
            index = raw.read_index(selected_obj, selected.shape, selected.chunks,
                                   selected.dtype.itemsize, maxshape=selected.maxshape)
            self.assertEqual(index.index_type, "fixed_array")
            first = index.chunks[0]
            sibling_addr = int(handle["sibling"].id.get_chunk_info_by_coord((0, 0)).byte_offset)
            block_start, block_end, _ = next(
                item for item in raw.metadata_ranges if item[2] == "fixed-array data block"
            )
            pointer_width = raw.superblock.offset_size
            self.assertEqual(sibling_addr, raw.absolute(sibling_addr))
        image = bytearray(self.source.read_bytes())
        image[first.pointer_offset:first.pointer_offset + pointer_width] = sibling_addr.to_bytes(
            pointer_width, "little"
        )
        image[block_end-4:block_end] = lookup3(image[block_start:block_end-4]).to_bytes(4, "little")
        self.source.write_bytes(image)
        before = self.source.read_bytes()
        with ModernH5File(self.source) as raw:
            repaired = raw.read_index(selected_obj, (8, 12), (2, 3), 4, maxshape=(8, 12))
            self.assertEqual(repaired.chunks[0].address, sibling_addr)

        with self.assertRaisesRegex(FormatError, "overlaps rooted sibling dataset /sibling"):
            recover(self.source, "/selected", self.output, self.report)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_unrelated_contiguous_sibling_is_observed_without_false_refusal(self) -> None:
        selected_data = np.arange(16, dtype="<u4").reshape(4, 4)
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("selected", data=selected_data, chunks=(4, 4))
            handle.create_dataset("sibling", data=np.arange(10, dtype="<u4"))
        result = recover(self.source, "/selected", self.output, self.report)
        inventory = result["ownership_inventory"]
        self.assertTrue(inventory["complete"])
        self.assertEqual(inventory["sibling_datasets_seen"], 1)
        self.assertEqual(inventory["sibling_allocations_checked"], 1)
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["selected"][:], selected_data)

    def test_namespace_budget_is_reported_as_incomplete(self) -> None:
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("selected", data=np.arange(16, dtype="<u4"), chunks=(4,))
            handle.create_dataset("sibling", data=np.arange(8, dtype="<u4"), chunks=(4,))
        with h5py.File(self.source, "r") as handle:
            selected_obj = int(h5py.h5o.get_info(handle["selected"].id).addr)
        inventory = inventory_other_allocations(self.source, selected_obj, max_links=1)
        self.assertFalse(inventory.complete)
        self.assertTrue(any("link limit" in reason for reason in inventory.incomplete_reasons))


if __name__ == "__main__":
    unittest.main()
