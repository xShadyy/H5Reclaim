"""Independently score all current authentic structural candidates."""

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

from run_real_candidate_exports import score  # noqa: E402


class RealCandidateExportsTests(unittest.TestCase):
    def test_public_exports_and_evaluator_detects_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / "candidates"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "benchmarks" / "run_real_candidate_exports.py"),
                 "--work-dir", str(work), "--json"], cwd=ROOT,
                capture_output=True, text=True, timeout=90, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout[-800:])
            result = json.loads(completed.stdout)
            self.assertEqual(result, json.loads((work / "candidate_exports.json").read_text()))
            self.assertEqual(result["original_files_verified"], 4)
            self.assertEqual(result["dataset_count"], 251)
            self.assertEqual(result["survey_candidate_count"], 6)
            self.assertEqual(result["candidate_cases"], 6)
            self.assertEqual((result["exact_chunks"], result["wrong_chunks"]), (196, 0))
            self.assertTrue(result["all_cases_passed"])
            self.assertTrue(all(case["passed"] for case in result["cases"]))
            mask = next(case for case in result["cases"]
                        if case["source_id"] == "gwosc_gw150914_h1_strain"
                        and case["dataset"] == "/quality/simple/DQmask")
            folder = Path(mask["input"]).parent
            original = ROOT / "corpus" / "files" / "H-H1_GWOSC_4KHZ_R1-1126259447-32.hdf5"
            output = folder / "exported.h5"
            with h5py.File(output, "r+") as file:
                selected = file["/quality/simple/DQmask"]
                selected[0] = int(selected[0]) ^ 1
            result = score(original, Path(mask["input"]), output,
                           folder / "evidence.json", mask["dataset"])
            self.assertEqual((result["exact_chunks"], result["wrong_chunks"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
