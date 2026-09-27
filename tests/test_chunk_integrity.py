"""A prior capture detects silent payload changes in rooted current chunks."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.baseline import capture_baseline
from h5reclaim.chunk_integrity import export_verified_chunks
from h5reclaim.recovery import RecoveryError, analyze


class ProspectiveChunkIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "experiment.h5"
        self.baseline = self.directory / "prior-chunks.json"
        self.output = self.directory / "derived.h5"
        self.report = self.directory / "evidence.json"

    def _source(self, *, latest: bool, checksum: bool = False) -> np.ndarray:
        values = np.arange(64, dtype="<u4").reshape(8, 8) * 17
        with h5py.File(self.source, "w", libver="latest" if latest else "earliest") as file:
            file.create_dataset("x", data=values, chunks=(2, 2), fletcher32=checksum)
        capture_baseline(self.source, "/x", self.baseline)
        return values

    def _export(self) -> dict:
        return export_verified_chunks(
            self.source, "/x", self.baseline,
            hashlib.sha256(self.baseline.read_bytes()).hexdigest(),
            self.output, self.report,
        )

    def test_unfiltered_native_read_can_be_silent_but_prior_capture_withholds_chunk(self) -> None:
        for latest in (False, True):
            with self.subTest(latest=latest):
                for path in (self.source, self.baseline, self.output, self.report):
                    path.unlink(missing_ok=True)
                expected = self._source(latest=latest)
                record = next(r for r in analyze(self.source, "/x").records
                              if r.coordinate == (2, 4))
                raw = bytearray(self.source.read_bytes())
                raw[record.absolute_offset + 3] ^= 0x01
                self.source.write_bytes(raw)
                damaged_hash = hashlib.sha256(raw).hexdigest()
                with h5py.File(self.source) as file:
                    self.assertFalse(np.array_equal(file["x"][...], expected))
                self.assertTrue(analyze(self.source, "/x").report["complete"])
                result = self._export()
                self.assertEqual(result["counts"]["recovered"], 15)
                self.assertEqual(result["counts"]["unavailable"], 1)
                self.assertEqual(result["unresolved_chunks"][0]["coordinate"], [2, 4])
                self.assertEqual(result, json.loads(self.report.read_text()))
                self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), damaged_hash)
                with h5py.File(self.output) as file:
                    data = file["x"][...]
                    status = file["/_h5reclaim/chunk_status"][...]
                    self.assertEqual(np.argwhere(status == 4).tolist(), [[1, 2]])
                    self.assertTrue(np.all(data[2:4, 4:6] == 0))
                    expected = expected.copy()
                    expected[2:4, 4:6] = 0
                    np.testing.assert_array_equal(data, expected)

    def test_fletcher_failure_retains_decode_failed_and_never_promotes_checksum_fault(self) -> None:
        self._source(latest=True, checksum=True)
        record = next(r for r in analyze(self.source, "/x").records
                      if r.coordinate == (0, 0))
        raw = bytearray(self.source.read_bytes())
        raw[record.absolute_offset] ^= 1
        self.source.write_bytes(raw)
        result = self._export()
        self.assertEqual(result["counts"]["recovered"], 15)
        self.assertEqual(result["counts"]["decode_failed"], 1)
        self.assertEqual(result["unresolved_chunks"][0]["existing_status"], "decode_failed")
        with h5py.File(self.output) as file:
            self.assertTrue(np.all(file["x"][0:2, 0:2] == 0))

    def test_untouched_complete_source_and_wrong_prior_digest(self) -> None:
        expected = self._source(latest=True)
        with self.assertRaisesRegex(RecoveryError, "retained SHA-256"):
            export_verified_chunks(self.source, "/x", self.baseline, "0" * 64,
                                   self.output, self.report)
        self.assertFalse(self.output.exists())
        result = self._export()
        self.assertTrue(result["complete"])
        with h5py.File(self.output) as file:
            np.testing.assert_array_equal(file["x"][...], expected)

    def test_old_baseline_schema_and_alias_conflicts_refuse(self) -> None:
        self._source(latest=False)
        with self.assertRaisesRegex(RecoveryError, "distinct files"):
            export_verified_chunks(self.source, "/x", self.source,
                                   hashlib.sha256(self.source.read_bytes()).hexdigest(),
                                   self.output, self.report)
        self.source.unlink()
        with h5py.File(self.source, "w", libver="earliest") as file:
            file.create_dataset("x", data=np.arange(48, dtype="<u4").reshape(6, 8),
                                chunks=(2, 2))
        with self.assertRaisesRegex(RecoveryError, "contradicts damaged dataset"):
            self._export()
        self.assertFalse(self.output.exists())
