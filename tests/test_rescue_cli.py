"""Public rescue orchestration across distinct evidence routes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import shutil
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.baseline import capture_baseline
from h5reclaim.parity_sidecar import capture_parity_sidecar


class RescueCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.out = self.base / "recovered.h5"
        self.report = self.base / "evidence.json"

    def _run(self, source: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "h5reclaim", "rescue", str(source), "--dataset", "/science",
             "--output", str(self.out), "--report", str(self.report), *extra],
            capture_output=True, text=True, timeout=30,
        )

    def test_nonchunked_uses_element_validity_and_preserves_source(self) -> None:
        source = self.base / "input.h5"
        with h5py.File(source, "w", libver="latest") as file:
            file.create_dataset("science", data=np.arange(17, dtype="<u4"))
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        result = self._run(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("17 accepted elements", result.stdout)
        with h5py.File(self.out) as file:
            np.testing.assert_array_equal(file["science"][:], np.arange(17, dtype="<u4"))
            self.assertTrue(np.all(file["/_h5reclaim/element_status"][:] == 1))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_external_raw_uses_pinned_bytes_and_rejects_bad_pin(self) -> None:
        raw, source = self.base / "readings.raw", self.base / "input.h5"
        with h5py.File(source, "w") as file:
            data = file.create_dataset("science", shape=(10,), dtype="<u4", external=[(str(raw), 0, 40)])
            data[:] = np.arange(10, dtype="<u4")
        manifest = self.base / "related.json"
        document = {"schema_version": 1, "files": [{"declared_name": str(raw), "path": str(raw),
                    "sha256": "0" * 64}]}
        manifest.write_text(json.dumps(document), encoding="utf-8")
        refused = self._run(source, "--related-files", str(manifest))
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse(self.out.exists())
        document["files"][0]["sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(document), encoding="utf-8")
        accepted = self._run(source, "--related-files", str(manifest))
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        with h5py.File(self.out) as file:
            np.testing.assert_array_equal(file["science"][:], np.arange(10, dtype="<u4"))
            self.assertTrue(np.all(file["/_h5reclaim/validity"][:] == 1))

    def test_family_source_must_match_member_zero(self) -> None:
        template = str(self.base / "part%03d.h5")
        with h5py.File(template, "w", driver="family", memb_size=1024) as file:
            file.create_dataset("science", data=np.arange(1000, dtype="<u4"), chunks=(100,))
        members = sorted(self.base.glob("part[0-9][0-9][0-9].h5"))
        manifest = self.base / "family.json"
        manifest.write_text(json.dumps({"schema_version": 1, "member_size": 1024, "members": [
            {"index": index, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for index, path in enumerate(members)
        ]}), encoding="utf-8")
        refused = self._run(members[1], "--family-members", str(manifest))
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse(self.out.exists())
        accepted = self._run(members[0], "--family-members", str(manifest))
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        with h5py.File(self.out) as file:
            np.testing.assert_array_equal(file["science"][:], np.arange(1000, dtype="<u4"))

    def test_compound_type_uses_rooted_fixed_record_route(self) -> None:
        source = self.base / "records.h5"
        dtype = np.dtype([("time", "<i4"), ("reading", "<f8")])
        rows = np.array([(1, 1.5), (2, -2.25)], dtype=dtype)
        with h5py.File(source, "w", libver="latest") as file:
            file.create_dataset("science", data=rows)
        result = self._run(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["operation"], "structural_nonchunked_export")
        self.assertEqual(report["accepted_elements"], len(rows))
        self.assertIsNotNone(report["dataset"]["exact_file_datatype_sha256"])
        with h5py.File(self.out) as file:
            self.assertEqual(file["science"][:].tobytes(), rows.tobytes())

    def test_split_driver_checks_metadata_member_and_exports_raw_member(self) -> None:
        stem = self.base / "run"
        with h5py.File(stem, "w", driver="split", meta_ext=b"-m.h5", raw_ext=b"-r.h5") as file:
            file.create_dataset("science", data=np.arange(14, dtype="<u4"), chunks=(7,))
        metadata, raw = self.base / "run-m.h5", self.base / "run-r.h5"
        manifest = self.base / "split.json"
        manifest.write_text(json.dumps({"schema_version": 1, "driver": "split", "members": [
            {"role": role, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for role, path in (("metadata", metadata), ("raw", raw))
        ]}), encoding="utf-8")
        self.assertNotEqual(self._run(raw, "--split-members", str(manifest)).returncode, 0)
        self.assertFalse(self.out.exists())
        result = self._run(metadata, "--split-members", str(manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        with h5py.File(self.out) as file:
            np.testing.assert_array_equal(file["science"][:], np.arange(14, dtype="<u4"))

    def test_virtual_source_is_materialized_from_pinned_file_not_fill(self) -> None:
        related = self.base / "source.h5"
        virtual = self.base / "virtual.h5"
        with h5py.File(related, "w") as file:
            file.create_dataset("science", data=np.arange(10, dtype="<u4"))
        layout = h5py.VirtualLayout(shape=(10,), dtype="<u4")
        layout[:] = h5py.VirtualSource("source.h5", "science", shape=(10,))
        with h5py.File(virtual, "w", libver="latest") as file:
            file.create_virtual_dataset("science", layout, fillvalue=31337)
        manifest = self.base / "virtual_sources.json"
        manifest.write_text(json.dumps({"schema_version": 1, "files": [{
            "declared_name": "source.h5", "path": str(related),
            "sha256": hashlib.sha256(related.read_bytes()).hexdigest(),
        }]}), encoding="utf-8")
        result = self._run(virtual, "--related-files", str(manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.report.read_text())["output_path"], str(self.out))
        with h5py.File(self.out) as file:
            np.testing.assert_array_equal(file["science"][:], np.arange(10, dtype="<u4"))
            self.assertTrue(np.all(file["/_h5reclaim/validity"][:] == 1))

    def test_replica_route_replaces_changed_chunk_against_prior_capture(self) -> None:
        pristine = self.base / "capture.h5"
        damaged = self.base / "damaged.h5"
        replica = self.base / "replica.h5"
        baseline = self.base / "baseline.json"
        with h5py.File(pristine, "w", libver="latest") as file:
            file.create_dataset("science", data=np.arange(16, dtype="<u4"), chunks=(8,))
        capture_baseline(pristine, "/science", baseline)
        shutil.copyfile(pristine, damaged)
        shutil.copyfile(pristine, replica)
        with h5py.File(damaged, "r") as file:
            address = int(file["science"].id.get_chunk_info_by_coord((0,)).byte_offset)
        with damaged.open("r+b") as handle:
            handle.seek(address)
            first = handle.read(1)
            handle.seek(address)
            handle.write(bytes([first[0] ^ 0x20]))
        manifest = self.base / "replicas.json"
        manifest.write_text(json.dumps({
            "schema_version": 1, "damaged_sha256": hashlib.sha256(damaged.read_bytes()).hexdigest(),
            "baseline": {"path": str(baseline), "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest()},
            "replicas": [{"path": str(replica), "sha256": hashlib.sha256(replica.read_bytes()).hexdigest()}],
        }), encoding="utf-8")
        result = self._run(damaged, "--replicas", str(manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["replacements_from_replicas"], 1)
        with h5py.File(self.out) as file:
            np.testing.assert_array_equal(file["science"][:], np.arange(16, dtype="<u4"))

    def test_parity_route_reconstructs_one_changed_chunk_from_prior_capture(self) -> None:
        pristine, damaged = self.base / "capture.h5", self.base / "damaged.h5"
        baseline, parity = self.base / "baseline.json", self.base / "parity.zip"
        with h5py.File(pristine, "w", libver="latest") as file:
            file.create_dataset("science", data=np.arange(32, dtype="<u4"), chunks=(8,))
        capture_baseline(pristine, "/science", baseline)
        capture_parity_sidecar(pristine, "/science", baseline, parity, stripe_width=4)
        shutil.copyfile(pristine, damaged)
        with h5py.File(damaged, "r") as file:
            address = int(file["science"].id.get_chunk_info_by_coord((8,)).byte_offset)
        with damaged.open("r+b") as handle:
            handle.seek(address)
            first = handle.read(1)
            handle.seek(address)
            handle.write(bytes([first[0] ^ 0x40]))
        manifest = self.base / "parity_manifest.json"
        manifest.write_text(json.dumps({
            "schema_version": 1, "damaged_sha256": hashlib.sha256(damaged.read_bytes()).hexdigest(),
            "baseline": {"path": str(baseline), "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest()},
            "parity": {"path": str(parity), "sha256": hashlib.sha256(parity.read_bytes()).hexdigest()},
        }), encoding="utf-8")
        result = self._run(damaged, "--parity", str(manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.report.read_text())["reconstructed_from_parity"], 1)
        with h5py.File(self.out) as file:
            np.testing.assert_array_equal(file["science"][:], np.arange(32, dtype="<u4"))
