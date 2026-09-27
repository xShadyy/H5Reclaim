"""Large, physically sparse HDF5 with genuine damaged checked index metadata."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.format import FormatError, UnsupportedFormat
from h5reclaim.large_structural import recover_large_fixed_array
from h5reclaim.metadata import UnsupportedCase, read_dataset_spec
from h5reclaim.modern_indexes import ModernH5File
from h5reclaim.modern_link_repair import _scan_extents


class LargeStructuralTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / "damaged.h5"
        self.output = root / "recovered.h5"
        self.report = root / "evidence.json"

    def _source(self, *, count: int = 64, userblock: int = 0) -> tuple[dict[int, int], object]:
        truth = {0: 17, count // 2: 91, count - 1: 211}
        with h5py.File(self.source, "w", libver="latest", userblock_size=userblock) as opened:
            dataset = opened.create_dataset("science", shape=(count,), chunks=(1,), dtype="u1")
            for coordinate, value in truth.items():
                dataset[coordinate] = value
        spec = read_dataset_spec(self.source, "/science")
        with ModernH5File(self.source, large_sparse_scan=True, max_chunks=65536) as reader:
            index = reader.read_index(
                spec.object_address, spec.shape, spec.chunks, 1,
                maxshape=spec.maxshape, filters=spec.filters, max_chunks=65536,
            )
        self.assertEqual(index.index_type, "fixed_array")
        return truth, index

    def _damage(self, pointer: int) -> None:
        with self.source.open("r+b") as opened:
            opened.seek(pointer)
            original = opened.read(1)
            opened.seek(pointer)
            opened.write(bytes([original[0] ^ 0x67]))

    @unittest.skipUnless(hasattr(os, "SEEK_DATA") and hasattr(os, "SEEK_HOLE"),
                         "large sparse userblock fixture needs sparse extent APIs")
    def test_sparse_source_over_four_gib_and_grid_over_8192_reconstructs_checked_link(self) -> None:
        truth, index = self._source(count=8200, userblock=1 << 32)
        self.assertGreater(self.source.stat().st_size, 4 << 30)
        self.assertGreater(index.data_block_pointer_offset, 4 << 30)
        self._damage(index.data_block_pointer_offset)
        damaged_stat = self.source.stat()
        with self.assertRaises((OSError, RuntimeError, KeyError)):
            with h5py.File(self.source, "r") as opened:
                _ = opened["science"][:]

        report = recover_large_fixed_array(
            self.source, "/science", self.output, self.report,
        )
        self.assertEqual(report["operation"], "large_checked_fixed_array_pointer_recovery")
        self.assertEqual(report["allocated_chunks_checked"], len(truth))
        self.assertEqual(report["chunk_grid_count"], 8200)
        self.assertEqual(report["unknown_elements"], 8200 - len(truth))
        self.assertLess(report["source"]["snapshot_physical_data_bytes_copied"], 1 << 20)
        self.assertEqual(self.source.stat().st_size, damaged_stat.st_size)
        self.assertEqual(self.source.stat().st_mtime_ns, damaged_stat.st_mtime_ns)
        with h5py.File(self.output, "r") as result:
            evidence = result["/_h5reclaim/physical_evidence"][:]
            self.assertEqual(evidence["origin"].tolist(), sorted(truth))
            self.assertTrue(np.all(evidence["source_offset"] > (4 << 30)))
            validity = result["/_h5reclaim/validity"][:]
            self.assertEqual(int(np.sum(validity)), len(truth))
            for coordinate, value in truth.items():
                self.assertEqual(int(result["/science"][coordinate]), value)
                self.assertEqual(int(validity[coordinate]), 1)
            self.assertEqual(int(validity[1]), 0)

    def test_intact_index_is_not_reported_as_repair(self) -> None:
        self._source()
        with self.assertRaisesRegex(UnsupportedCase, "requires one damaged checked FAHD pointer"):
            recover_large_fixed_array(self.source, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_streams_more_than_8192_allocated_chunks_after_pointer_damage(self) -> None:
        values = np.arange(8200, dtype="u1")
        with h5py.File(self.source, "w", libver="latest") as opened:
            opened.create_dataset("science", data=values, chunks=(1,))
        spec = read_dataset_spec(self.source, "/science")
        with ModernH5File(self.source, large_sparse_scan=True, max_chunks=65536) as reader:
            index = reader.read_index(spec.object_address, spec.shape, spec.chunks, 1,
                                      maxshape=spec.maxshape, max_chunks=65536)
        self._damage(index.data_block_pointer_offset)
        report = recover_large_fixed_array(self.source, "/science", self.output, self.report)
        self.assertEqual(report["allocated_chunks_checked"], 8200)
        self.assertEqual(report["unknown_elements"], 0)
        with h5py.File(self.output, "r") as result:
            np.testing.assert_array_equal(result["science"][:], values)
            self.assertTrue(np.all(result["/_h5reclaim/validity"][:] == 1))

    def test_damaged_child_checksum_refuses_even_if_original_parent_restores(self) -> None:
        _, index = self._source()
        self._damage(index.data_block_pointer_offset)
        with self.source.open("r+b") as opened:
            opened.seek(index.data_block_address + 6)
            original = opened.read(1)
            opened.seek(index.data_block_address + 6)
            opened.write(bytes([original[0] ^ 0x40]))
        with self.assertRaises(FormatError):
            recover_large_fixed_array(self.source, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    @unittest.skipUnless(hasattr(os, "SEEK_DATA") and hasattr(os, "SEEK_HOLE"),
                         "mocked sparse extent budget needs extent APIs")
    def test_sparse_scan_refuses_excess_physical_data(self) -> None:
        self._source(count=64, userblock=1 << 32)
        with ModernH5File(self.source, large_sparse_scan=True) as reader:
            base = reader.superblock.base_address
            # Simulate an extent whose declared physical allocation exceeds
            # the scan quota without writing hundreds of MiB of test data.
            reader.size = base + (512 << 20) + 2
            reader.superblock = replace(reader.superblock, eof_address=reader.size)
            def allocation(_fd, _offset, whence):
                if whence == os.SEEK_DATA:
                    return base
                return base + (512 << 20) + 1
            with patch("h5reclaim.modern_link_repair.os.lseek", side_effect=allocation):
                with self.assertRaisesRegex(UnsupportedFormat, "512 MiB allocated data budget"):
                    list(_scan_extents(reader))


if __name__ == "__main__":
    unittest.main()
