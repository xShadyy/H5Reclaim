"""Dense-group name ownership from checked B-tree and fractal heap chains."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.dense_group_links import read_dense_group_links
from h5reclaim.format import FormatError
from h5reclaim.metadata_fallback import _messages
from h5reclaim.modern_indexes import ModernH5File, lookup3


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
