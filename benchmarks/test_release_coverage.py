"""Adversarial checks for independent release-panel scoring."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

import h5py

from benchmarks.run_release_coverage import generate, run_case, score


class ReleaseScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="h5reclaim-score-check-")
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / "source.h5"
        generate(cls.source, "fixed_array", 42)
        cls.case = run_case(cls.source, "fixed_array", "intact", None, cls.root, timeout=90)
        if not cls.case["observed"]["fully_exact"]:
            raise AssertionError(cls.case)
        cls.output = cls.root / "cases/fixed_array-intact/output.h5"
        cls.report_path = cls.root / "cases/fixed_array-intact/report.json"
        cls.report = json.loads(cls.report_path.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def copied_output(self):
        destination = self.root / (self._testMethodName + ".h5")
        shutil.copyfile(self.output, destination)
        return destination

    def test_one_wrong_accepted_value_is_counted_and_fails_usefulness(self):
        output = self.copied_output()
        with h5py.File(output, "r+") as handle:
            handle["science"][0, 0] += 1
        result = score(self.source, output, self.report_path)
        self.assertEqual(result["wrong_accepted_elements"], 1)
        self.assertEqual(result["exact_accepted_elements"], 76)
        self.assertFalse(result["useful_output"])
        self.assertFalse(result["fully_exact"])

    def test_unknown_mask_cannot_be_reported_complete(self):
        output = self.copied_output()
        selected = self.report["datasets"][0]["report"]
        status_path = selected.get("validity_map") or selected["whole_file_metadata_group"] + "/chunk_status"
        with h5py.File(output, "r+") as handle:
            handle[status_path][0, 0] = 6
        result = score(self.source, output, self.report_path)
        self.assertEqual(result["unknown_elements"], 12)
        self.assertIn("complete report contains unresolved coordinates", result["evaluation_errors"])
        self.assertFalse(result["useful_output"])

    def test_historical_match_without_capture_is_rejected(self):
        output = self.copied_output()
        path = self.report["datasets"][0]["report"]["historical_integrity"]["prior_capture_match_status_dataset"]
        with h5py.File(output, "r+") as handle:
            handle[path][0, 0] = 1
        result = score(self.source, output, self.report_path)
        self.assertTrue(any("historical equality claimed" in error for error in result["evaluation_errors"]))
        self.assertFalse(result["useful_output"])

    def test_unrelated_omission_does_not_hide_a_missing_attribute(self):
        output = self.copied_output()
        report = json.loads(json.dumps(self.report))
        report["scientific_context"]["datasets"][0]["attributes_omitted"] = ["unrelated"]
        report_path = self.root / "unrelated-omission.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with h5py.File(output, "r+") as handle:
            del handle["science"].attrs["units"]
            handle[report["metadata_group"] + "/report_json"][()] = json.dumps(report)
        result = score(self.source, output, report_path)
        self.assertIn("attribute differs: science:units", result["evaluation_errors"])
        self.assertFalse(result["useful_output"])

    def test_refused_fault_keeps_all_coordinates_in_the_denominator(self):
        case = run_case(self.source, "fixed_array", "superblock_checksum_bitflip", 44, self.root, timeout=90)
        self.assertEqual(case["observed"]["decision"], "refused")
        self.assertEqual(case["observed"]["total_elements"], 77)
        self.assertEqual(case["observed"]["unknown_elements"], 77)
        self.assertEqual(case["observed"]["exact_accepted_elements"], 0)
        self.assertFalse(case["observed"]["useful_output"])
        self.assertTrue(case["source_unchanged"])


if __name__ == "__main__":
    unittest.main()
