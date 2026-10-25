"""Saved output measurements and artifact fingerprints detect later changes."""

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.recovery import recover
from h5reclaim.result_verification import ResultMismatch, verify_result
from h5reclaim.result_integrity import fingerprint_result, verify_fingerprint


class ResultValueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.output, self.report = (self.root / n for n in ("source.h5", "output.h5", "report.json"))
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("data", data=np.arange(63, dtype="i4").reshape(9, 7), chunks=(4, 4))
        recover(self.source, "/data", self.output, self.report)

    def test_value_check_detects_a_changed_measurement_with_unchanged_maps(self):
        result = verify_result(self.output, self.report, check_values=True)
        self.assertEqual(result["datasets"][0]["values"]["accepted_elements_checked"], 63)
        with h5py.File(self.output, "r+") as handle:
            handle["data"][2, 3] = -999
        self.assertEqual(verify_result(self.output, self.report)["status"], "passed")
        with self.assertRaises(ResultMismatch):
            verify_result(self.output, self.report, check_values=True)

    def test_fingerprint_checks_both_artifacts_and_refuses_overwrite(self):
        manifest = self.root / "fingerprint.json"
        fingerprint_result(self.output, self.report, manifest)
        self.assertEqual(verify_fingerprint(self.output, self.report, manifest)["status"], "passed")
        with self.assertRaises(FileExistsError):
            fingerprint_result(self.output, self.report, manifest)
        self.report.write_text(self.report.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "report bytes differ"):
            verify_fingerprint(self.output, self.report, manifest)

    def test_non_object_fingerprint_is_a_normal_input_error(self):
        manifest = self.root / "fingerprint.json"
        manifest.write_text("[]")
        with self.assertRaises(ValueError):
            verify_fingerprint(self.output, self.report, manifest)


if __name__ == "__main__":
    unittest.main()
