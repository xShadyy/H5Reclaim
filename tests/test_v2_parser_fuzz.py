"""Small checksum-repaired v2 B-tree node mutation regression sampler."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError, UnsupportedFormat
from h5reclaim.modern_indexes import ModernH5File, lookup3


class V2NodeFuzzTests(unittest.TestCase):
    def test_64_valid_checksum_leaf_mutations_stay_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pristine, changed = (Path(directory) / name for name in ("pristine.h5", "changed.h5"))
            with h5py.File(pristine, "x", libver="latest") as file:
                selected = file.create_dataset("science", shape=(11, 7), maxshape=(None, None),
                                               chunks=(4, 3), dtype="<u4")
                selected[:4, :3] = np.arange(12, dtype="<u4").reshape(4, 3)
                selected[8:11, 6:7] = np.arange(3, dtype="<u4").reshape(3, 1)
            with h5py.File(pristine, "r") as native, ModernH5File(pristine) as reader:
                selected = native["/science"]
                object_address = int(h5py.h5o.get_info(selected.id).addr)
                index = reader.read_index(object_address, selected.shape, selected.chunks,
                                          selected.dtype.itemsize, maxshape=selected.maxshape)
                self.assertEqual(index.index_type, "v2_btree")
                self.assertEqual(len(index.chunks), 2)
                start = index.chunks[0].evidence["node_address"]
                self.assertEqual(start, index.chunks[1].evidence["node_address"])
                checksum = start + 6 + 2 * 24
                metadata = reader.metadata_ranges[:]
            healthy = pristine.read_bytes()
            rng = random.Random(5114)
            accepted = refused = 0
            for _ in range(64):
                raw = bytearray(healthy)
                position = rng.randrange(start + 6, checksum)
                raw[position] ^= 1 << rng.randrange(8)
                raw[checksum:checksum+4] = lookup3(raw[start:checksum]).to_bytes(4, "little")
                changed.write_bytes(raw)
                try:
                    with ModernH5File(changed) as parser:
                        candidate = parser.read_index(object_address, (11, 7), (4, 3), 4,
                                                      maxshape=(None, None), max_chunks=4096)
                except (FormatError, UnsupportedFormat):
                    refused += 1
                    continue
                accepted += 1
                origins = [record.coordinate for record in candidate.chunks]
                self.assertEqual(len(origins), len(set(origins)))
                for record in candidate.chunks:
                    self.assertTrue(all(0 <= coordinate < bound and coordinate % width == 0
                                        for coordinate, bound, width in zip(
                                            record.coordinate, (11, 7), (4, 3))))
                    absolute = parser.absolute(record.address)
                    self.assertTrue(0 <= absolute < absolute + record.size <= len(raw))
                    self.assertTrue(all(absolute + record.size <= low or high <= absolute
                                        for low, high, _kind in metadata))
            self.assertGreater(refused, 0)
            self.assertEqual(accepted + refused, 64)


if __name__ == "__main__":
    unittest.main()
