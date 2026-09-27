"""Dense-group name ownership from checked B-tree and fractal heap chains."""

from __future__ import annotations

import tempfile
import unittest
import hashlib
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.dense_group_links import _read_heap, read_dense_group_links
from h5reclaim.format import FormatError
from h5reclaim.metadata_fallback import _messages
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.recovery import recover


class DenseGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "dense.h5"

    def _create(self, count: int = 20) -> int:
        with h5py.File(self.path, "w", libver="latest") as handle:
            group = handle.create_group("lab")
            for n in range(count):
                group.create_dataset(f"sensor_{n:03}",
                                     data=(np.arange(4, dtype="<u4") + n).reshape(2, 2),
                                     chunks=(2, 2))
            return h5py.h5o.get_info(group.id).addr

    def _create_nested(self, count: int, name_size: int) -> tuple[int, int, np.ndarray]:
        expected = np.arange(8, dtype="<u4")
        with h5py.File(self.path, "w", libver="latest") as handle:
            group = handle.create_group("lab")
            selected = group.create_dataset("science", data=expected, chunks=(8,))
            target = h5py.h5o.get_info(selected.id).addr
            for n in range(count):
                group[f"soft_{n:04d}_" + "x" * name_size] = h5py.SoftLink("/lab/science")
            return h5py.h5o.get_info(group.id).addr, target, expected

    def _info(self, reader: ModernH5File, group_address: int):
        message = [m for m in _messages(reader, group_address) if m.kind == 2][0]
        return message, int.from_bytes(message.data[2:10], "little"), int.from_bytes(
            message.data[10:18], "little")

    def _read(self, reader: ModernH5File, group_address: int):
        info, _, _ = self._info(reader, group_address)
        return read_dense_group_links(reader, group_address=group_address,
                                      link_info=info.data, link_info_offset=info.absolute_offset)

    def test_all_links_match_native_at_depth_zero_and_deeper_nodes(self):
        for count in (20, 40, 100, 400):
            with self.subTest(count=count):
                group_address = self._create(count)
                with h5py.File(self.path, "r") as native, ModernH5File(self.path) as reader:
                    links = self._read(reader, group_address)
                    self.assertEqual(len(links), count)
                    self.assertEqual({link.name for link in links},
                                     {f"sensor_{n:03}" for n in range(count)})
                    for link in links:
                        self.assertEqual(link.object_address,
                                         h5py.h5o.get_info(native[f"lab/{link.name}"].id).addr)
                        self.assertGreaterEqual(link.heap_record_offset, 0)
                    self.assertTrue(any(kind == "dense FHDB" for _, _, kind in reader.metadata_ranges))
                    if count >= 40:
                        self.assertTrue(any(kind == "dense root FHIB"
                                            for _, _, kind in reader.metadata_ranges))

    def test_child_and_descendant_indirect_blocks_match_native_links(self):
        for count, name_size, expected_label in (
            (3000, 200, "dense child FHIB"),
            (3800, 1200, "dense descendant FHIB"),
        ):
            with self.subTest(count=count):
                address, target, _ = self._create_nested(count, name_size)
                with ModernH5File(self.path) as reader:
                    links = self._read(reader, address)
                    self.assertEqual(len(links), count + 1)
                    self.assertEqual([link.object_address for link in links
                                      if link.name == "science"], [target])
                    self.assertEqual(sum(link.object_address is None for link in links), count)
                    self.assertTrue(any(kind == expected_label for _, _, kind
                                        in reader.metadata_ranges))

    def test_child_indirect_offset_contradiction_refuses_after_valid_checksum(self):
        address, _, _ = self._create_nested(3000, 200)
        with ModernH5File(self.path) as reader:
            self._read(reader, address)
            child_start, child_end, _ = next(entry for entry in reader.metadata_ranges
                                             if entry[2] == "dense child FHIB")
            offset_byte = child_start + 5 + reader.superblock.offset_size
        raw = bytearray(self.path.read_bytes())
        raw[offset_byte] ^= 1
        raw[child_end-4:child_end] = lookup3(raw[child_start:child_end-4]).to_bytes(4, "little")
        self.path.write_bytes(raw)
        with ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(FormatError, "child FHIB"):
                self._read(reader, address)

    def test_redirected_child_pointer_refuses_even_with_repaired_parent_checksum(self):
        address, _, _ = self._create_nested(3000, 200)
        with ModernH5File(self.path) as reader:
            self._read(reader, address)
            children = [entry for entry in reader.metadata_ranges
                        if entry[2] == "dense child FHIB"]
            root_start, root_end, _ = next(entry for entry in reader.metadata_ranges
                                           if entry[2] == "dense root FHIB")
            self.assertGreaterEqual(len(children), 2)
            old, replacement = (child[0] for child in children[:2])
            width = reader.superblock.offset_size
        raw = bytearray(self.path.read_bytes())
        location = raw.find(old.to_bytes(width, "little"), root_start, root_end-4)
        self.assertGreaterEqual(location, root_start)
        raw[location:location+width] = replacement.to_bytes(width, "little")
        raw[root_end-4:root_end] = lookup3(raw[root_start:root_end-4]).to_bytes(4, "little")
        self.path.write_bytes(raw)
        with ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(FormatError, "indirect block reused|child FHIB"):
                self._read(reader, address)

    def test_nested_dense_group_recovers_selected_native_inaccessible_dataset(self):
        address, target, expected = self._create_nested(3000, 200)
        with ModernH5File(self.path) as reader:
            message = next(m for m in _messages(reader, target) if m.kind == 5)
        raw = bytearray(self.path.read_bytes())
        raw[message.absolute_offset] = 255  # Invalid optional fill message blocks native open.
        flags = raw[target + 5]
        size_width = 1 << (flags & 3)
        prefix = 6 + (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0) + size_width
        chunk_size = int.from_bytes(raw[target+prefix-size_width:target+prefix], "little")
        end = target + prefix + chunk_size
        raw[end:end+4] = lookup3(raw[target:end]).to_bytes(4, "little")
        self.path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        with h5py.File(self.path) as handle:
            with self.assertRaises((KeyError, OSError)):
                handle["/lab/science"]
        output, report_path = self.path.with_name("recovered.h5"), self.path.with_name("report.json")
        report = recover(self.path, "/lab/science", output, report_path)
        self.assertEqual(report["counts"]["recovered"], 1)
        self.assertEqual(report["metadata_resolution"]["route"],
                         "checksummed_modern_dense_hard_links")
        with h5py.File(output) as handle:
            np.testing.assert_array_equal(handle["/lab/science"][:], expected)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), digest)

    def test_unrelated_soft_link_remains_explicitly_nonhard(self):
        group_address = self._create()
        with h5py.File(self.path, "r+") as handle:
            handle["lab/soft"] = h5py.SoftLink("/lab/sensor_000")
        with ModernH5File(self.path) as reader:
            links = self._read(reader, group_address)
            self.assertIsNone([link for link in links if link.name == "soft"][0].object_address)

    def test_deleted_group_link_does_not_gain_stale_heap_ownership(self):
        group_address = self._create(40)
        with h5py.File(self.path, "r+") as handle:
            del handle["lab/sensor_019"]
        with ModernH5File(self.path) as reader:
            links = self._read(reader, group_address)
            self.assertEqual(len(links), 39)
            self.assertNotIn("sensor_019", {link.name for link in links})

    def test_corrupt_btree_header_checksum_refuses(self):
        group_address = self._create()
        with ModernH5File(self.path) as reader:
            _, _, index = self._info(reader, group_address)
        raw = bytearray(self.path.read_bytes()); raw[index+12] ^= 0x40
        self.path.write_bytes(raw)
        with ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(FormatError, "name index header checksum"):
                self._read(reader, group_address)

    def test_corrupt_direct_block_checksum_refuses(self):
        group_address = self._create()
        with ModernH5File(self.path) as reader:
            _, heap, _ = self._info(reader, group_address)
            root = int.from_bytes(reader.read_at(heap+132, 8), "little")
        raw = bytearray(self.path.read_bytes()); raw[root+34] ^= 0x01
        self.path.write_bytes(raw)
        with ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(FormatError, "FHDB checksum"):
                self._read(reader, group_address)

    def test_corrupt_indirect_heap_pointer_checksum_refuses(self):
        group_address = self._create(100)
        with ModernH5File(self.path) as reader:
            _, heap, _ = self._info(reader, group_address)
            root = int.from_bytes(reader.read_at(heap+132, 8), "little")
            self.assertEqual(reader.read_at(root, 4), b"FHIB")
        raw = bytearray(self.path.read_bytes()); raw[root+22] ^= 0x08
        self.path.write_bytes(raw)
        with ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(FormatError, "root FHIB"):
                self._read(reader, group_address)

    def test_corrupt_internal_name_tree_node_refuses(self):
        group_address = self._create(100)
        with ModernH5File(self.path) as reader:
            _, _, index = self._info(reader, group_address)
            header = reader.read_at(index, 38)
            self.assertGreater(int.from_bytes(header[12:14], "little"), 0)
            root = int.from_bytes(header[16:24], "little")
        raw = bytearray(self.path.read_bytes()); raw[root+7] ^= 0x01
        self.path.write_bytes(raw)
        with ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(FormatError, "name-index node checksum"):
                self._read(reader, group_address)

    def test_rechecks_name_hash_even_when_tree_checksum_is_recomputed(self):
        group_address = self._create()
        with ModernH5File(self.path) as reader:
            _, _, index = self._info(reader, group_address)
            header = reader.read_at(index, 38)
            root = int.from_bytes(header[16:24], "little")
            count = int.from_bytes(header[24:26], "little")
        self.assertEqual(count, 20)
        raw = bytearray(self.path.read_bytes())
        raw[root+6] ^= 0x01  # first indexed name hash, not the heap link
        checksum = root + 6 + count*11
        raw[checksum:checksum+4] = lookup3(raw[root:checksum]).to_bytes(4, "little")
        self.path.write_bytes(raw)
        with ModernH5File(self.path) as reader:
            with self.assertRaises(FormatError):
                self._read(reader, group_address)

    def test_link_info_must_belong_to_requested_rooted_group(self):
        group_address = self._create()
        with h5py.File(self.path, "r+") as handle:
            handle.create_group("other")
        with ModernH5File(self.path) as reader:
            root = reader.superblock.root_object_address
            _messages(reader, root)
            info, _, _ = self._info(reader, group_address)
            with self.assertRaisesRegex(FormatError, "outside validated group header"):
                read_dense_group_links(reader, group_address=root,
                                       link_info=info.data, link_info_offset=info.absolute_offset)


if __name__ == "__main__":
    unittest.main()
