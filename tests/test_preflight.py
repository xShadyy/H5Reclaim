"""Independent metadata inspection and bounded signature discovery."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.large_streaming import LargeBudget
from h5reclaim.metadata import read_dataset_spec
from h5reclaim.modern_indexes import ModernH5File
from h5reclaim.preflight import preflight, scan_markers


class PreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.h5"

    def fixture(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("data", data=np.arange(120, dtype="i4").reshape(12, 10),
                chunks=(4, 5), compression="gzip", fletcher32=True)

    def test_raw_preflight_does_not_open_a_file_through_h5py(self):
        self.fixture()
        before = self.source.read_bytes()
        with patch("h5py.File", side_effect=AssertionError("native open forbidden")):
            result = preflight(self.source)
        self.assertEqual(result["decision"], "accept", result)
        self.assertEqual(result["objects"][1]["index_type"], "fixed_array")
        self.assertFalse(result["boundary"]["payload_reads"])
        self.assertEqual(before, self.source.read_bytes())

    def test_checked_index_damage_becomes_a_repairable_finding(self):
        self.fixture()
        spec = read_dataset_spec(self.source, "/data")
        with ModernH5File(self.source) as reader:
            index = reader.read_index(spec.object_address, spec.shape, spec.chunks, 4,
                maxshape=spec.maxshape, filters=spec.filters)
        with self.source.open("r+b") as stream:
            stream.seek(index.data_block_pointer_offset)
            old = stream.read(1)
            stream.seek(index.data_block_pointer_offset)
            stream.write(bytes([old[0] ^ 0x40]))
        result = preflight(self.source)
        self.assertEqual(result["decision"], "reject_corrupt")
        self.assertEqual(result["index_repairs"][0]["kind"], "fahd_to_fadb")

    def test_strict_profile_reports_external_links_without_following_them(self):
        self.fixture()
        with h5py.File(self.source, "r+") as handle:
            handle["outside"] = h5py.ExternalLink("not-present.h5", "/data")
        result = preflight(self.source, profile="strict")
        self.assertEqual(result["decision"], "reject_dependencies")
        self.assertFalse(result["boundary"]["external_file_opens"])

    def test_marker_scan_finds_cross_block_candidates_without_ownership(self):
        self.source.write_bytes(b"abcFAHD123OHDR456FADB")
        result = scan_markers(self.source, budget=LargeBudget(block_bytes=5))
        self.assertEqual([(r["signature"], r["offset"]) for r in result["candidates"]],
                         [("FAHD", 3), ("OHDR", 10), ("FADB", 17)])
        self.assertTrue(all(r["ownership"] == "unassigned" for r in result["candidates"]))


if __name__ == "__main__":
    unittest.main()
