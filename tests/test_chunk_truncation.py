"""Tail truncation uses only originally present rooted metadata and payload."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.chunk_truncation import analyze_truncated, recover_truncated
from h5reclaim.metadata_fallback import read_dataset_spec_fallback
from h5reclaim.modern_indexes import ModernH5File, lookup3


class ChunkTruncationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "damaged.h5"
        self.output = self.base / "result.h5"
        self.report = self.base / "report.json"

    def make_fixture(self, libver="latest", *, compression=None):
        values = np.arange(100, dtype="<u4") * 7 + 3
        with h5py.File(self.source, "w", libver=libver) as handle:
            handle.create_dataset("science", data=values, chunks=(10,),
                                  compression=compression)
        return values

    def assert_partial(self, values, expected_count, available):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = recover_truncated(self.source, "/science", self.output, self.report)
        self.assertEqual(result["counts"]["recovered"], expected_count)
        self.assertEqual(result["counts"]["unavailable"], 10 - expected_count)
        self.assertFalse(result["complete"])
        self.assertEqual(result["source"]["sha256_before"], before)
        self.assertEqual(result["source"]["sha256_after"], before)
        self.assertEqual(result["source"]["size_bytes"], self.source.stat().st_size)
        self.assertEqual(len(result["evidence_ledger"]["proposals"]), expected_count)
        self.assertTrue(all(item["status"] == "accepted"
                            for item in result["evidence_ledger"]["decisions"]))
        self.assertEqual(json.loads(self.report.read_text()), result)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        with h5py.File(self.output, "r") as output:
            np.testing.assert_array_equal(output["science"][:expected_count * 10],
                                          values[:expected_count * 10])
            np.testing.assert_array_equal(output["/_h5reclaim/chunk_status"][:],
                                          np.array([1] * expected_count +
                                                   [4] * (10 - expected_count), dtype="u1"))
            self.assertEqual(output["science"].attrs["h5reclaim_complete"], False)
        self.assertEqual(result["truncated_payloads"][-1]["physical_bytes_available"], available)

    def test_v1_partially_present_last_chunk(self):
        values = self.make_fixture(libver="earliest")
        self.source.write_bytes(self.source.read_bytes()[:-20])
        self.assert_partial(values, 9, 20)
        self.assertEqual(json.loads(self.report.read_text())["index"]["type"],
                         "v1_raw_data_btree")

    def test_raw_fallback_explicitly_omits_scientific_attributes(self):
        self.make_fixture()
        with h5py.File(self.source, "r+") as file:
            file["science"].attrs["h5reclaim_complete"] = np.uint32(17)
        self.source.write_bytes(self.source.read_bytes()[:-20])
        result = recover_truncated(self.source, "/science", self.output, self.report)
        self.assertEqual(result["selected_annotation_collisions"], [])
        self.assertEqual(result["dataset"]["attributes_copied"], [])
        self.assertTrue(any("all attribute metadata" in name
                            for name in result["dataset"]["attributes_omitted"]))
        with h5py.File(self.output, "r") as output:
            self.assertEqual(output["science"].attrs["h5reclaim_complete"], False)
            self.assertEqual(json.loads(output["/_h5reclaim/report_json"][()]), result)

    def test_modern_fixed_array_partially_present_last_chunk(self):
        values = self.make_fixture()
        self.source.write_bytes(self.source.read_bytes()[:-20])
        self.assert_partial(values, 9, 20)
        self.assertEqual(json.loads(self.report.read_text())["index"]["type"], "fixed_array")

    def test_filtered_chunk_is_not_decoded_from_zero_extension(self):
        values = self.make_fixture(compression="gzip")
        with h5py.File(self.source) as handle, ModernH5File(self.source) as reader:
            data = handle["science"]
            index = reader.read_index(int(h5py.h5o.get_info(data.id).addr),
                                      data.shape, data.chunks, data.dtype.itemsize,
                                      maxshape=data.maxshape, filters=(1,))
            last = index.chunks[-1]
            offset = reader.absolute(last.address)
        self.source.write_bytes(self.source.read_bytes()[:offset])
        self.assert_partial(values, 9, 0)

    def test_extensible_array_retains_earlier_present_chunks(self):
        values = np.arange(100, dtype="<u4")
        with h5py.File(self.source, "w", libver="latest") as handle:
            data = handle.create_dataset("science", shape=(100,), maxshape=(None,),
                                         chunks=(10,), dtype="<u4")
            data[:] = values
        with h5py.File(self.source) as handle, ModernH5File(self.source) as reader:
            data = handle["science"]
            index = reader.read_index(int(h5py.h5o.get_info(data.id).addr),
                                      data.shape, data.chunks, data.dtype.itemsize,
                                      maxshape=data.maxshape)
            self.assertEqual(index.index_type, "extensible_array")
            last = index.chunks[-1]
            offset = reader.absolute(last.address)
        self.source.write_bytes(self.source.read_bytes()[:offset + 8])
        self.assert_partial(values, 9, 8)

    def test_all_indexed_payload_gone_cannot_become_zero_measurements(self):
        self.make_fixture()
        with h5py.File(self.source) as handle, ModernH5File(self.source) as reader:
            data = handle["science"]
            index = reader.read_index(int(h5py.h5o.get_info(data.id).addr),
                                      data.shape, data.chunks, data.dtype.itemsize,
                                      maxshape=data.maxshape)
            start = min(reader.absolute(chunk.address) for chunk in index.chunks)
        self.source.write_bytes(self.source.read_bytes()[:start])
        result = recover_truncated(self.source, "/science", self.output, self.report)
        self.assertEqual(result["counts"]["recovered"], 0)
        self.assertEqual(result["counts"]["unavailable"], 10)
        self.assertFalse(result["evidence_ledger"]["proposals"])
        with h5py.File(self.output) as output:
            self.assertTrue(np.all(output["/_h5reclaim/chunk_status"][:] == 4))

    def test_missing_rooted_index_metadata_refuses_without_publication(self):
        self.make_fixture(libver="earliest")
        with h5py.File(self.source, "r") as handle:
            address = int(h5py.h5o.get_info(handle["science"].id).addr)
        from h5reclaim.format import H5File
        with H5File(self.source) as reader:
            layout = reader.read_dataset_layout(address, rank=1)
            reader.walk_tree(layout.root_address, rank=1, element_size=4)
            index_start, index_end, _ = next(
                item for item in reader.metadata_ranges if item[2] == "B-tree node")
        self.assertGreater(index_end, index_start)
        self.source.write_bytes(self.source.read_bytes()[:index_end - 1])
        with self.assertRaisesRegex(ValueError, "metadata|index|EOF|source|read"):
            recover_truncated(self.source, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_modern_header_cut_refuses_without_publication(self):
        self.make_fixture()
        rooted = read_dataset_spec_fallback(self.source, "/science")
        headers = [item for item in rooted.metadata_ranges
                   if item[2] == "selected object header"]
        self.assertEqual(len(headers), 1)
        self.source.write_bytes(self.source.read_bytes()[:headers[0][1] - 1])
        with self.assertRaises(ValueError):
            recover_truncated(self.source, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_modern_index_cut_refuses_without_publication(self):
        self.make_fixture()
        with h5py.File(self.source) as handle, ModernH5File(self.source) as reader:
            data = handle["science"]
            reader.read_index(int(h5py.h5o.get_info(data.id).addr),
                              data.shape, data.chunks, data.dtype.itemsize,
                              maxshape=data.maxshape)
            blocks = [item for item in reader.metadata_ranges
                      if item[2] == "fixed-array data block"]
            self.assertEqual(len(blocks), 1)
            index_end = blocks[0][1]
        self.source.write_bytes(self.source.read_bytes()[:index_end - 1])
        with self.assertRaises(ValueError):
            recover_truncated(self.source, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_declared_eof_gap_is_capped_and_modern_checksum_must_match(self):
        self.make_fixture()
        raw = bytearray(self.source.read_bytes())
        offsize = raw[9]
        position = 12 + 2 * offsize
        huge = len(raw) + 256 * 1024 * 1024 + 1
        raw[position:position + offsize] = huge.to_bytes(offsize, "little")
        checksum_offset = 12 + 4 * offsize
        raw[checksum_offset:checksum_offset + 4] = lookup3(raw[:checksum_offset]).to_bytes(4, "little")
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "tail limit"):
            analyze_truncated(self.source, "/science")
        raw[position:position + offsize] = (len(raw) + 20).to_bytes(offsize, "little")
        # The deliberately stale checksum must not be bypassed by padding.
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "checksum"):
            analyze_truncated(self.source, "/science")

    def test_no_declared_gap_uses_regular_route(self):
        self.make_fixture()
        with self.assertRaisesRegex(ValueError, "ordinary rescue"):
            analyze_truncated(self.source, "/science")

    def _cli(self):
        return subprocess.run([
            sys.executable, "-m", "h5reclaim", "rescue", str(self.source),
            "--dataset", "/science", "--truncated-chunks",
            "--output", str(self.output), "--report", str(self.report),
        ], capture_output=True, text=True, timeout=30)

    def test_public_cli_publishes_original_hash_and_unknown_cut_chunk(self):
        values = self.make_fixture()
        self.source.write_bytes(self.source.read_bytes()[:-20])
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self._cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("partial | 9 accepted chunks", result.stdout)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["source"]["sha256_before"], source_hash)
        self.assertEqual(report["source"]["sha256_after"], source_hash)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), source_hash)
        with h5py.File(self.output) as output:
            np.testing.assert_array_equal(output["science"][:90], values[:90])
            self.assertEqual(output["/_h5reclaim/chunk_status"][-1], 4)

    def test_public_cli_intact_file_refuses_without_outputs(self):
        self.make_fixture()
        result = self._cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("declared HDF5 EOF does not exceed physical EOF", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())


if __name__ == "__main__":
    unittest.main()
