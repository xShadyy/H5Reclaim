"""Public CLI keeps operator claims separate from observed recovery evidence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def command(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "h5reclaim", *args], cwd=ROOT,
        env=environment, capture_output=True, text=True, timeout=30, check=False,
    )


class TriageCliTests(unittest.TestCase):
    def test_human_output_escapes_control_characters_in_dataset_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "strange.h5"
            selected = "/readings\n\x1b[31m"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset(selected, data=np.arange(144, dtype="<u4").reshape(12, 12),
                                      chunks=(1, 1))
            readable = command("diagnose", str(source), "--dataset", selected)
            self.assertEqual(readable.returncode, 0, readable.stderr)
            self.assertNotIn("\x1b", readable.stdout)
            self.assertIn("\\n\\u001b", readable.stdout)
            exact = command("diagnose", str(source), "--dataset", selected, "--json")
            self.assertEqual(json.loads(exact.stdout)["selection"]["selected_path"], selected)

    def test_newer_chunk_index_offers_structural_inspection_and_native_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "modern.h5"
            values = np.arange(77, dtype="<f8").reshape(7, 11)
            with h5py.File(source, "x", libver="latest") as handle:
                handle.create_dataset("readings", data=values, chunks=(3, 4), compression="gzip")
            before = source.read_bytes()
            triage = command("diagnose", str(source), "--dataset", "/readings", "--json")
            self.assertEqual(triage.returncode, 0, triage.stderr)
            self.assertEqual(json.loads(triage.stdout)["next_action"], "inspect_anchored_index")

            output, report = root / "copy.h5", root / "copy.json"
            copied = command(
                "export-readable", str(source), "--dataset", "/readings",
                "--output", str(output), "--report", str(report),
            )
            self.assertEqual(copied.returncode, 0, copied.stderr)
            self.assertIn("No damaged index was reconstructed", copied.stdout)
            evidence = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(evidence["mode"], "readable_export")
            self.assertIn("not structurally recovered", evidence["limits"])
            with h5py.File(output, "r") as handle:
                np.testing.assert_array_equal(handle["/readings"][...], values)
            self.assertEqual(source.read_bytes(), before)

    def test_diagnosis_with_matching_and_conflicting_operator_hints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "file.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset(
                    "measurements", data=np.arange(144, dtype="<u4").reshape(12, 12),
                    chunks=(1, 1),
                )
            before = source.read_bytes()
            hints_path = root / "hints.json"
            hints = {
                "schema_version": 1,
                "dataset": {"path": "/measurements", "shape": [12, 12],
                            "chunks": [1, 1], "filters": []},
                "source_sha256": hashlib.sha256(before).hexdigest(),
            }
            hints_path.write_text(json.dumps(hints), encoding="utf-8")
            result = command("diagnose", str(source), "--hints", str(hints_path), "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertFalse(report["recovery_attempted"])
            self.assertIsNone(report["recovered_values"])
            self.assertEqual(report["next_action"], "inspect_anchored_index")
            self.assertEqual({item["status"] for item in report["operator_hints"]["comparisons"]}, {"matches"})

            hints["dataset"]["shape"] = [12, 13]
            hints_path.write_text(json.dumps(hints), encoding="utf-8")
            conflict = command("diagnose", str(source), "--hints", str(hints_path), "--json")
            self.assertEqual(conflict.returncode, 1, conflict.stderr)
            report = json.loads(conflict.stdout)
            self.assertEqual(report["next_action"], "resolve_hint_conflicts")
            self.assertIn("conflicts", {item["status"] for item in report["operator_hints"]["comparisons"]})
            self.assertEqual(source.read_bytes(), before)

    def test_recovery_rejects_conflicting_hints_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "file.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset(
                    "measurements", data=np.arange(144, dtype="<u4").reshape(12, 12),
                    chunks=(1, 1),
                )
            before = source.read_bytes()
            hints_path = root / "hints.json"
            hints_path.write_text(json.dumps({
                "schema_version": 1,
                "dataset": {"path": "/measurements", "shape": [12, 13]},
            }), encoding="utf-8")
            output, report = root / "output.h5", root / "report.json"
            result = command(
                "recover", str(source), "--hints", str(hints_path),
                "--output", str(output), "--report", str(report),
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("operator hints conflict", result.stderr)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())
            self.assertEqual(source.read_bytes(), before)

            hints_path.write_text(json.dumps({
                "schema_version": 1,
                "dataset": {"path": "/measurements", "shape": [12, 12],
                            "dtype": "<u4", "filters": []},
            }), encoding="utf-8")
            accepted = command(
                "recover", str(source), "--hints", str(hints_path),
                "--output", str(output), "--report", str(report),
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            evidence = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(evidence["operator_hints"]["trust_level"], "unverified_operator_assertion")
            self.assertTrue(all(item["status"] == "matches" for item in evidence["operator_hints"]["comparisons"]))
            with h5py.File(output, "r") as handle:
                np.testing.assert_array_equal(handle["/measurements"][...], np.arange(144).reshape(12, 12))
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
