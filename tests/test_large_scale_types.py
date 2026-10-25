"""Real HDF5 layouts beyond the former rank and chunk-grid ceilings."""

from __future__ import annotations

import tempfile
import tracemalloc
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.large_structural import recover_large_structural
from h5reclaim.large_streaming import LargeBudget
from h5reclaim.metadata import UnsupportedCase, read_dataset_spec
from h5reclaim.modern_indexes import ModernH5File


class LargeScaleTypesTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_too_small_metadata_budget_refuses_cleanly(self) -> None:
        source = self.root / "tiny.h5"
        output = self.root / "tiny-recovered.h5"
        report_path = self.root / "tiny-report.json"
        with h5py.File(source, "w", libver="latest") as opened:
            opened.create_dataset("science", data=np.array([1, 2], dtype="u1"),
                                  chunks=(1,))
        with self.assertRaisesRegex(UnsupportedCase, "metadata budget"):
            recover_large_structural(source, "/science", output, report_path,
                                     budget=LargeBudget(max_metadata_bytes=7))
        self.assertFalse(output.exists())
        self.assertFalse(report_path.exists())

    def test_million_slot_sparse_fixed_array_is_checked_without_materializing_grid(self) -> None:
        source = self.root / "million.h5"
        truth = {0: 7, 500_000: 9, 1_000_000: 11}
        with h5py.File(source, "w", libver="latest") as opened:
            selected = opened.create_dataset("science", (1_000_001,), chunks=(1,),
                                             dtype="u1", compression="gzip")
            for offset, value in truth.items():
                selected[offset] = value
        spec = read_dataset_spec(source, "/science", max_elements=1_100_000)
        tracemalloc.start()
        try:
            with ModernH5File(source, large_sparse_scan=True, max_chunks=1_100_000,
                              max_chunk_bytes=64 << 20) as reader:
                index = reader.read_index(spec.object_address, spec.shape, spec.chunks, 1,
                                          maxshape=spec.maxshape, filters=spec.filters,
                                          max_chunks=1_100_000)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(index.index_type, "fixed_array")
        self.assertEqual({chunk.coordinate[0] for chunk in index.chunks}, set(truth))
        self.assertLess(peak, 24 << 20)
        assert index.data_block_pointer_offset is not None
        with source.open("r+b") as opened:
            opened.seek(index.data_block_pointer_offset)
            before = opened.read(1)
            opened.seek(index.data_block_pointer_offset)
            opened.write(bytes([before[0] ^ 0x67]))
        output = self.root / "million-recovered.h5"
        report_path = self.root / "million-report.json"
        result = recover_large_structural(source, "/science", output, report_path,
            budget=LargeBudget(max_chunks=1_100_000, max_grid=1_100_000))
        self.assertEqual(result["allocated_chunks_checked"], len(truth))
        self.assertEqual(result["unknown_elements"], 1_000_001 - len(truth))
        with h5py.File(output) as restored:
            for offset, value in truth.items():
                self.assertEqual(int(restored["/science"][offset]), value)
                self.assertEqual(int(restored["/_h5reclaim/validity"][offset]), 1)
            self.assertEqual(int(restored["/_h5reclaim/validity"][1234]), 0)

    def test_rank_32_compound_string_chunks_survive_checked_link_recovery(self) -> None:
        source = self.root / "rank32.h5"
        output = self.root / "output.h5"
        report_path = self.root / "report.json"
        shape = (2,) + (1,) * 31
        chunks = (1,) * 32
        dtype = np.dtype([("sample", "<i4"), ("label", "S9")])
        with h5py.File(source, "w", libver="latest") as opened:
            selected = opened.create_dataset("science", shape, chunks=chunks,
                                             dtype=dtype, compression="gzip")
            selected[(0,) * 32] = (17, b"alpha")
            selected[(1,) + (0,) * 31] = (29, b"beta")
            file_type = selected.id.get_type().copy()
        spec = read_dataset_spec(source, "/science", max_rank=32)
        with ModernH5File(source, large_sparse_scan=True, max_chunks=65_536) as reader:
            index = reader.read_index(spec.object_address, spec.shape, spec.chunks,
                                      dtype.itemsize, maxshape=spec.maxshape,
                                      filters=spec.filters, max_chunks=65_536)
        self.assertEqual(index.index_type, "fixed_array")
        self.assertEqual(len(index.chunks), 2)
        assert index.data_block_pointer_offset is not None
        with source.open("r+b") as opened:
            opened.seek(index.data_block_pointer_offset)
            first = opened.read(1)
            opened.seek(index.data_block_pointer_offset)
            opened.write(bytes([first[0] ^ 0x67]))
        result = recover_large_structural(source, "/science", output, report_path)
        self.assertEqual(result["accepted_elements"], 2)
        with h5py.File(output) as restored:
            selected = restored["/science"]
            self.assertTrue(selected.id.get_type().equal(file_type))
            self.assertEqual(int(selected[(0,) * 32]["sample"]), 17)
            self.assertEqual(selected[(1,) + (0,) * 31]["label"], b"beta")
            self.assertTrue(np.all(restored["/_h5reclaim/validity"][:] == 1))

    def test_wide_compound_and_six_dimensional_array_keep_exact_file_types(self) -> None:
        for kind, dtype, limit in (
            ("compound", np.dtype([(f"field_{n}", "u1") for n in range(70)]),
             {"max_type_members": 70}),
            ("array", np.dtype((np.dtype(">u2"), (1, 1, 1, 1, 1, 2))),
             {"max_array_rank": 6}),
        ):
            with self.subTest(kind=kind):
                source = self.root / f"{kind}.h5"
                with h5py.File(source, "w", libver="latest") as opened:
                    selected = opened.create_dataset("records", (2,), chunks=(1,), dtype=dtype)
                    if kind == "compound":
                        selected[0] = tuple(range(70))
                        selected[1] = tuple(range(70, 140))
                    else:
                        selected[0] = np.array([3, 5], dtype=">u2").reshape((1,) * 5 + (2,))
                        selected[1] = np.array([7, 11], dtype=">u2").reshape((1,) * 5 + (2,))
                    file_type = selected.id.get_type().copy()
                    expected = selected[:]
                with self.assertRaisesRegex(UnsupportedCase, "limit"):
                    read_dataset_spec(source, "/records")
                spec = read_dataset_spec(source, "/records", **limit)
                self.assertEqual(np.dtype(spec.dtype).itemsize, file_type.get_size())
                self.assertTrue(h5py.h5t.decode(spec.file_type_encoding).equal(file_type))
                with ModernH5File(source, large_sparse_scan=True, max_chunks=65_536) as reader:
                    index = reader.read_index(spec.object_address, spec.shape, spec.chunks,
                                              file_type.get_size(), maxshape=spec.maxshape,
                                              filters=spec.filters, max_chunks=65_536)
                self.assertEqual(index.index_type, "fixed_array")
                assert index.data_block_pointer_offset is not None
                with source.open("r+b") as opened:
                    opened.seek(index.data_block_pointer_offset)
                    first = opened.read(1)
                    opened.seek(index.data_block_pointer_offset)
                    opened.write(bytes([first[0] ^ 0x67]))
                output = self.root / f"{kind}-recovered.h5"
                report_path = self.root / f"{kind}-report.json"
                result = recover_large_structural(source, "/records", output, report_path)
                self.assertEqual(result["accepted_elements"], len(expected))
                with h5py.File(output) as recovered:
                    actual = recovered["/records"]
                    self.assertTrue(actual.id.get_type().equal(file_type))
                    np.testing.assert_array_equal(actual[:], expected)


if __name__ == "__main__":
    unittest.main()
