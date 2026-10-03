"""Diagnosis routes external dependencies and status flags without value reads."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.__main__ import main
from h5reclaim.diagnose import diagnose
from tools.make_status_fixture import copy_with_write_flag


class DependencyIntegrationTests(unittest.TestCase):
    def test_external_raw_and_vds_route_to_explicit_dependency_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "linked.h5"
            layout = h5py.VirtualLayout(shape=(4,), dtype="<i4")
            layout[:] = h5py.VirtualSource("missing-source.h5", "/readings", shape=(4,))
            with h5py.File(source, "x", libver="latest") as handle:
                handle.create_virtual_dataset("virtual", layout, fillvalue=-10)
                handle.create_dataset(
                    "external", shape=(4,), dtype="<i4",
                    external=[("missing-raw.bin", 0, 16)],
                )
            before = source.read_bytes()
            with patch.object(h5py.Dataset, "__getitem__", side_effect=AssertionError("value read")):
                virtual = diagnose(source, "/virtual")
                external = diagnose(source, "/external")
            for report, expected in ((virtual, "missing-source.h5"), (external, "missing-raw.bin")):
                self.assertEqual(report["next_action"], "resolve_external_or_virtual_dependencies")
                self.assertEqual(report["dependencies"]["dependencies"][0]["file_name"], expected)
                self.assertFalse(report["dependencies"]["referenced_files_opened"])
                self.assertFalse(report["dependencies"]["values_read"])
                self.assertFalse(report["recovery_attempted"])
            self.assertEqual(source.read_bytes(), before)

    def test_external_link_is_reported_without_opening_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "parent.h5"
            with h5py.File(source, "x") as handle:
                handle["absent"] = h5py.ExternalLink("not-here.h5", "/measurements")
            report = diagnose(source, "/absent")
            self.assertEqual(report["next_action"], "resolve_external_or_virtual_dependencies")
            self.assertEqual(report["dependencies"]["outcome"], "external_link")
            self.assertEqual(report["dependencies"]["dependencies"][0]["object_path"], "/measurements")
            self.assertFalse(report["dependencies"]["referenced_files_opened"])

    def test_write_flag_route_does_not_claim_stale_or_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            written = folder / "writer.h5"
            flagged = folder / "interrupted.h5"
            with h5py.File(written, "x", libver="latest") as handle:
                handle.create_dataset("readings", data=np.arange(4))
            copy_with_write_flag(written, flagged)
            before = flagged.read_bytes()
            report = diagnose(flagged, "/readings")
            self.assertEqual(report["condition"], "metadata_unreadable")
            self.assertEqual(report["next_action"], "consider_status_copy_probe")
            self.assertEqual(report["file_status"]["status_flags"]["interpretation"], "write_flag_present")
            self.assertFalse(report["file_status"]["checksum_validated"])
            self.assertFalse(report["recovery_attempted"])
            self.assertEqual(flagged.read_bytes(), before)

    def test_probe_status_cli_returns_unavailable_without_h5clear_and_no_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            written = folder / "writer.h5"
            flagged = folder / "interrupted.h5"
            with h5py.File(written, "x", libver="latest") as handle:
                handle.create_dataset("readings", data=np.arange(4))
            copy_with_write_flag(written, flagged)
            before = flagged.read_bytes()
            stdout = io.StringIO()
            with patch("h5reclaim.dependency_routes.shutil.which", return_value=None), \
                    contextlib.redirect_stdout(stdout):
                exit_code = main(["probe-status", str(flagged), "--json"])
            result = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertEqual(result["outcome"], "unavailable")
            self.assertTrue(result["trial_was_disposable"])
            self.assertFalse(result["source_modified"])
            self.assertEqual(flagged.read_bytes(), before)

    def test_diagnose_cli_accepts_explicit_related_file_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "parent.h5"
            raw = folder / "different-local-name.bin"
            raw.write_bytes(b"1234567890abcdef")
            with h5py.File(source, "x") as handle:
                handle.create_dataset(
                    "external", shape=(4,), dtype="<i4",
                    external=[("declared-data.bin", 0, 16)],
                )
            source_before = source.read_bytes()
            manifest = folder / "related.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "files": [{
                    "declared_name": "declared-data.bin", "path": str(raw),
                    "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
                }],
            }), encoding="utf-8")
            stdout = io.StringIO()
            with patch.object(h5py.Dataset, "__getitem__", side_effect=AssertionError("value read")), \
                    contextlib.redirect_stdout(stdout):
                exit_code = main([
                    "diagnose", str(source), "--dataset", "/external",
                    "--related-files", str(manifest), "--json",
                ])
            report = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["next_action"], "resolve_external_or_virtual_dependencies")
            self.assertTrue(report["dependency_validation"]["all_declared_present_and_hash_matched"])
            self.assertFalse(report["dependency_validation"]["values_read"])
            self.assertFalse(report["recovery_attempted"])
            self.assertEqual(source.read_bytes(), source_before)


if __name__ == "__main__":
    unittest.main()
