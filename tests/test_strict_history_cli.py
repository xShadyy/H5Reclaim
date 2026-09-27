"""Public strict policy refuses unprotected current data and gates prior hashes."""

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


class StrictHistoryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "readings.h5"
        self.output = self.root / "derived.h5"
        self.report = self.root / "report.json"
        self.baseline = self.root / "prior.json"
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("/measurements", data=np.arange(16, dtype="<u4"), chunks=(8,))

    def _rescue(self, *options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "h5reclaim", "rescue", str(self.source),
             "--dataset", "/measurements", "--output", str(self.output),
             "--report", str(self.report), *options],
            capture_output=True, text=True, check=False,
        )

    def test_strict_mode_without_prior_capture_does_not_publish_current_values(self) -> None:
        result = self._rescue("--strict-history")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("prior capture", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_strict_chunk_baseline_marks_silent_change_unknown(self) -> None:
        captured = subprocess.run(
            [sys.executable, "-m", "h5reclaim", "capture-baseline", str(self.source),
             "--dataset", "/measurements", "--output", str(self.baseline)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(captured.returncode, 0, captured.stderr)
        prior = hashlib.sha256(self.baseline.read_bytes()).hexdigest()
        with h5py.File(self.source, "r+") as handle:
            handle["/measurements"][0] = 999999
        changed = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self._rescue("--strict-history", "--chunk-baseline", str(self.baseline),
                              "--chunk-baseline-sha256", prior)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), changed)
        evidence = json.loads(self.report.read_text())
        self.assertEqual(evidence["historical_integrity"]["policy"], "require_prior_capture_match")
        self.assertEqual(evidence["historical_integrity"]["matching_units"], 1)
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle["/_h5reclaim/historical_status"][:], [0, 1])
            self.assertEqual(int(handle["/measurements"][0]), 0)
            np.testing.assert_array_equal(handle["/measurements"][8:], np.arange(8, 16))
            self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()]), evidence)


if __name__ == "__main__":
    unittest.main()
