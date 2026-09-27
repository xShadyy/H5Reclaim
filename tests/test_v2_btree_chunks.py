"""Differential and adversarial checks for modern B-tree chunk attribution."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.v2_btree_chunks import read_v2_btree_chunks


class V2BTreeChunkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "science.h5"

    def fixture(self, *, filtered=False, shape=(60, 60), sparse=False):
        with h5py.File(self.path, "w", libver="latest") as handle:
            ds = handle.create_dataset(
                "measurements", shape=shape, maxshape=(None, None),
                chunks=(2, 2), dtype="<u4", compression="gzip" if filtered else None,
            )
            if sparse:
                ds[0:2, 0:2] = 17
                ds[-2:, -2:] = 91
            else:
                ds[...] = np.arange(shape[0] * shape[1], dtype="<u4").reshape(shape)

    def inspect(self):
        with h5py.File(self.path, "r") as handle, ModernH5File(self.path) as reader:
            dataset = handle["measurements"]
            obj = h5py.h5o.get_info(dataset.id).addr
            layout, layout_offset = reader._parse_object_layout(obj)
            kind_offset = 5 + layout[3] * layout[4]
            self.assertEqual(layout[kind_offset], 5)  # Index type 5: v2 B-tree.
            root = int.from_bytes(layout[-8:], "little")
            pipeline = dataset.id.get_create_plist()
            filters = tuple(pipeline.get_filter(i)[0]
                            for i in range(pipeline.get_nfilters()))
            index = read_v2_btree_chunks(
                reader, object_address=obj, index_address=root,
                layout_version=layout[0], layout_pointer_offset=layout_offset + len(layout) - 8,
                shape=dataset.shape, chunk_shape=dataset.chunks,
                element_size=dataset.dtype.itemsize, filters=filters,
                maxshape=dataset.maxshape,
            )
            self.assertEqual(index.index_type, "v2_btree")
            self.assertEqual(len(index.chunks), dataset.id.get_num_chunks())
            for record in index.chunks:
                native = dataset.id.get_chunk_info_by_coord(record.coordinate)
                self.assertEqual(
                    (record.coordinate, reader.absolute(record.address),
                     record.size, record.filter_mask),
                    (native.chunk_offset, native.byte_offset,
                     native.size, native.filter_mask),
                )
                self.assertEqual(reader.read_at(record.address, record.size),
                                 dataset.id.read_direct_chunk(record.coordinate)[1])
                for link in record.evidence["link_path"]:
                    self.assertEqual(int.from_bytes(
                        reader._read_absolute(link["pointer_offset"],
                                              link["pointer_length"]), "little"
                    ), link["target_address"])
                    self.assertTrue(link["checksum_verified"])
            return obj, root, index, len(reader.metadata_ranges)

    def _parse_again(self, obj, root, *, filters=(), shape=(60, 60)):
        with ModernH5File(self.path) as reader:
            layout, layout_offset = reader._parse_object_layout(obj)
            return read_v2_btree_chunks(
                reader, object_address=obj, index_address=root,
                layout_version=layout[0], layout_pointer_offset=layout_offset + len(layout) - 8,
                shape=shape, chunk_shape=(2, 2), element_size=4, filters=filters,
                maxshape=(None, None),
            )

    def test_unfiltered_internal_and_leaf_records_match_native(self):
        self.fixture()
        _, _, index, metadata_count = self.inspect()
        self.assertEqual(len(index.chunks), 900)
        self.assertGreater(metadata_count, 3)
        self.assertTrue(any(len(record.evidence["node_chain"]) == 2
                            for record in index.chunks))

    def test_filtered_internal_and_leaf_records_match_native(self):
        self.fixture(filtered=True)
        _, _, index, _ = self.inspect()
        self.assertTrue(all(record.evidence["tree_type"] == 11 for record in index.chunks))
        self.assertTrue(any(record.size != 16 for record in index.chunks))

    def test_deep_tree_count_widths_match_native(self):
        self.fixture(shape=(180, 180))
        _, root, index, _ = self.inspect()
        with ModernH5File(self.path) as reader:
            header = reader.read_at(root, 38)
        self.assertEqual(int.from_bytes(header[12:14], "little"), 2)
        self.assertEqual(len(index.chunks), 8100)
        self.assertTrue(any(len(record.evidence["node_chain"]) == 3
                            for record in index.chunks))

    def test_sparse_index_uses_only_observed_records(self):
        self.fixture(sparse=True)
        _, _, index, _ = self.inspect()
        self.assertEqual([record.coordinate for record in index.chunks],
                         [(0, 0), (58, 58)])

    def test_filtered_per_chunk_skip_mask_is_preserved(self):
        self.fixture(filtered=True, sparse=True)
        with h5py.File(self.path, "r+") as handle:
            ds = handle["measurements"]
            payload = np.arange(4, dtype="<u4").tobytes()
            ds.id.write_direct_chunk((2, 0), payload, filter_mask=1)
        _, _, index, _ = self.inspect()
        self.assertEqual(index.chunks[1].coordinate, (2, 0))
        self.assertEqual(index.chunks[1].filter_mask, 1)
        self.assertEqual(index.chunks[1].size, 16)

    def test_header_checksum_mismatch_refuses(self):
        self.fixture()
        obj, root, _, _ = self.inspect()
        raw = bytearray(self.path.read_bytes())
        raw[root + 34] ^= 0x80
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "checksum mismatch"):
            self._parse_again(obj, root)

    def test_bthd_root_link_restores_original_checksum_and_public_export(self):
        self.fixture(filtered=True)
        obj, root, healthy, _ = self.inspect()
        before = self.path.read_bytes()
        raw = bytearray(before)
        raw[root + 16] ^= 0x80
        self.path.write_bytes(raw)
        damaged = self._parse_again(obj, root, filters=(1,))
        self.assertEqual(len(damaged.chunks), len(healthy.chunks))
        self.assertEqual(damaged.reconstructed_links[0]["kind"], "bthd_to_root")
        self.assertEqual(damaged.reconstructed_links[0]["pointer_offset"], root + 16)
        from h5reclaim.recovery import recover
        report = recover(
            self.path, "/measurements", Path(self.path.parent) / "recovered.h5",
            Path(self.path.parent) / "recovered.json")
        self.assertEqual(report["counts"]["recovered"], len(healthy.chunks))
        self.assertTrue(report["structural_repair"])
        self.assertEqual(report["unresolved_links"][0]["kind"], "bthd_to_root")
        self.assertEqual(self.path.read_bytes(), bytes(raw))

    def test_btin_child_link_restores_original_checksum(self):
        self.fixture()
        obj, root, healthy, _ = self.inspect()
        first_root = healthy.chunks[0].evidence["node_chain"][0]
        header = self.path.read_bytes()[root:root+38]
        count = int.from_bytes(header[24:26], "little")
        child_pointer = first_root + 6 + count * 24
        raw = bytearray(self.path.read_bytes())
        raw[child_pointer] ^= 0x80
        self.path.write_bytes(raw)
        repaired = self._parse_again(obj, root)
        self.assertEqual(len(repaired.chunks), len(healthy.chunks))
        self.assertEqual(repaired.reconstructed_links[0]["kind"], "btin_to_child")
        self.assertEqual(repaired.reconstructed_links[0]["pointer_offset"], child_pointer)
        from h5reclaim.recovery import recover
        report = recover(
            self.path, "/measurements", Path(self.path.parent) / "btin_recovered.h5",
            Path(self.path.parent) / "btin_recovered.json")
        self.assertEqual(report["counts"]["recovered"], len(healthy.chunks))
        self.assertGreater(report["reconstructed_chunks"], 0)
        self.assertLess(report["reconstructed_chunks"], len(healthy.chunks))

    def test_depth_two_internal_link_repairs_checked_internal_child(self):
        self.fixture(shape=(180, 180))
        obj, root, healthy, _ = self.inspect()
        self.assertEqual(len(healthy.chunks), 8100)
        path = next(item.evidence["link_path"] for item in healthy.chunks
                    if len(item.evidence["link_path"]) == 3)
        broken_at = path[1]["pointer_offset"]
        raw = bytearray(self.path.read_bytes())
        raw[broken_at] ^= 0x40
        self.path.write_bytes(raw)
        repaired = self._parse_again(obj, root, shape=(180, 180))
        self.assertEqual(len(repaired.chunks), len(healthy.chunks))
        self.assertEqual(repaired.reconstructed_links[0]["kind"], "btin_to_child")
        self.assertEqual(repaired.reconstructed_links[0]["target_address"], path[1]["target_address"])

    def test_pointer_repair_refuses_two_faults_and_corrupt_candidate(self):
        self.fixture()
        obj, root, healthy, _ = self.inspect()
        child = healthy.chunks[0].evidence["node_chain"][0]
        header = self.path.read_bytes()[root:root+38]
        count = int.from_bytes(header[24:26], "little")
        child_pointer = child + 6 + count * 24
        original = self.path.read_bytes()
        cases = (
            (root+16, root+26),  # Root address and total records.
            (root+16, child+8),  # Root address and root node data.
            (child_pointer, child+8),  # Child pointer and unrelated node data.
        )
        for positions in cases:
            with self.subTest(positions=positions):
                raw = bytearray(original)
                for at in positions:
                    raw[at] ^= 1
                self.path.write_bytes(raw)
                with self.assertRaises(FormatError):
                    self._parse_again(obj, root)

    def test_leaf_checksum_mismatch_refuses_all_records(self):
        self.fixture()
        obj, root, index, _ = self.inspect()
        leaf = index.chunks[0].evidence["node_address"]
        raw = bytearray(self.path.read_bytes())
        raw[leaf + 8] ^= 0x80
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "node checksum"):
            self._parse_again(obj, root)

    def test_valid_checksum_but_out_of_range_coordinate_refuses(self):
        self.fixture(sparse=True)
        obj, root, index, _ = self.inspect()
        first = index.chunks[0]
        # Root is a leaf with two records: its checksum follows both.
        record_address = first.pointer_offset
        checksum_address = first.evidence["node_address"] + 6 + 2 * 24
        raw = bytearray(self.path.read_bytes())
        raw[record_address + 8:record_address + 16] = (1000).to_bytes(8, "little")
        node_start = first.evidence["node_address"]
        raw[checksum_address:checksum_address + 4] = lookup3(
            raw[node_start:checksum_address]
        ).to_bytes(4, "little")
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "coordinate outside"):
            self._parse_again(obj, root)

    def test_valid_checksum_but_duplicate_coordinate_refuses(self):
        self.fixture(sparse=True)
        obj, root, index, _ = self.inspect()
        first, second = index.chunks
        raw = bytearray(self.path.read_bytes())
        # Preserve a valid node checksum after forging a duplicate key.
        raw[second.pointer_offset + 8:second.pointer_offset + 24] = (
            raw[first.pointer_offset + 8:first.pointer_offset + 24]
        )
        node_start = first.evidence["node_address"]
        checksum_address = node_start + 6 + 2 * 24
        raw[checksum_address:checksum_address + 4] = lookup3(
            raw[node_start:checksum_address]
        ).to_bytes(4, "little")
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "repeat or are out of order"):
            self._parse_again(obj, root)

    def test_valid_header_checksum_but_incorrect_total_refuses(self):
        self.fixture()
        obj, root, _, _ = self.inspect()
        raw = bytearray(self.path.read_bytes())
        raw[root + 26:root + 34] = (899).to_bytes(8, "little")
        raw[root + 34:root + 38] = lookup3(raw[root:root + 34]).to_bytes(4, "little")
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "total contradicts header"):
            self._parse_again(obj, root)

    def test_rechecksummed_child_pointer_into_metadata_refuses(self):
        self.fixture()
        obj, root, index, _ = self.inspect()
        root_node = index.chunks[0].evidence["node_chain"][0]
        header = self.path.read_bytes()[root:root + 38]
        count = int.from_bytes(header[24:26], "little")
        first_child_pointer = root_node + 6 + count * 24
        checksum_address = first_child_pointer + (count + 1) * 9
        raw = bytearray(self.path.read_bytes())
        raw[first_child_pointer:first_child_pointer + 8] = root.to_bytes(8, "little")
        raw[checksum_address:checksum_address + 4] = lookup3(
            raw[root_node:checksum_address]
        ).to_bytes(4, "little")
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "metadata overlaps"):
            self._parse_again(obj, root)

    def test_caller_supplied_foreign_layout_pointer_refuses(self):
        self.fixture(sparse=True)
        obj, root, _, _ = self.inspect()
        with ModernH5File(self.path) as reader:
            reader._parse_object_layout(obj)
            with self.assertRaisesRegex(FormatError, "layout pointer"):
                read_v2_btree_chunks(
                    reader, object_address=obj, index_address=root,
                    layout_version=4, layout_pointer_offset=obj,
                    shape=(60, 60), chunk_shape=(2, 2), element_size=4, filters=(),
                )

    def test_maximum_extents_contradiction_refuses(self):
        self.fixture(sparse=True)
        obj, root, _, _ = self.inspect()
        with ModernH5File(self.path) as reader:
            layout, layout_offset = reader._parse_object_layout(obj)
            with self.assertRaisesRegex(FormatError, "multiple unlimited"):
                read_v2_btree_chunks(
                    reader, object_address=obj, index_address=root,
                    layout_version=layout[0],
                    layout_pointer_offset=layout_offset + len(layout) - 8,
                    shape=(60, 60), chunk_shape=(2, 2), element_size=4,
                    filters=(), maxshape=(None, 60),
                )


if __name__ == "__main__":
    unittest.main()
