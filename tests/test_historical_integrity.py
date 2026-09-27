"""Prior-capture comparison is distinct from an apparently readable value."""

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
from h5reclaim.historical_integrity import finalize_staged_history
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.payload_integrity import capture_element_baseline, export_verified_nonchunked
from h5reclaim.recovery import RecoveryError, recover
from h5reclaim.recovery_capsule import capture_recovery_capsule, restore_from_capsule


class HistoricalIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "science.h5"
        self.baseline = self.root / "prior.json"
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("/science", data=np.arange(16, dtype="<i4"), chunks=(8,))
        capture_baseline(self.source, "/science", self.baseline)
        self.baseline_hash = hashlib.sha256(self.baseline.read_bytes()).hexdigest()

    def _paths(self, stem: str) -> tuple[Path, Path]:
        return self.root / f"{stem}.h5", self.root / f"{stem}.json"

    def test_silent_unchecksummed_mutation_has_no_prior_match_without_capture_gate(self) -> None:
        with h5py.File(self.source, "r+") as handle:
            handle["/science"][0] = 123456
        output, report = self._paths("structural")
        original = recover(self.source, "/science", output, report)
        self.assertEqual(original["counts"]["recovered"], 2)
        with self.assertRaisesRegex(UnsupportedCase, "prior capture"):
            finalize_staged_history(output, report, strict=True)
        result = finalize_staged_history(output, report)
        self.assertEqual(result["historical_integrity"]["matching_units"], 0)
        self.assertEqual(result["historical_integrity"]["unknown_units"], 2)
        with h5py.File(output, "r") as handle:
            np.testing.assert_array_equal(handle["/_h5reclaim/chunk_status"][:], [1, 1])
            np.testing.assert_array_equal(handle["/_h5reclaim/historical_status"][:], [0, 0])
            self.assertEqual(int(handle["/science"][0]), 123456)
            self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()])["historical_integrity"],
                             result["historical_integrity"])

    def test_strict_prior_baseline_gates_changed_chunk_and_keeps_matching_chunk(self) -> None:
        with h5py.File(self.source, "r+") as handle:
            handle["/science"][0] = 123456
        output, report = self._paths("verified")
        export_verified_chunks(self.source, "/science", self.baseline,
                               self.baseline_hash, output, report)
        result = finalize_staged_history(output, report, strict=True)
        self.assertEqual(result["historical_integrity"]["matching_units"], 1)
        self.assertEqual(result["historical_integrity"]["unknown_units"], 1)
        self.assertEqual(result["historical_integrity"]["capture_sha256"], self.baseline_hash)
        self.assertFalse(result["historical_integrity"]["capture_time_authenticated"])
        with h5py.File(output, "r") as handle:
            np.testing.assert_array_equal(handle["/_h5reclaim/chunk_status"][:], [4, 1])
            np.testing.assert_array_equal(handle["/_h5reclaim/historical_status"][:], [0, 1])
            self.assertEqual(int(handle["/science"][0]), 0)
            np.testing.assert_array_equal(handle["/science"][8:], np.arange(8, 16))

    def test_bogus_prior_route_without_pinned_digest_is_rejected(self) -> None:
        output, report = self._paths("invalid")
        recover(self.source, "/science", output, report)
        evidence = json.loads(report.read_text())
        evidence["operation"] = "prospective_chunk_baseline_reconciliation"
        report.write_text(json.dumps(evidence), encoding="utf-8")
        with self.assertRaisesRegex(RecoveryError, "capture evidence"):
            finalize_staged_history(output, report, strict=True)
        with h5py.File(output, "r") as handle:
            self.assertNotIn("historical_status", handle["/_h5reclaim"])

    def test_pristine_but_unprotected_current_chunks_remain_historically_unknown(self) -> None:
        output, report = self._paths("pristine")
        recover(self.source, "/science", output, report)
        result = finalize_staged_history(output, report)
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(result["historical_integrity"]["matching_units"], 0)
        self.assertIsNone(result["historical_integrity"]["capture_sha256"])

    def test_element_baseline_has_per_element_historical_map(self) -> None:
        contiguous = self.root / "contiguous.h5"
        with h5py.File(contiguous, "w", libver="latest") as handle:
            handle.create_dataset("/science", data=np.arange(12, dtype="<i4"))
        baseline = self.root / "elements.zip"
        digest = capture_element_baseline(contiguous, "/science", baseline)["archive_sha256"]
        with h5py.File(contiguous, "r+") as handle:
            handle["/science"][3] = 999
        output, report = self._paths("elements")
        export_verified_nonchunked(contiguous, "/science", baseline, digest, output, report)
        result = finalize_staged_history(output, report, strict=True)
        self.assertEqual(result["historical_integrity"]["matching_units"], 11)
        with h5py.File(output) as handle:
            expected = np.ones(12, dtype="u1")
            expected[3] = 0
            np.testing.assert_array_equal(handle["/_h5reclaim/historical_status"][:], expected)

    def test_capsule_full_chunks_keep_prior_capture_status_after_root_loss(self) -> None:
        capsule = self.root / "capsule.zip"
        digest = capture_recovery_capsule(self.source, "/science", capsule)["archive_sha256"]
        damaged = self.root / "root_lost.h5"
        damaged.write_bytes(self.source.read_bytes())
        with damaged.open("r+b") as stream:
            stream.write(b"DESTROYED" + b"\0" * 247)
        output, report = self._paths("capsule")
        restore_from_capsule(damaged, capsule, digest, output, report, dataset_path="/science")
        result = finalize_staged_history(output, report, strict=True)
        self.assertEqual(result["historical_integrity"]["matching_units"], 16)
        with h5py.File(output) as handle:
            self.assertEqual(handle["/_h5reclaim/historical_status"].shape, (16,))
            np.testing.assert_array_equal(handle["/_h5reclaim/historical_status"][:],
                                          np.ones(16, dtype="u1"))


if __name__ == "__main__":
    unittest.main()
