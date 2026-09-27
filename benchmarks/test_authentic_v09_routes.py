"""Evaluator self-check on the pinned scientific trial; run as a unittest module."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h5py

from .run_authentic_v09_routes import DATASET, TrialError, _score, _source, run


class AuthenticV09EvaluatorTests(unittest.TestCase):
    def test_controlled_routes_and_postpublication_false_accept(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h5reclaim-v09-evaluator-") as temporary:
            folder = Path(temporary) / "trials"
            result = run(folder)
            self.assertEqual(result["controlled_cases"], 5)
            self.assertTrue(result["all_cases_passed"])
            self.assertTrue(result["tampered_capsule_pin_refused"])
            self.assertEqual([item["evaluation"]["unknown_chunks"] for item in result["cases"]],
                             [0, 0, 0, 0, 1])
            reference, _entry = _source()
            source = folder / "root_link_lost" / "damaged.h5"
            output = folder / "root_link_lost" / "recovered.h5"
            report = folder / "root_link_lost" / "report.json"
            with h5py.File(output, "r+") as file:
                file[DATASET][0] = 12345.0
            with self.assertRaisesRegex(TrialError, "accepted chunks differ"):
                _score(reference, source, result["cases"][0]["damaged_sha256"], output, report,
                       route="prospective_recovery_capsule",
                       capsule_sha=result["prospective_pins"]["capsule_sha256"])


if __name__ == "__main__":
    unittest.main()
