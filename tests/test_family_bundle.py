"""Family driver bundle export uses all pinned physical members."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.family_bundle import export_family
from h5reclaim.recovery import RecoveryError


class FamilyBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.template = str(self.folder / "run%03d.h5")
        with h5py.File(self.template, "w", driver="family", memb_size=1024) as handle:
            handle.create_dataset("science", data=np.arange(1000, dtype="<u4"), chunks=(100,))
        self.members = sorted(self.folder.glob("run[0-9][0-9][0-9].h5"))
        self.manifest = {"schema_version": 1, "member_size": 1024, "members": [
            {"index": i, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for i, path in enumerate(self.members)
        ]}
        self.output = self.folder / "out.h5"
        self.report = self.folder / "out.json"

    def test_family_crosses_members_and_preserves_current_values(self) -> None:
        result = export_family(self.manifest, "/science", self.output, self.report)
        self.assertEqual(result["outcome"], "complete")
        self.assertTrue(any(len(item["physical_parts"]) > 1 for item in result["accepted_ranges"]))
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["science"][:], np.arange(1000, dtype="<u4"))
            self.assertTrue(np.all(handle["/_h5reclaim/validity"][:] == 1))

    def test_missing_last_member_is_rejected(self) -> None:
        self.manifest["members"].pop()
        with self.assertRaises((RecoveryError, OSError, ValueError)):
            export_family(self.manifest, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())

    def test_wrong_hash_refuses_without_output(self) -> None:
        self.manifest["members"][1]["sha256"] = "0" * 64
        with self.assertRaisesRegex(RecoveryError, "pinned hash"):
            export_family(self.manifest, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())

    def test_output_cannot_alias_member(self) -> None:
        with self.assertRaisesRegex(RecoveryError, "new paths|destinations must differ"):
            export_family(self.manifest, "/science", self.members[0], self.report)
