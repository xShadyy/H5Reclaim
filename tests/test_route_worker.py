"""Subprocess route publication and failure cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_second_publication_link_failure_removes_staged_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output, report = (base / name for name in
                                      ("source.h5", "output.h5", "report.json"))
            with h5py.File(source, "w") as handle:
                handle.create_dataset("science", data=np.arange(8, dtype="<u4"))
            original_link = os.link
            calls = 0

            def fail_after_report(left: Path, right: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated second link failure")
                original_link(left, right)

            with patch("h5reclaim.route_worker.os.link", side_effect=fail_after_report):
                with self.assertRaisesRegex(OSError, "second link failure"):
                    run_route("readable", output, report, source=str(source), dataset="/science")
            self.assertEqual(calls, 2)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())
