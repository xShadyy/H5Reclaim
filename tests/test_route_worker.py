"""Subprocess route publication and failure cleanup."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.recovery import RecoveryError
from h5reclaim.route_worker import run_route


class RouteWorkerTests(unittest.TestCase):
    def test_family_worker_finishes_and_publishes_atomic_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with h5py.File(str(base / "part%03d.h5"), "w", driver="family", memb_size=1024) as handle:
                handle.create_dataset("measurements", data=np.arange(100, dtype="<u4"), chunks=(20,))
            files = sorted(base.glob("part[0-9][0-9][0-9].h5"))
            manifest = base / "family.json"
            manifest.write_text(json.dumps({"schema_version": 1, "member_size": 1024, "members": [
                {"index": i, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for i, path in enumerate(files)
            ]}), encoding="utf-8")
            output, report = base / "result.h5", base / "result.json"
            found = run_route("family", output, report, manifest=str(manifest), dataset="/measurements")
            self.assertEqual(found["outcome"], "complete")
            with h5py.File(output, "r") as handle:
                np.testing.assert_array_equal(handle["measurements"][:], np.arange(100, dtype="<u4"))
            self.assertEqual(json.loads(report.read_text()), found)

    def test_failure_keeps_destinations_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with self.assertRaises(RecoveryError):
                run_route("family", base / "result.h5", base / "result.json",
                          manifest=str(base / "absent.json"), dataset="/science")
            self.assertFalse((base / "result.h5").exists())
            self.assertFalse((base / "result.json").exists())
