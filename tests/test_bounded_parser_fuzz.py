"""Seeded mutations with repaired checksums exercise bounded parser decisions.

This is a regression sampler, not formal fuzzing or an exhaustive proof of
memory safety. It targets plausible, checksummed corruption rather than only
random bytes that fail at the first checksum.
"""

from __future__ import annotations

import random
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

from run_stratified_layouts import _generate  # noqa: E402
from h5reclaim.format import FormatError, UnsupportedFormat  # noqa: E402
from h5reclaim.modern_indexes import ModernH5File, lookup3  # noqa: E402


class BoundedParserFuzzTests(unittest.TestCase):
    def test_rechecks_valid_array_checksum_after_96_seeded_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            pristine, mutant = folder / "pristine.h5", folder / "mutant.h5"
            _generate(pristine, "fixed_array", np.random.default_rng(501))
            with h5py.File(pristine, "r") as native, ModernH5File(pristine) as reader:
                selected = native["/science"]
                object_address = int(h5py.h5o.get_info(selected.id).addr)
                index = reader.read_index(object_address, selected.shape, selected.chunks,
                                          selected.dtype.itemsize, maxshape=selected.maxshape)
                self.assertEqual(index.index_type, "fixed_array")
                start, end, _ = next(x for x in reader.metadata_ranges
                                     if x[2] == "fixed-array data block")
                metadata = list(reader.metadata_ranges)
            healthy = pristine.read_bytes()
            rng = random.Random(501)
            accepted = refused = 0
            for _ in range(96):
                raw = bytearray(healthy)
                position = rng.randrange(start + 6, end - 4)
                raw[position] ^= 1 << rng.randrange(8)
                raw[end - 4:end] = lookup3(raw[start:end - 4]).to_bytes(4, "little")
                mutant.write_bytes(raw)
                try:
                    with ModernH5File(mutant) as parser:
                        candidate = parser.read_index(
                            object_address, (8, 12), (2, 3), 4,
                            maxshape=(8, 12), max_chunks=4096,
                        )
                except (FormatError, UnsupportedFormat):
                    refused += 1
                    continue
                accepted += 1
                self.assertEqual(len(candidate.chunks), 16)
                self.assertEqual(len({x.coordinate for x in candidate.chunks}), 16)
                for chunk in candidate.chunks:
                    start_byte = chunk.address
                    end_byte = chunk.address + chunk.size
                    self.assertLessEqual(end_byte, len(raw))
                    self.assertTrue(all(end_byte <= low or high <= start_byte
                                        for low, high, _kind in metadata))
            self.assertGreater(refused, 0)
            self.assertEqual(accepted + refused, 96)

    def test_rechecks_valid_extensible_index_checksum_after_64_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            pristine, mutant = folder / "pristine.h5", folder / "mutant.h5"
            _generate(pristine, "extensible_array", np.random.default_rng(611))
            with h5py.File(pristine, "r") as native, ModernH5File(pristine) as reader:
                selected = native["/science"]
                object_address = int(h5py.h5o.get_info(selected.id).addr)
                index = reader.read_index(object_address, selected.shape, selected.chunks,
                                          selected.dtype.itemsize, maxshape=selected.maxshape)
                self.assertEqual(index.index_type, "extensible_array")
                start, end, _ = next(x for x in reader.metadata_ranges
                                     if x[2] == "extensible-array index block")
                metadata = list(reader.metadata_ranges)
            healthy = pristine.read_bytes()
            rng = random.Random(611)
            accepted = refused = 0
            for _ in range(64):
                raw = bytearray(healthy)
                position = rng.randrange(start + 6, end - 4)
                raw[position] ^= 1 << rng.randrange(8)
                raw[end - 4:end] = lookup3(raw[start:end - 4]).to_bytes(4, "little")
                mutant.write_bytes(raw)
                try:
                    with ModernH5File(mutant) as parser:
                        candidate = parser.read_index(object_address, (20, 5), (4, 5), 4,
                                                      maxshape=(None, 5), max_chunks=4096)
                except (FormatError, UnsupportedFormat):
                    refused += 1
                    continue
                accepted += 1
                self.assertEqual(len(candidate.chunks), 5)
                self.assertEqual(len({x.coordinate for x in candidate.chunks}), 5)
                for chunk in candidate.chunks:
                    start_byte = chunk.address
                    end_byte = chunk.address + chunk.size
                    self.assertLessEqual(end_byte, len(raw))
                    self.assertTrue(all(end_byte <= low or high <= start_byte
                                        for low, high, _kind in metadata))
            self.assertGreater(refused, 0)
            self.assertEqual(accepted + refused, 64)

    def test_rechecks_selected_object_header_checksum_after_64_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            pristine, mutant = folder / "pristine.h5", folder / "mutant.h5"
            _generate(pristine, "single", np.random.default_rng(913))
            with h5py.File(pristine, "r") as native, ModernH5File(pristine) as reader:
                selected = native["/science"]
                object_address = int(h5py.h5o.get_info(selected.id).addr)
                index = reader.read_index(object_address, selected.shape, selected.chunks,
                                          selected.dtype.itemsize, maxshape=selected.maxshape)
                self.assertEqual(index.index_type, "single_chunk")
                start, end, _ = next(x for x in reader.metadata_ranges
                                     if x[2] == "selected object header")
                metadata = list(reader.metadata_ranges)
            healthy = pristine.read_bytes()
            rng = random.Random(913)
            accepted = refused = 0
            for _ in range(64):
                raw = bytearray(healthy)
                position = rng.randrange(start + 6, end - 4)
                raw[position] ^= 1 << rng.randrange(8)
                raw[end - 4:end] = lookup3(raw[start:end - 4]).to_bytes(4, "little")
                mutant.write_bytes(raw)
                try:
                    with ModernH5File(mutant) as parser:
                        candidate = parser.read_index(object_address, (4, 4), (4, 4), 4,
                                                      maxshape=(4, 4), max_chunks=4096)
                except (FormatError, UnsupportedFormat):
                    refused += 1
                    continue
                accepted += 1
                self.assertEqual(len(candidate.chunks), 1)
                self.assertEqual(candidate.chunks[0].coordinate, (0, 0))
                self.assertLessEqual(candidate.chunks[0].address + candidate.chunks[0].size,
                                     len(raw))
                self.assertTrue(all(candidate.chunks[0].address + candidate.chunks[0].size <= low
                                    or high <= candidate.chunks[0].address
                                    for low, high, _kind in metadata))
            self.assertGreater(refused, 0)
            self.assertEqual(accepted + refused, 64)


if __name__ == "__main__":
    unittest.main()
