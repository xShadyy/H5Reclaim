"""Public rescue orchestration across distinct evidence routes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np


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

    def test_compound_type_uses_explicit_native_readable_route(self) -> None:
        source = self.base / "records.h5"
        dtype = np.dtype([("time", "<i4"), ("reading", "<f8")])
        rows = np.array([(1, 1.5), (2, -2.25)], dtype=dtype)
        with h5py.File(source, "w", libver="latest") as file:
            file.create_dataset("science", data=rows)
        result = self._run(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("native-readable copy", result.stdout)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["mode"], "readable_export")
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
