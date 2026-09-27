"""Public prior chunk baseline detects a silently changed current payload."""

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

from h5reclaim.recovery import analyze


class ChunkBaselineCliTests(unittest.TestCase):
    def test_public_rescue_with_prior_capture_withholds_changed_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "science.h5"
            expected = np.arange(16, dtype="<u4").reshape(4, 4)
            with h5py.File(source, "w", libver="latest") as file:
                file.create_dataset("science", data=expected, chunks=(2, 2))
            baseline = root / "prior.json"
            captured = subprocess.run([
                sys.executable, "-m", "h5reclaim", "capture-baseline", str(source),
                "--dataset", "/science", "--output", str(baseline),
            ], capture_output=True, text=True, check=False)
            self.assertEqual(captured.returncode, 0, captured.stderr)
            baseline_sha = hashlib.sha256(baseline.read_bytes()).hexdigest()
            record = next(record for record in analyze(source, "/science").records
                          if record.coordinate == (0, 0))
            raw = bytearray(source.read_bytes())
            raw[record.absolute_offset] ^= 1
            source.write_bytes(raw)
            source_sha = hashlib.sha256(raw).hexdigest()
            with h5py.File(source, "r") as file:
                self.assertNotEqual(file["science"][0, 0], expected[0, 0])
            output, report = root / "out.h5", root / "evidence.json"
            result = subprocess.run([
                sys.executable, "-m", "h5reclaim", "rescue", str(source),
                "--dataset", "/science", "--chunk-baseline", str(baseline),
                "--chunk-baseline-sha256", baseline_sha,
                "--output", str(output), "--report", str(report),
            ], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(report.read_text())
            self.assertEqual(evidence["operation"], "prospective_chunk_baseline_reconciliation")
            self.assertEqual(evidence["counts"]["recovered"], 3)
            self.assertEqual(evidence["counts"]["unavailable"], 1)
            with h5py.File(output, "r") as file:
                valid = file["/_h5reclaim/chunk_status"][:]
                self.assertEqual(valid[0, 0], 4)
                self.assertTrue(np.all(file["science"][:2, :2] == 0))
                np.testing.assert_array_equal(file["science"][2:, :], expected[2:, :])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_sha)


if __name__ == "__main__":
    unittest.main()
