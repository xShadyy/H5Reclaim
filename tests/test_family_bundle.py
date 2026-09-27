"""Family driver bundle export uses all pinned physical members."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.family_bundle import export_family
from h5reclaim.metadata import UnsupportedCase
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

    def test_distinct_hardlink_names_cannot_claim_two_family_indices(self) -> None:
        # Both manifest paths can be SHA-256-pinned yet identify the same
        # physical file. Without this refusal a native read reports the
        # wrong latter-member bytes as complete measurements.
        self.members[4].unlink()
        os.link(self.members[3], self.members[4])
        self.manifest["members"][4]["sha256"] = hashlib.sha256(self.members[4].read_bytes()).hexdigest()
        with self.assertRaisesRegex(RecoveryError, "one physical file"):
            export_family(self.manifest, "/science", self.output, self.report)
        self.assertFalse(self.output.exists() or self.report.exists())

    def test_stored_chunk_size_is_bounded_before_native_read(self) -> None:
        from h5reclaim import family_bundle
        with patch.object(family_bundle, "MAX_STORED_CHUNK_BYTES", 100):
            with self.assertRaisesRegex(UnsupportedCase, "stored chunk exceeds"):
                export_family(self.manifest, "/science", self.output, self.report)
        self.assertFalse(self.output.exists() or self.report.exists())
