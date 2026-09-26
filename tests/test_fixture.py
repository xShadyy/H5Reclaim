"""Meaningful checks of the public fixture command and its output."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tools" / "make_healthy_fixture.py"


class HealthyFixtureTest(unittest.TestCase):
    def test_small_fixture_exact_values_and_allocated_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "healthy.h5"
            result = subprocess.run(
                [
                    sys.executable,
                    str(GENERATOR),
                    "--output",
                    str(path),
                    "--rows",
                    "64",
                    "--cols",
                    "96",
                    "--chunk-rows",
                    "8",
                    "--chunk-cols",
                    "12",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("verified elements: 6144", result.stdout)
            with h5py.File(path, "r") as handle:
                dataset = handle["/measurements"]
                self.assertEqual(dataset.chunks, (8, 12))
                self.assertEqual(dataset.id.get_num_chunks(), 64)
                self.assertEqual(dataset.id.get_storage_size(), 6144 * 4)
                expected = np.arange(6144, dtype="<u4").reshape(64, 96)
                np.testing.assert_array_equal(dataset[...], expected)

            repeated = subprocess.run(
                [sys.executable, str(GENERATOR), "--output", str(path)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(repeated.returncode, 0)
            with h5py.File(path, "r") as handle:
                np.testing.assert_array_equal(handle["/measurements"][...], expected)


if __name__ == "__main__":
    unittest.main()
