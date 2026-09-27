"""Public prospective element capture and current damaged-file reconciliation."""

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

from h5reclaim.nonchunked_recovery import read_nonchunked_spec


class ElementBaselineCliTests(unittest.TestCase):
    def test_prior_capture_detects_silent_element_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "science.h5"
            values = np.arange(20, dtype="<u4")
            with h5py.File(source, "w", libver="latest") as file:
                file.create_dataset("science", data=values)
            offset = read_nonchunked_spec(source, "/science").source_absolute_offset
            self.assertIsNotNone(offset)
            baseline = root / "baseline.zip"
            capture = subprocess.run([
                sys.executable, "-m", "h5reclaim", "capture-element-baseline",
                str(source), "--dataset", "/science", "--output", str(baseline),
            ], capture_output=True, text=True, check=False)
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline_hash = hashlib.sha256(baseline.read_bytes()).hexdigest()
            self.assertIn(baseline_hash, capture.stdout)
            raw = bytearray(source.read_bytes())
            raw[offset + 7 * 4] ^= 1
            source.write_bytes(raw)
            damaged_hash = hashlib.sha256(raw).hexdigest()
            with h5py.File(source, "r") as file:
                self.assertNotEqual(file["science"][7], values[7])
            output, report = root / "result.h5", root / "evidence.json"
            rescue = subprocess.run([
                sys.executable, "-m", "h5reclaim", "rescue", str(source),
                "--dataset", "/science", "--element-baseline", str(baseline),
                "--element-baseline-sha256", baseline_hash,
                "--output", str(output), "--report", str(report),
            ], capture_output=True, text=True, check=False)
            self.assertEqual(rescue.returncode, 0, rescue.stderr)
            evidence = json.loads(report.read_text())
            self.assertEqual(evidence["counts"], {"recovered": 19, "unknown": 1})
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), damaged_hash)
            with h5py.File(output, "r") as file:
                status = file["/_h5reclaim/element_status"][:]
                np.testing.assert_array_equal(file["science"][:][status == 1], values[status == 1])
                self.assertEqual(file["science"][7], 0)
                self.assertEqual(status[7], 0)

    def test_digest_required_with_element_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.h5"
            with h5py.File(source, "w") as file:
                file.create_dataset("x", data=np.arange(3))
            result = subprocess.run([
                sys.executable, "-m", "h5reclaim", "rescue", str(source),
                "--dataset", "/x", "--element-baseline", str(root / "absent.zip"),
                "--output", str(root / "out.h5"), "--report", str(root / "out.json"),
            ], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must be supplied together", result.stderr)
            self.assertFalse((root / "out.h5").exists())


if __name__ == "__main__":
    unittest.main()
