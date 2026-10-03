"""Status/dependency triage must not turn path claims into measurements."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.dependency_routes import (
    DependencyError,
    MAX_MANIFEST_BYTES,
    discover_h5clear,
    inspect_dependencies,
    inspect_superblock_status,
    load_dependency_manifest,
    probe_status_copy,
    validate_dependency_manifest,
)
from tools.make_status_fixture import copy_with_write_flag


class StatusAndDependencyTests(unittest.TestCase):
    def test_modern_superblock_flags_and_eoa_are_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.h5"
            with h5py.File(path, "x", libver="latest") as handle:
                handle.create_dataset("readings", data=np.arange(4))
            before = path.read_bytes()
            report = inspect_superblock_status(path, 0, path.stat().st_size)
            self.assertEqual(report["superblock_version"], 3)
            self.assertEqual(report["status_flags"]["interpretation"], "no_write_flag_observed")
            self.assertEqual(report["status_flags"]["raw"], 0)
            self.assertEqual(report["end_of_address"]["relation"], "equal")
            self.assertFalse(report["checksum_validated"])
            self.assertEqual(path.read_bytes(), before)

    def test_old_consistency_bytes_do_not_mean_stale_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.h5"
            with h5py.File(path, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset("readings", data=[1, 2, 3])
            data = bytearray(path.read_bytes())
            self.assertIn(data[8], (0, 1))
            data[20:24] = b"\x01\x00\x00\x00"
            path.write_bytes(data)
            report = inspect_superblock_status(path, 0, path.stat().st_size)
            self.assertEqual(report["status_flags"]["raw"], 1)
            self.assertEqual(report["status_flags"]["interpretation"], "unused_in_this_version")
            self.assertIsNone(report["status_flags"]["write_access_bit"])

    def test_short_or_unknown_superblock_never_claims_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short.h5"
            path.write_bytes(b"\x89HDF\r\n\x1a\n\x03\x08\x08")
            report = inspect_superblock_status(path, 0, path.stat().st_size)
            self.assertEqual(report["outcome"], "unavailable")
            path.write_bytes(b"\x89HDF\r\n\x1a\n\xff\0\0\0")
            report = inspect_superblock_status(path, 0, path.stat().st_size)
            self.assertEqual(report["status_flags"]["interpretation"], "unknown_version")

    def test_virtual_and_external_raw_sources_are_only_declared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dependent.h5"
            layout = h5py.VirtualLayout(shape=(6,), dtype="<i4")
            layout[:] = h5py.VirtualSource("not-present-vds.h5", "/observations", shape=(6,))
            with h5py.File(path, "x", libver="latest") as handle:
                handle.create_virtual_dataset("virtual", layout, fillvalue=-1)
                handle.create_dataset(
                    "external", shape=(6,), dtype="<i4",
                    external=[("not-present-raw.bin", 0, 24)],
                )
            before = path.read_bytes()
            with patch.object(h5py.Dataset, "__getitem__", side_effect=AssertionError("payload read")):
                vds = inspect_dependencies(path, "/virtual")
                raw = inspect_dependencies(path, "/external")
            self.assertEqual(vds["outcome"], "complete")
            self.assertEqual(vds["dependencies"][0]["kind"], "virtual_source")
            self.assertEqual(vds["dependencies"][0]["file_name"], "not-present-vds.h5")
            self.assertEqual(vds["dependencies"][0]["object_path"], "/observations")
            self.assertEqual(raw["dependencies"][0]["kind"], "external_raw_storage")
            self.assertEqual(raw["dependencies"][0]["file_name"], "not-present-raw.bin")
            self.assertFalse(raw["referenced_files_opened"])
            self.assertFalse(raw["values_read"])
            self.assertFalse((Path(directory) / "not-present-raw.bin").exists())
            self.assertEqual(path.read_bytes(), before)

    def test_external_link_and_intermediate_soft_link_are_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.h5"
            with h5py.File(path, "x") as handle:
                handle["outside"] = h5py.ExternalLink("missing.h5", "/results")
                handle["alias"] = h5py.SoftLink("/outside")
            external = inspect_dependencies(path, "/outside")
            self.assertEqual(external["outcome"], "external_link")
            self.assertEqual(external["dependencies"][0]["file_name"], "missing.h5")
            self.assertEqual(inspect_dependencies(path, "/alias")["outcome"], "soft_link")
            self.assertEqual(inspect_dependencies(path, "/unknown")["outcome"], "missing_or_unknown_link")

    def test_h5clear_discovery_does_not_execute(self) -> None:
        with patch("h5reclaim.dependency_routes.shutil.which", return_value=None), patch(
            "h5reclaim.dependency_routes.subprocess.run", side_effect=AssertionError("executed")
        ):
            self.assertFalse(discover_h5clear()["available"])

    def test_status_trial_rejects_eoa_past_end_without_executing_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "truncated.h5"
            with h5py.File(path, "x", libver="latest") as handle:
                handle.create_dataset("readings", data=np.arange(4))
            data = bytearray(path.read_bytes())
            self.assertEqual(data[8], 3)
            data[11] = 1
            data[12 + 2 * data[9]:12 + 3 * data[9]] = (len(data) + 500).to_bytes(data[9], "little")
            path.write_bytes(data)
            with patch("h5reclaim.dependency_routes.subprocess.run", side_effect=AssertionError("executed")):
                report = probe_status_copy(path, executable="h5clear")
            self.assertEqual(report["outcome"], "not_applicable")
            self.assertEqual(report["observation"]["end_of_address"]["relation"], "past_physical_eof")
            self.assertEqual(path.read_bytes(), bytes(data))

    def test_status_trial_invokes_h5clear_only_on_disposable_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            writer_file = folder / "open-writer.h5"
            damaged = folder / "flagged-copy.h5"
            with h5py.File(writer_file, "x", libver="latest") as handle:
                handle.create_dataset("readings", data=np.arange(4))
            copy_with_write_flag(writer_file, damaged)
            before = damaged.read_bytes()
            self.assertEqual(before[8], 3)
            self.assertEqual(before[11] & 1, 1)
            calls = []

            def fake_h5clear(command, **kwargs):
                calls.append(command)
                self.assertEqual(command[:2], ["h5clear", "--status"])
                self.assertNotEqual(Path(command[2]), damaged)
                self.assertNotEqual(Path(command[2]), writer_file)
                trial = Path(command[2])
                with trial.open("ab") as handle:
                    handle.write(b"unexpected")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("h5reclaim.dependency_routes.subprocess.run", side_effect=fake_h5clear):
                report = probe_status_copy(damaged, executable="h5clear")
            self.assertEqual(len(calls), 1)
            self.assertEqual(report["outcome"], "unexpected_tool_change")
            self.assertFalse(report["recovery_attempted"])
            self.assertIsNone(report["recovered_values"])
            self.assertEqual(damaged.read_bytes(), before)
            self.assertFalse(Path(calls[0][2]).exists())

    def test_status_only_change_can_make_copy_metadata_openable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            healthy = folder / "closed.h5"
            flagged = folder / "flagged.h5"
            with h5py.File(healthy, "x", libver="latest") as handle:
                handle.create_dataset("readings", data=np.arange(4))
            copy_with_write_flag(healthy, flagged)
            original = flagged.read_bytes()
            closed = healthy.read_bytes()
            self.assertEqual(len(original), len(closed))
            self.assertEqual(original[11] & 1, 1)

            def fake_h5clear(command, **kwargs):
                trial = Path(command[2])
                self.assertNotEqual(trial, flagged)
                data = bytearray(trial.read_bytes())
                data[11] = closed[11]
                offsize = data[9]
                checksum = 12 + 4 * offsize
                data[checksum:checksum + 4] = closed[checksum:checksum + 4]
                trial.write_bytes(data)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("h5reclaim.dependency_routes.subprocess.run", side_effect=fake_h5clear):
                result = probe_status_copy(flagged, executable="h5clear")
            self.assertEqual(result["outcome"], "copy_metadata_opened")
            self.assertFalse(result["recovery_attempted"])
            self.assertFalse(result["source_modified"])
            self.assertEqual(flagged.read_bytes(), original)

    def test_explicit_bundle_matches_names_and_pinned_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            main = folder / "main.h5"
            related = folder / "actual-source.bin"
            related.write_bytes(b"six instrument readings!")
            with h5py.File(main, "x", libver="latest") as handle:
                handle.create_dataset(
                    "external", shape=(6,), dtype="<i4",
                    external=[("declared.bin", 0, 24)],
                )
            inventory = inspect_dependencies(main, "/external")
            manifest_path = folder / "bundle.json"
            manifest_path.write_text(json.dumps({
                "schema_version": 1,
                "files": [{
                    "declared_name": "declared.bin",
                    "path": str(related),
                    "sha256": hashlib.sha256(related.read_bytes()).hexdigest(),
                }],
            }), encoding="utf-8")
            manifest = load_dependency_manifest(manifest_path)
            report = validate_dependency_manifest(inventory, manifest)
            self.assertTrue(report["all_declared_present_and_hash_matched"])
            self.assertFalse(report["values_read"])
            self.assertFalse(report["historical_values_verified"])
            self.assertEqual(report["references"][0]["size_bytes"], 24)
            related.write_bytes(b"short")
            manifest["files"][0]["sha256"] = hashlib.sha256(related.read_bytes()).hexdigest()
            report = validate_dependency_manifest(inventory, manifest)
            self.assertEqual(report["references"][0]["status"], "declared_raw_range_missing")
            self.assertFalse(report["all_declared_present_and_hash_matched"])
            related.write_bytes(b"replaced bytes")
            report = validate_dependency_manifest(inventory, manifest)
            self.assertEqual(report["references"][0]["status"], "hash_mismatch")
            self.assertFalse(report["all_declared_present_and_hash_matched"])

    def test_manifest_never_resolves_declared_name_as_ambient_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "ambient.h5").write_bytes(b"unrelated")
            inventory = {
                "outcome": "complete",
                "dependencies": [{"kind": "virtual_source", "file_name": "ambient.h5", "declared_name_exact": True}],
            }
            other = folder / "other.h5"
            other.write_bytes(b"separate")
            manifest = {
                "schema_version": 1,
                "files": [{
                    "declared_name": "other.h5", "path": str(other),
                    "sha256": hashlib.sha256(other.read_bytes()).hexdigest(),
                }],
            }
            report = validate_dependency_manifest(inventory, manifest)
            self.assertEqual(report["references"][0]["status"], "not_supplied")
            self.assertEqual(report["unused_manifest_names"], ["other.h5"])
            self.assertFalse(report["all_declared_present_and_hash_matched"])

    def test_dynamic_virtual_pattern_stays_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            related = Path(directory) / "source-1.h5"
            related.write_bytes(b"data")
            inventory = {
                "outcome": "complete",
                "dependencies": [{"kind": "virtual_source", "file_name": "source-%b.h5", "declared_name_exact": True}],
            }
            manifest = {
                "schema_version": 1,
                "files": [{"declared_name": "source-%b.h5", "path": str(related),
                           "sha256": hashlib.sha256(related.read_bytes()).hexdigest()}],
            }
            report = validate_dependency_manifest(inventory, manifest)
            self.assertEqual(report["references"][0]["status"], "dynamic_filename_pattern_unresolved")
            self.assertFalse(report["all_declared_present_and_hash_matched"])

    def test_virtual_bundle_requires_declared_local_source_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            related = folder / "source.h5"
            with h5py.File(related, "x") as handle:
                handle.create_dataset("another_name", data=np.arange(4))
            inventory = {
                "outcome": "complete",
                "dependencies": [{
                    "kind": "virtual_source", "file_name": "experiment.h5",
                    "object_path": "/readings", "declared_name_exact": True,
                }],
            }
            manifest = {
                "schema_version": 1,
                "files": [{
                    "declared_name": "experiment.h5", "path": str(related),
                    "sha256": hashlib.sha256(related.read_bytes()).hexdigest(),
                }],
            }
            report = validate_dependency_manifest(inventory, manifest)
            self.assertEqual(report["references"][0]["status"], "source_link_not_local_hard_link")
            self.assertFalse(report["all_declared_present_and_hash_matched"])
            with h5py.File(related, "r+") as handle:
                handle["readings"] = handle["another_name"]
            manifest["files"][0]["sha256"] = hashlib.sha256(related.read_bytes()).hexdigest()
            report = validate_dependency_manifest(inventory, manifest)
            self.assertEqual(report["references"][0]["status"], "hash_matched")
            self.assertEqual(report["references"][0]["target_metadata"], "local_object_metadata_present")
            self.assertTrue(report["all_declared_present_and_hash_matched"])

    def test_manifest_rejects_relative_paths_duplicates_and_oversize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / "manifest.json"
            digest = "0" * 64
            entry = {"declared_name": "a.h5", "path": "relative.h5", "sha256": digest}
            path.write_text(json.dumps({"schema_version": 1, "files": [entry]}))
            with self.assertRaisesRegex(DependencyError, "absolute"):
                load_dependency_manifest(path)
            entry["path"] = str(folder / "a.h5")
            path.write_text(json.dumps({"schema_version": 1, "files": [entry, entry]}))
            with self.assertRaisesRegex(DependencyError, "duplicate"):
                load_dependency_manifest(path)
            with path.open('wb') as stream:
                stream.truncate(MAX_MANIFEST_BYTES + 1)
            with self.assertRaisesRegex(DependencyError, "64 MiB"):
                load_dependency_manifest(path)


if __name__ == "__main__":
    unittest.main()
