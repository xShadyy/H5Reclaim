"""Held-out seeded layout matrix and an independent false-acceptance check."""

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

from run_stratified_layouts import _fault, _generate, _score, digest  # noqa: E402


class StratifiedLayoutTests(unittest.TestCase):
    def test_layout_matrix_and_deliberate_false_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "matrix"
            process = subprocess.run(
                [sys.executable, str(ROOT / "benchmarks" / "run_stratified_layouts.py"),
                 "--seed", "11235813", "--trials", "1", "--work-dir", str(directory), "--json"],
                cwd=ROOT, capture_output=True, text=True, check=False, timeout=150,
            )
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout[-1500:])
            result = json.loads(process.stdout)
            self.assertEqual(result, json.loads((directory / "stratified.json").read_text()))
            self.assertTrue(result["all_cases_passed"])
            self.assertEqual(result["case_count"], 28)
            self.assertEqual(result["verified_authentic_originals"], 4)
            self.assertEqual(result["historical_mismatch_regions"], 1)
            self.assertEqual(result["unverified_mismatch_regions"], 1)
            cases = {case["name"]: case for case in result["cases"]}
            self.assertEqual(cases["real_gwosc_4khz"]["observed"]["exact_regions"], 64)
            self.assertEqual(cases["sparse_edge_readable"]["observed"]["unknown_regions"], 7)
            self.assertEqual(cases["trial_000_fixed_array_competing_pointer"]["observed"]["decision"], "refused")
            self.assertEqual(cases["trial_000_filtered_filtered_payload"]["observed"]["unknown_regions"], 1)
            for case in result["cases"]:
                self.assertTrue(case["passed"], case["name"])
                self.assertEqual(digest(Path(case["input"])), case["input_sha256"])
                if case["observed"]["decision"] == "refused":
                    self.assertFalse((directory / "cases" / case["name"] / "output.h5").exists())
                    self.assertFalse((directory / "cases" / case["name"] / "report.json").exists())

            # A changed measurement behind a valid filtered source checksum
            # must be detected independently of the tool's evidence report.
            name = "filtered_single"
            original = directory / "truth" / "filtered.h5"
            trial = Path(cases[name]["input"])
            output = directory / "cases" / name / "output.h5"
            report = directory / "cases" / name / "report.json"
            with h5py.File(output, "r+") as handle:
                handle["/science"][0] = 1000.0
            scored = _score(original, trial, output, report, "/science", "recover")
            self.assertEqual(scored["wrong_historical_regions"], 1)
            self.assertEqual(scored["wrong_with_unverified_integrity"], 0)
            self.assertEqual(scored["exact_regions"], 0)

    def test_generated_contents_and_fault_site_change_with_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first, second, copy1, copy2, copy3 = (base / name for name in
                                                   ("a.h5", "b.h5", "copy1.h5", "copy2.h5", "copy3.h5"))
            _generate(first, "single", np.random.default_rng(3))
            _generate(second, "single", np.random.default_rng(4))
            self.assertNotEqual(digest(first), digest(second))
            before = digest(first)
            a = _fault(first, copy1, "unchecked_payload", 7)
            b = _fault(first, copy2, "unchecked_payload", 7)
            c = _fault(first, copy3, "unchecked_payload", 8)
            self.assertEqual((a, digest(copy1)), (b, digest(copy2)))
            self.assertNotEqual((a, digest(copy1)), (c, digest(copy3)))
            self.assertEqual(digest(first), before)


if __name__ == "__main__":
    unittest.main()
