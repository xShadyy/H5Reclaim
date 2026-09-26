"""Black-box recovery and independent exact-placement benchmark checks."""

from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.run_recovery import (  # noqa: E402
    BenchmarkError, evaluate, result_exit_code, run_trial,
)


class EvaluationTest(unittest.TestCase):
    def test_wrong_placement_and_missing_region_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pristine = root / "pristine.h5"
            output = root / "output.h5"
            report = root / "report.json"
            truth = np.arange(16, dtype="<u4").reshape((4, 4))
            with h5py.File(pristine, "w") as handle:
                handle.create_dataset("measurements", data=truth, chunks=(2, 2))
            with h5py.File(output, "w") as handle:
                values = truth.copy()
                values[:2, :2] = truth[2:4, 2:4]
                handle.create_dataset("measurements", data=values)
                handle.create_dataset("/_h5reclaim/chunk_status", data=np.array(
                    [[1, 1], [2, 1]], dtype="uint8"
                ))
            report.write_text(json.dumps({
                "complete": False, "outcome": "partial",
                "source": {
                    "sha256_before": hashlib.sha256(pristine.read_bytes()).hexdigest(),
                    "sha256_after": hashlib.sha256(pristine.read_bytes()).hexdigest(),
                    "size_bytes": pristine.stat().st_size,
                },
                "dataset": {
                    "path": "/measurements", "shape": [4, 4], "chunks": [2, 2],
                    "chunk_grid": [2, 2],
                },
                "counts": {
                    "recovered": 3, "allocation_unknown": 1, "ambiguous": 0,
                    "unavailable": 0, "unsupported": 0, "decode_failed": 0,
                },
                "mappings": [
                    {"chunk_index": [i, j], "coordinate": [i * 2, j * 2],
                     "route": "intact_tree"}
                    for i, j in ((0, 0), (0, 1), (1, 1))
                ],
            }), encoding="utf-8")

            summary = evaluate(pristine, pristine, output, report)
            self.assertEqual(summary["wrong_placement_coordinates"], [[0, 0]])
            self.assertEqual(summary["wrong_placement_chunks"], 1)
            self.assertEqual(summary["incorrect_values_chunks"], 0)
            self.assertEqual(summary["missing_region_chunks"], 1)
            self.assertEqual(summary["status_counts"]["allocation_unknown"], 1)
            self.assertEqual(result_exit_code({
                **summary, "native_unavailable_chunks": 1,
                "recovered_from_native_unavailable_chunks": 1,
            }), 1)

            altered_report = json.loads(report.read_text(encoding="utf-8"))
            altered_report["counts"]["recovered"] = 4
            report.write_text(json.dumps(altered_report), encoding="utf-8")
            with self.assertRaisesRegex(BenchmarkError, "conflicts"):
                evaluate(pristine, pristine, output, report)

    def test_incorrect_bytes_and_duplicate_matches_are_not_called_placement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pristine = root / "pristine.h5"
            output = root / "output.h5"
            report = root / "report.json"
            truth = np.zeros((4, 6), dtype="<u4")
            for i, j, value in (
                (0, 0, 1), (0, 1, 10), (0, 2, 10),
                (1, 0, 20), (1, 1, 30), (1, 2, 40),
            ):
                truth[i * 2:(i + 1) * 2, j * 2:(j + 1) * 2] = value
            with h5py.File(pristine, "w") as handle:
                handle.create_dataset("measurements", data=truth, chunks=(2, 2))
            values = truth.copy()
            values[0, 0] = 999  # No reference chunk has these exact bytes.
            values[2:4, 0:2] = truth[0:2, 2:4]  # Matches two reference chunks.
            with h5py.File(output, "w") as handle:
                handle.create_dataset("measurements", data=values)
                handle.create_dataset(
                    "/_h5reclaim/chunk_status", data=np.ones((2, 3), dtype="uint8")
                )
            source_hash = hashlib.sha256(pristine.read_bytes()).hexdigest()
            report.write_text(json.dumps({
                "complete": True, "outcome": "complete",
                "source": {
                    "sha256_before": source_hash, "sha256_after": source_hash,
                    "size_bytes": pristine.stat().st_size,
                },
                "dataset": {
                    "path": "/measurements", "shape": [4, 6], "chunks": [2, 2],
                    "chunk_grid": [2, 3],
                },
                "counts": {
                    "recovered": 6, "allocation_unknown": 0, "ambiguous": 0,
                    "unavailable": 0, "unsupported": 0, "decode_failed": 0,
                },
                "mappings": [
                    {"chunk_index": [i, j], "coordinate": [i * 2, j * 2],
                     "route": "intact_tree"}
                    for i in range(2) for j in range(3)
                ],
            }), encoding="utf-8")

            summary = evaluate(pristine, pristine, output, report)
            self.assertEqual(summary["wrong_placement_chunks"], 0)
            self.assertEqual(summary["incorrect_values_coordinates"], [[0, 0], [1, 0]])
            self.assertEqual(summary["incorrect_values_chunks"], 2)
            self.assertEqual(result_exit_code({
                **summary, "native_unavailable_chunks": 1,
                "recovered_from_native_unavailable_chunks": 1,
            }), 1)
            self.assertEqual(result_exit_code({
                **summary, "incorrect_values_chunks": 0,
                "native_unavailable_chunks": 1,
                "recovered_from_native_unavailable_chunks": 1,
            }), 0)


class EndToEndTest(unittest.TestCase):
    def test_broken_link_recovery_has_exact_placement_and_keeps_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = run_trial(
                Path(directory) / "trial", rows=512, cols=512,
                chunk_rows=16, chunk_cols=16, seed=0x48355245434C4149,
            )
            self.assertTrue(summary["source_preserved"])
            self.assertTrue(summary["pristine_preserved"])
            self.assertEqual(summary["expected_chunks"], 1024)
            self.assertEqual(summary["wrong_placement_chunks"], 0)
            self.assertEqual(summary["incorrect_values_chunks"], 0)
            self.assertEqual(summary["missing_region_chunks"], 0)
            self.assertGreater(summary["native_unavailable_chunks"], 0)
            self.assertGreater(summary["recovered_from_native_unavailable_chunks"], 0)
            self.assertGreater(summary["reconstructed_link_mappings"], 0)


if __name__ == "__main__":
    unittest.main()
