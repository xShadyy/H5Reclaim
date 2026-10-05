"""First-use commands provide safe destinations and actionable coverage summaries."""

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

from h5reclaim.__main__ import _render_report
from h5reclaim.recovery import VERSION


ROOT = Path(__file__).resolve().parents[1]


def command(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "h5reclaim", *args], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=60, check=False,
    )


class CliUsabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "measurements.h5"
        self.values = np.arange(16, dtype="<u4")
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("science", data=self.values, chunks=(8,), fletcher32=True)
            handle.create_dataset("calibration", data=np.array([1.5, 2.5], dtype="<f8"))

    def test_no_arguments_and_version_explain_the_installed_tool(self) -> None:
        help_result = command()
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("h5reclaim rescue damaged.h5", help_result.stdout)
        self.assertIn("h5reclaim report damaged.recovered.report.json", help_result.stdout)
        version = command("--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertEqual(version.stdout.strip(), f"h5reclaim {VERSION}")

    def test_whole_file_defaults_publish_new_sibling_files_and_summary(self) -> None:
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = command("rescue", str(self.source))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.base / "measurements.recovered.h5"
        report = self.base / "measurements.recovered.report.json"
        self.assertTrue(output.is_file())
        evidence = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(evidence["datasets_exported"], 2)
        with h5py.File(output) as handle:
            np.testing.assert_array_equal(handle["science"][:], self.values)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        summarized = command("report", str(report))
        self.assertEqual(summarized.returncode, 0, summarized.stderr)
        self.assertIn("Datasets: 2/2 exported | 0 failed", summarized.stdout)
        self.assertIn("[COMPLETE] /science", summarized.stdout)
        self.assertIn(evidence["metadata_group"] + "/datasets/d", summarized.stdout)
        self.assertIn("does not revalidate output files", summarized.stdout)

        # A second run cannot silently replace published output or evidence.
        output_bytes, report_bytes = output.read_bytes(), report.read_bytes()
        repeated = command("rescue", str(self.source))
        self.assertEqual(repeated.returncode, 2)
        self.assertIn("destination already exists", repeated.stderr)
        self.assertEqual(output.read_bytes(), output_bytes)
        self.assertEqual(report.read_bytes(), report_bytes)

    def test_selected_output_derives_report_beside_chosen_destination(self) -> None:
        destination = self.base / "selected.hdf5"
        result = command("rescue", str(self.source), "--dataset", "/science", "--output", str(destination))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(destination.is_file())
        report = self.base / "selected.report.json"
        self.assertTrue(report.is_file())
        self.assertIn(str(report), result.stdout)
        evidence = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(evidence["dataset"]["path"], "/science")
        self.assertIn("2 accepted chunks | 0 unknown chunks", result.stdout)

    def test_explicit_report_can_use_default_output(self) -> None:
        report = self.base / "custom-evidence.json"
        result = command("rescue", str(self.source), "--report", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.base / "measurements.recovered.h5").is_file())
        self.assertTrue(report.is_file())

    def test_partial_exit_is_opt_in_and_output_remains_available(self) -> None:
        with h5py.File(self.source, "r") as handle:
            address = int(handle["science"].id.get_chunk_info_by_coord((0,)).byte_offset)
        with self.source.open("r+b") as stream:
            stream.seek(address)
            first = stream.read(1)
            stream.seek(address)
            stream.write(bytes([first[0] ^ 0x20]))
        for name, flag, expected in (("compatible", [], 0), ("strict", ["--fail-on-partial"], 1)):
            with self.subTest(flag=flag):
                output = self.base / f"{name}.h5"
                report = self.base / f"{name}.report.json"
                result = command("rescue", str(self.source), "--dataset", "/science",
                                 "--output", str(output), *flag)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertTrue(output.is_file())
                self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["outcome"], "partial")
                self.assertIn("1 accepted chunks | 1 unknown chunks", result.stdout)
                self.assertIn("Status map: /_h5reclaim/chunk_status", result.stdout)
                summary = command("report", str(report))
                self.assertEqual(summary.returncode, 0, summary.stderr)
                self.assertIn("Partial export:", summary.stdout)

    def test_whole_file_partial_flag_counts_unknown_data(self) -> None:
        with h5py.File(self.source, "r") as handle:
            address = int(handle["science"].id.get_chunk_info_by_coord((0,)).byte_offset)
        with self.source.open("r+b") as stream:
            stream.seek(address)
            stream.write(b"\xff")
        result = command("rescue", str(self.source), "--fail-on-partial")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Partial export:", result.stdout)
        self.assertTrue((self.base / "measurements.recovered.h5").is_file())

    def test_existing_or_aliased_destinations_never_replace_source(self) -> None:
        before = self.source.read_bytes()
        for output, report in ((self.source, self.base / "evidence.json"),
                               (self.base / "shared", self.base / "shared")):
            with self.subTest(output=output, report=report):
                result = command("rescue", str(self.source), "--output", str(output), "--report", str(report))
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.source.read_bytes(), before)
                self.assertFalse((self.base / "evidence.json").exists())

    def test_invalid_report_is_a_concise_error(self) -> None:
        invalid = self.base / "invalid.json"
        for content in ("{invalid", "[]", '{"schema_version": 1, "outcome": "complete"}'):
            with self.subTest(content=content):
                invalid.write_text(content, encoding="utf-8")
                result = command("report", str(invalid))
                self.assertEqual(result.returncode, 2)
                self.assertIn("report summary failed", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_rescue_rejects_nonabsolute_or_malformed_dataset_paths_before_output(self) -> None:
        for selected in ("science", "", "/", "/science/", "/../science", "//science"):
            with self.subTest(selected=selected):
                result = command("rescue", str(self.source), "--dataset", selected)
                self.assertEqual(result.returncode, 2)
                self.assertIn("absolute HDF5 dataset path", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse((self.base / "measurements.recovered.h5").exists())
                self.assertFalse((self.base / "measurements.recovered.report.json").exists())

    def test_capsule_summary_includes_both_validity_maps_and_escapes_names(self) -> None:
        report = {
            "outcome": "partial", "source": {"path": "input.h5"}, "output_path": "output.h5",
            "dataset": {"path": "/science\n\x1b[31m"}, "accepted_elements": 6, "unknown_elements": 2,
            "validity": {"chunk_status": "/_h5reclaim/chunk_status",
                         "element_status": "/_h5reclaim/element_status"},
        }
        summary = _render_report(report)
        self.assertIn("6 accepted elements | 2 unknown elements", summary)
        self.assertIn("Status map: /_h5reclaim/chunk_status", summary)
        self.assertIn("Status map: /_h5reclaim/element_status", summary)
        self.assertNotIn("\x1b", summary)
        self.assertIn("\\n\\u001b", summary)

    def test_survey_labels_do_not_imply_all_recovery_routes_are_unsupported(self) -> None:
        result = command("survey", str(self.source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("structural parsers only", result.stdout)
        self.assertIn("datasets labeled unsupported here", result.stdout)
        diagnosis = command("diagnose", str(self.source), "--dataset", "/calibration")
        self.assertEqual(diagnosis.returncode, 0, diagnosis.stderr)
        self.assertIn("structural parser unsupported", diagnosis.stdout)
        self.assertIn("rescue also tries native-readable", diagnosis.stdout)


if __name__ == "__main__":
    unittest.main()
