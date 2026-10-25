"""Recover real HDF5-written compressed arrays through damaged index links."""

import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.large_streaming import LargeBudget
from h5reclaim.large_structural import recover_large_structural
from h5reclaim.metadata import read_dataset_spec
from h5reclaim.modern_indexes import ModernH5File
from h5reclaim.masked_reader import read_masked
from h5reclaim.recovery import sha256_file
from h5reclaim.rescue import auto_rescue


class LargeCompressedRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source, self.output, self.report = (self.root / n for n in ("source.h5", "output.h5", "report.json"))

    def fixture(self, kind, shape, chunks, *, codec="gzip", dtype="<i4"):
        values = np.arange(np.prod(shape), dtype=dtype).reshape(shape)
        maximum = shape if kind == "fixed_array" else (None, *shape[1:]) if kind == "extensible_array" else (None,) * len(shape)
        with h5py.File(self.source, "w", libver="latest") as handle:
            data = handle.create_dataset("science", data=values, maxshape=maximum, chunks=chunks,
                compression=codec, shuffle=True, fletcher32=True)
            data.attrs["units"] = np.bytes_("volts")
        spec = read_dataset_spec(self.source, "/science", max_elements=1 << 30, max_chunk_bytes=16 << 20)
        with ModernH5File(self.source, large_sparse_scan=True, max_chunks=65536) as reader:
            index = reader.read_index(spec.object_address, shape, chunks, np.dtype(dtype).itemsize,
                maxshape=maximum, filters=spec.filters, max_chunks=65536)
            self.assertEqual(index.index_type, kind)
            if kind == "fixed_array": pointer = index.data_block_pointer_offset
            elif kind == "extensible_array": pointer = index.data_block_pointer_offset
            else: pointer = reader.absolute(index.base_address) + 16
            payload = reader.absolute(index.chunks[0].address)
        with self.source.open("r+b") as stream:
            stream.seek(pointer)
            old = stream.read(1)
            stream.seek(pointer)
            stream.write(bytes([old[0] ^ 0x40]))
        return values, payload

    def test_large_2d_fixed_array_and_3d_extensible_array_and_btree(self):
        for kind, shape, chunks in (("fixed_array", (1100, 1000), (256, 250)),
                                    ("extensible_array", (72, 128, 128), (8, 32, 32)),
                                    ("v2_btree", (1100, 1000), (100, 100))):
            with self.subTest(kind=kind):
                values, _ = self.fixture(kind, shape, chunks)
                before = sha256_file(self.source)
                report = recover_large_structural(self.source, "/science", self.output, self.report)
                self.assertEqual(report["accepted_elements"], values.size)
                self.assertEqual(report["index"]["type"], kind)
                self.assertEqual(before, sha256_file(self.source))
                with h5py.File(self.output, "r") as handle:
                    np.testing.assert_array_equal(handle["science"][:], values)
                    self.assertTrue(np.all(handle[report["validity_map"]][:] == 1))
                    self.assertEqual(handle["science"].attrs["units"], b"volts")
                self.output.unlink()
                self.report.unlink()

    def test_one_corrupt_payload_remains_masked_after_index_repair(self):
        values, payload = self.fixture("fixed_array", (45, 33), (8, 8), codec="lzf")
        with self.source.open("r+b") as stream:
            stream.seek(payload + 2)
            old = stream.read(1)
            stream.seek(payload + 2)
            stream.write(bytes([old[0] ^ 1]))
        report = recover_large_structural(self.source, "/science", self.output, self.report)
        self.assertEqual(report["unknown_elements"], 64)
        self.assertEqual(report["counts"]["decode_failed"], 1)
        masked = read_masked(self.output, self.report, "/science")
        self.assertTrue(masked.mask[:8, :8].all())
        np.testing.assert_array_equal(masked.compressed(), np.ma.array(values, mask=masked.mask).compressed())

    def test_large_decoded_chunk_and_automatic_routing(self):
        values, _ = self.fixture("fixed_array", (2048, 1024), (1024, 1024))
        report = auto_rescue(self.source, "/science", self.output, self.report)
        self.assertEqual(report["automatic_routing"]["selected"], "large_structural")
        self.assertEqual(report["accepted_elements"], values.size)
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["science"][:], values)

    def test_logical_budget_refuses_without_publishing(self):
        self.fixture("fixed_array", (1100, 1000), (256, 250))
        with self.assertRaises(ValueError):
            recover_large_structural(self.source, "/science", self.output, self.report,
                budget=LargeBudget(max_logical_bytes=1 << 20))
        self.assertFalse(self.output.exists())

    def test_extensible_and_btree_recovery_above_the_old_8192_chunk_limit(self):
        for kind in ("extensible_array", "v2_btree"):
            with self.subTest(kind=kind):
                values, _ = self.fixture(kind, (100, 100), (1, 1), dtype="u1")
                result = recover_large_structural(self.source, "/science", self.output, self.report)
                self.assertEqual(result["allocated_chunks_checked"], 10000)
                self.assertEqual(result["accepted_elements"], 10000)
                with h5py.File(self.output, "r") as handle:
                    np.testing.assert_array_equal(handle["science"][:], values)
                self.output.unlink()
                self.report.unlink()

    def test_saved_large_payload_digests_are_verified_after_publication(self):
        from h5reclaim.result_verification import verify_result, ResultMismatch
        self.fixture("fixed_array", (45, 33), (8, 8))
        recover_large_structural(self.source, "/science", self.output, self.report)
        self.assertTrue(verify_result(self.output, self.report, check_values=True)["values_checked"])
        with h5py.File(self.output, "r+") as handle:
            handle["science"][2, 3] = -99
        with self.assertRaises(ResultMismatch):
            verify_result(self.output, self.report, check_values=True)


if __name__ == "__main__":
    unittest.main()
