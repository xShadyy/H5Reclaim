"""Independent truth scoring of seeded faults on unchanged scientific files."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

from run_seeded_matrix import _scenarios, _verify_manifest, score_output  # noqa: E402


class SeededMatrixTest(unittest.TestCase):
    def test_seeded_real_file_matrix_and_false_value_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_dir = Path(temporary) / "matrix"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "benchmarks" / "run_seeded_matrix.py"),
                 "--seed", "23", "--trials", "1", "--work-dir", str(work_dir), "--json"],
                cwd=ROOT, capture_output=True, text=True, timeout=150, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report, json.loads((work_dir / "matrix.json").read_text(encoding="utf-8")))
            self.assertEqual(report["original_files_verified"], 4)
            self.assertEqual(report["case_count"], 13)
            self.assertTrue(report["all_cases_passed"])
            self.assertEqual(report["false_accepted_chunks"], 0)
            self.assertEqual(report["newly_accessible_exact_chunks"], 113)
            cases = {case["case"]: case for case in report["cases"]}
            self.assertEqual(cases["trial_000_index_link"]["observed"]["exact_verified_chunks"], 128)
            self.assertEqual(cases["trial_000_index_link"]["observed"]["reconstructed_link_chunks"], 57)
            self.assertEqual(cases["trial_000_payload"]["observed"]["unresolved_chunks"], 1)
            self.assertEqual(cases["trial_000_link_and_payload"]["observed"]["unresolved_chunks"], 1)
            self.assertEqual(cases["trial_000_direct_payload"]["observed"]["exact_verified_chunks"], 63)
            for case in report["cases"]:
                self.assertTrue(case["passed"], case["case"])
                entry = next(item for item in json.loads((ROOT / "corpus" / "manifest.json").read_text())[
                    "entries"] if item["id"] == case["source_id"])
                original = ROOT / "corpus" / entry["path"]
                self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), case["original_sha256"])
                self.assertEqual(hashlib.sha256(Path(case["input"]).read_bytes()).hexdigest(), case["damaged_sha256"])
                if case["observed"]["decision"] == "refused":
                    self.assertFalse((work_dir / case["case"] / "recovered.h5").exists())
                    self.assertFalse((work_dir / case["case"] / "recovery.json").exists())

            # If a measurement is altered after a successful run, the scorer
            # must count it as falsely accepted, even though the validity map
            # and structural address in the report still claim success.
            baseline = cases["gwosc_4khz_intact"]
            entry = next(item for item in json.loads((ROOT / "corpus" / "manifest.json").read_text())[
                "entries"] if item["id"] == baseline["source_id"])
            with h5py.File(work_dir / "gwosc_4khz_intact" / "recovered.h5", "r+") as handle:
                handle["/strain/Strain"][0] = 123.0
            scored = score_output(
                ROOT / "corpus" / entry["path"], Path(baseline["input"]),
                work_dir / "gwosc_4khz_intact" / "recovered.h5",
                work_dir / "gwosc_4khz_intact" / "recovery.json",
            )
            self.assertEqual(scored["wrong_value_chunks"], 1)
            self.assertEqual(scored["false_accepted_chunks"], 1)
            self.assertEqual(scored["false_accepted_indices"], [0])
            self.assertEqual(scored["exact_verified_chunks"], 63)
            self.assertFalse(scored["safety_passed"])

    def test_seed_reproduces_sites_and_changes_mutation_sites(self) -> None:
        indexed = _verify_manifest()
        one = _scenarios(indexed, seed=23, trials=2)
        again = _scenarios(indexed, seed=23, trials=2)
        other = _scenarios(indexed, seed=24, trials=2)
        self.assertEqual(one, again)
        targets = lambda cases: [(trial, case.name, case.target_chunk,
                                  tuple((item.offset, item.after) for item in case.mutations))
                                 for trial, case in cases if trial is not None]
        self.assertNotEqual(targets(one), targets(other))
        self.assertEqual(len(one), 23)


if __name__ == "__main__":
    unittest.main()
