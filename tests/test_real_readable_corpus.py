"""Authentic Zenodo values are scored independently of native-readable export."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

from run_real_readable_corpus import score  # noqa: E402


class RealReadableCorpusTests(unittest.TestCase):
    def test_zenodo_current_values_and_independent_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "readable"
            process = subprocess.run(
                [sys.executable, str(ROOT / "benchmarks" / "run_real_readable_corpus.py"),
                 "--work-dir", str(folder), "--json"], cwd=ROOT,
                capture_output=True, text=True, check=False, timeout=90,
            )
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout[-900:])
            result = json.loads(process.stdout)
            self.assertEqual(result, json.loads((folder / "readable_corpus.json").read_text()))
            self.assertEqual(result["case_count"], 2)
            self.assertEqual(result["structural_candidates_in_corpus"], 6)
            self.assertTrue(result["all_cases_passed"])
            cases = {case["source_id"]: case for case in result["cases"]}
            quantum = cases["zenodo_qubit_feedback"]
            aircraft = cases["zenodo_pallas_cloud_aircraft"]
            self.assertEqual(quantum["observed"]["elements"], 2000)
            self.assertEqual(aircraft["observed"]["elements"], 2088)
            self.assertEqual(aircraft["observed"]["record_fields"], 37)
            self.assertTrue(all(case["passed"] for case in result["cases"]))

            copied = Path(quantum["input"])
            output = copied.parent / "readable.h5"
            original = ROOT / "corpus" / "files" / "fast_feedback_raw_data.h5"
            with h5py.File(output, "r+") as file:
                selected = file[quantum["dataset"]]
                selected[0, 0] = int(selected[0, 0]) ^ 1
            observed = score(original, copied, output, copied.parent / "evidence.json",
                             quantum["dataset"])
            self.assertFalse(observed["exact_current_values"])


if __name__ == "__main__":
    unittest.main()
