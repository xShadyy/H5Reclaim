"""Large readable route: sparse capture, scale, validity and failure bounds."""

from __future__ import annotations

import json
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.large_streaming import (
    GIB, LargeBudget, UnsupportedCase, _copy_dense, export_large_readable, sparse_snapshot,
)
from h5reclaim.recovery import RecoveryError


class LargeStreamingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.h5"
        self.output = self.root / "recovered.h5"
        self.report = self.root / "report.json"

    def _small_source(self, *, sparse: bool = False) -> None:
        with h5py.File(self.source, "w") as handle:
            dataset = handle.create_dataset("science", shape=(70,), dtype="<f8",
                                            chunks=(10,), compression="gzip")
            dataset[0:10] = np.arange(10, dtype="f8")
            dataset[40:50] = np.arange(40, 50, dtype="f8")
        if sparse:
            with self.source.open("r+b") as opened:
                opened.truncate(4 * GIB + 16384)

    def test_more_than_four_gib_sparse_file_and_partial_chunked_values(self) -> None:
        self._small_source(sparse=True)
        before = self.source.stat()
        with self.source.open("rb") as opened:
            prefix = opened.read(4096)
        report = export_large_readable(self.source, "/science", self.output, self.report)
        self.assertEqual(report["source"]["size_bytes"], 4 * GIB + 16384)
        self.assertLess(report["source"]["snapshot_physical_data_bytes_copied"], 8 * 1024 * 1024)
        self.assertEqual(report["accepted_elements"], 20)
        self.assertEqual(report["unknown_elements"], 50)
        self.assertEqual(report["outcome"], "partial")
        self.assertEqual(self.source.stat().st_size, before.st_size)
        self.assertEqual(self.source.stat().st_mtime_ns, before.st_mtime_ns)
        with self.source.open("rb") as opened:
            self.assertEqual(opened.read(4096), prefix)
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["/_h5reclaim/validity"][:], [1, 0, 0, 0, 1, 0, 0])
            np.testing.assert_array_equal(handle["/science"][0:10], np.arange(10, dtype="f8"))
            evidence = handle["/_h5reclaim/physical_evidence"][:]
            self.assertEqual(evidence["origin"].tolist(), [0, 40])
            self.assertEqual(len(evidence["raw_sha256"][0]), 64)
            self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()])["source"]["sha256_before"],
                             report["source"]["sha256_before"])

    def test_more_than_eight_thousand_chunks(self) -> None:
        n = 8200
        with h5py.File(self.source, "w") as handle:
            dataset = handle.create_dataset("science", shape=(n,), dtype="u1", chunks=(1,))
            dataset[:] = np.arange(n, dtype="u1")
        report = export_large_readable(self.source, "/science", self.output, self.report)
        self.assertEqual(report["allocated_chunks_checked"], n)
        self.assertEqual(report["accepted_elements"], n)
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["/science"][:], np.arange(n, dtype="u1"))
            self.assertTrue(np.all(handle["/_h5reclaim/validity"][:] == 1))
            self.assertEqual(handle["/_h5reclaim/physical_evidence"].shape, (n,))

    def test_contiguous_current_values_preserve_schema_and_range(self) -> None:
        values = np.arange(513, dtype=">i2")
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("science", data=values)
        report = export_large_readable(self.source, "/science", self.output, self.report)
        self.assertEqual(report["outcome"], "complete")
        self.assertEqual(report["dataset"]["dtype"], ">i2")
        self.assertIsNone(report["validity_map"])
        self.assertEqual(report["accepted_elements"], len(values))
        self.assertIsNotNone(report["dataset"]["source_contiguous_byte_range"])
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["science"][:], values)
            self.assertEqual(handle["science"].dtype.str, ">i2")

    def test_quota_refuses_before_publication(self) -> None:
        self._small_source()
        with self.assertRaisesRegex(UnsupportedCase, "copy or free-disk quota"):
            export_large_readable(self.source, "/science", self.output, self.report,
                                  budget=LargeBudget(max_copied_bytes=1))
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())
        with self.assertRaisesRegex(UnsupportedCase, "output exceeded"):
            export_large_readable(self.source, "/science", self.output, self.report,
                                  budget=LargeBudget(max_output_bytes=100))
        self.assertFalse(self.output.exists())

    def test_free_space_refusal_and_source_mutation(self) -> None:
        self._small_source()
        class TinyDisk:
            free = 1

        with patch("h5reclaim.large_streaming.shutil.disk_usage", return_value=TinyDisk()):
            with self.assertRaisesRegex(UnsupportedCase, "copy or free-disk quota"):
                export_large_readable(self.source, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())

        from h5reclaim import large_streaming
        original_copy = large_streaming._copy_sparse

        def mutate(*args, **kwargs):
            digest, copied = original_copy(*args, **kwargs)
            with self.source.open("r+b") as opened:
                opened.seek(0, 2)
                opened.write(b"mutation")
            return digest, copied

        with patch("h5reclaim.large_streaming._copy_sparse", side_effect=mutate):
            with self.assertRaisesRegex(RecoveryError, "input changed"):
                with sparse_snapshot(self.source):
                    pass
        self.assertFalse(self.output.exists())

    def test_bounded_full_copy_fallback_for_systems_without_sparse_extents(self) -> None:
        self._small_source()
        copied = self.root / "copy.h5"
        size = self.source.stat().st_size
        with self.source.open("rb") as source, copied.open("xb") as target:
            digest, written = _copy_dense(
                source, target, size=size, parent=self.root,
                budget=LargeBudget(max_copied_bytes=size), deadline=time.monotonic() + 30,
            )
        self.assertEqual(written, size)
        self.assertEqual(digest, hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(copied.read_bytes(), self.source.read_bytes())
        with patch("h5reclaim.large_streaming._has_sparse_extents", return_value=False):
            report = export_large_readable(self.source, "/science", self.output, self.report)
        self.assertEqual(report["source"]["snapshot_physical_data_bytes_copied"], size)
        with h5py.File(self.output, "r") as handle:
            self.assertEqual(handle["/_h5reclaim/validity"][:].tolist(), [1, 0, 0, 0, 1, 0, 0])


if __name__ == "__main__":
    unittest.main()
