"""Public-CLI evidence across authentic layouts, not an arbitrary damage claim."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.run_damage_catalog import CatalogError, _mutated_copy  # noqa: E402


class DamageCatalogTest(unittest.TestCase):
    def test_public_catalog_checks_exact_chunks_and_safe_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_dir = Path(temporary) / "cases"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "benchmarks" / "run_damage_catalog.py"),
                 "--work-dir", str(work_dir), "--json"],
                cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report["four_originals_verified"], 4)
            self.assertTrue(report["all_cases_passed"])
            self.assertEqual(report["case_count"], 10)
            self.assertEqual(report["reference_gwosc_chunks"], {"16khz": 128, "4khz": 64})
            self.assertEqual(
                json.loads((work_dir / "catalog.json").read_text(encoding="utf-8")), report,
            )
            cases = {case["case"]: case for case in report["cases"]}
            self.assertEqual(cases["one_index_link"]["observed"]["exact_verified_chunks"], 128)
            self.assertEqual(cases["one_index_link"]["observed"]["reconstructed_link_chunks"], 57)
            self.assertEqual(cases["gwosc_4khz_intact"]["observed"]["exact_verified_chunks"], 64)
            self.assertEqual(cases["gwosc_4khz_intact"]["observed"]["reconstructed_link_chunks"], 0)
            for name, reconstructed in (("corrupt_payload", 0), ("index_and_payload", 56)):
                observation = cases[name]["observed"]
                self.assertEqual(observation["decision"], "partial")
                self.assertEqual(observation["exact_verified_chunks"], 127)
                self.assertEqual(observation["rejected_corrupt_chunks"], 1)
                self.assertEqual(observation["reconstructed_link_chunks"], reconstructed)
                self.assertEqual(observation["status_counts"]["decode_failed"], 1)
                self.assertEqual(observation["wrong_claimed_chunks"], 0)
            four_khz_corruption = cases["gwosc_4khz_corrupt_payload"]["observed"]
            self.assertEqual(four_khz_corruption["decision"], "partial")
            self.assertEqual(four_khz_corruption["exact_verified_chunks"], 63)
            self.assertEqual(four_khz_corruption["rejected_corrupt_chunks"], 1)
            self.assertEqual(four_khz_corruption["reconstructed_link_chunks"], 0)
            for name in (
                "two_index_links", "selected_object_header", "gwosc_4khz_missing_payload_pointer",
                "zenodo_qubit_feedback", "zenodo_pallas_cloud_aircraft",
            ):
                self.assertEqual(cases[name]["observed"]["decision"], "refused")
                self.assertFalse((work_dir / name / "recovered.h5").exists())
                self.assertFalse((work_dir / name / "recovery.json").exists())
            for case in cases.values():
                original = ROOT / "corpus" / "files" / case["source_id"]
                digest = hashlib.sha256(original.read_bytes()).hexdigest()
                self.assertEqual(digest, case["original_sha256"])
                trial = Path(case["trial_input"])
                self.assertEqual(hashlib.sha256(trial.read_bytes()).hexdigest(), case["trial_input_sha256"])

    def test_byte_mutation_preconditions_fail_before_creating_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "source.bin", root / "mutated.bin"
            source.write_bytes(b"abcdef")
            for changes in (
                [(1, b"z", b"Z")],
                [(0, b"abc", b"123"), (2, b"cd", b"XY")],
                [(5, b"fg", b"xy")],
            ):
                with self.subTest(changes=changes), self.assertRaises(CatalogError):
                    _mutated_copy(source, destination, changes)
                self.assertFalse(destination.exists())
            self.assertEqual(source.read_bytes(), b"abcdef")


if __name__ == "__main__":
    unittest.main()
