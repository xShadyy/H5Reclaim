"""Public prospective captures and scored rescue on pinned authentic sources."""

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

from run_authentic_baseline_integrity import score_elements  # noqa: E402


class AuthenticBaselineIntegrityTests(unittest.TestCase):
    def test_public_trials_and_independent_scorer_detect_false_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "authentic"
            process = subprocess.run(
                [sys.executable, str(ROOT / "benchmarks" / "run_authentic_baseline_integrity.py"),
                 "--work-dir", str(folder), "--json"],
                cwd=ROOT, capture_output=True, text=True, check=False, timeout=100,
            )
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout[-900:])
            result = json.loads(process.stdout)
            self.assertEqual(result, json.loads((folder / "integrity_trials.json").read_text()))
            self.assertTrue(result["all_cases_passed"])
            self.assertEqual(result["controlled_cases"], 2)
            cases = {case["source_id"]: case for case in result["cases"]}
            quantum = cases["zenodo_qubit_feedback"]
            gwosc = cases["gwosc_gw150914_h1_strain"]
            self.assertEqual((quantum["observed"]["exact_accepted"], quantum["observed"]["unknown"]),
                             (1999, 1))
            self.assertEqual(quantum["observed"]["native_plausible_changed_values"], 1)
            self.assertEqual((gwosc["observed"]["exact_accepted"], gwosc["observed"]["unknown"]),
                             (63, 1))
            self.assertTrue(all(case["tampered_baseline_refused"] for case in cases.values()))

            case_dir = folder / "quantum_elements"
            original = ROOT / "corpus" / "files" / "fast_feedback_raw_data.h5"
            output = case_dir / "integrity_output.h5"
            with h5py.File(output, "r+") as file:
                dataset = file[quantum["dataset"]]
                dataset[0, 0] = int(dataset[0, 0]) ^ 1
            scored = score_elements(
                original, case_dir / "damaged_copy.h5", case_dir / "prior_element_hashes.zip",
                quantum["baseline_sha256"], output, case_dir / "evidence.json",
                quantum["dataset"], (127, 1),
            )
            self.assertEqual(scored["false_accepted"], 1)
            self.assertTrue(scored["issues"])


if __name__ == "__main__":
    unittest.main()
