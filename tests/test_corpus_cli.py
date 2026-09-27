"""The bundled corpus survey offers readable and machine-readable output."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from benchmarks.run_real_corpus import render_text


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "benchmarks" / "run_real_corpus.py"


class CorpusCliTests(unittest.TestCase):
    def test_default_summary_and_json_mode(self) -> None:
        plain = subprocess.run(
            [sys.executable, str(SCRIPT)], cwd=ROOT, capture_output=True, text=True
        )
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertIn("Original files verified: 4/4 (size and SHA-256)", plain.stdout)
        self.assertIn("Datasets surveyed: 251", plain.stdout)
        self.assertIn("Candidate means the metadata fits", plain.stdout)
        self.assertIn("this survey does not", plain.stdout)
        self.assertIn("GWOSC GW150914, Hanford H1 (16 kHz)", plain.stdout)
        self.assertNotIn('"kind":', plain.stdout)

        machine = subprocess.run(
            [sys.executable, str(SCRIPT), "--json"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(machine.returncode, 0, machine.stderr)
        result = json.loads(machine.stdout)
        self.assertEqual(result["kind"], "real_intact_data_coverage_survey")
        self.assertEqual(result["recovery_evaluation"], "not_performed_by_this_survey")
        self.assertEqual(result["dataset_support_counts"], {"candidate": 2, "unsupported": 249})
        self.assertTrue(result["baseline_matches_manifest"])

    def test_changed_baseline_is_visible_in_summary(self) -> None:
        sample = {
            "original_files_verified": 1,
            "dataset_support_counts": {"unsupported": 2},
            "baseline_matches_manifest": False,
            "baseline_changes": [{
                "id": "sample",
                "expected": {"candidate_count": 1},
                "observed": {"candidate_count": 0},
            }],
            "baseline_unpinned": [],
            "files": [{
                "id": "sample",
                "dataset_count": 2,
                "support_counts": {"unsupported": 2},
                "representative_dataset": {
                    "path": "/measurement",
                    "support": {"status": "unsupported", "reasons": [{"detail": "unsupported index"}]},
                },
            }],
        }
        output = render_text(sample)
        self.assertIn("Manifest coverage baseline: CHANGED", output)
        self.assertIn("sample candidate_count: expected 1, observed 0", output)
        self.assertIn("Reason: unsupported index", output)


if __name__ == "__main__":
    unittest.main()
