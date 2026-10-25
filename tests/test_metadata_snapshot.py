"""A prior full-file metadata snapshot can repair only captured, changed bytes."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.__main__ import main
from h5reclaim.metadata_snapshot import capture_metadata_snapshot, restore_metadata_snapshot
from h5reclaim.preflight import inspect_metadata_local
from h5reclaim.recovery import RecoveryError


class MetadataSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.healthy = self.root / "healthy.h5"
        self.damaged = self.root / "damaged.h5"
        self.snapshot = self.root / "checked-metadata.zip"
        self.output = self.root / "restored.h5"
        self.report = self.root / "audit.json"
        with h5py.File(self.healthy, "w", libver="latest") as handle:
            group = handle.create_group("science")
            group.attrs["unit"] = "count"
            item = group.create_dataset("values", data=np.arange(240, dtype="<i4").reshape(20, 12),
                                        chunks=(4, 3), compression="gzip")
            item.attrs["instrument"] = "alpha"
            handle["other"] = np.arange(12, dtype="<i8")
        self.expected = hashlib.sha256(self.healthy.read_bytes()).hexdigest()
        self.capture = capture_metadata_snapshot(self.healthy, self.snapshot)
        shutil.copyfile(self.healthy, self.damaged)

    def corrupt_message(self, path, kind):
        selected = next(item for item in inspect_metadata_local(self.healthy)["objects"]
                        if item["path"] == path)
        message = next(message for message in selected["messages"] if message["kind"] == kind)
        with self.damaged.open("r+b") as stream:
            stream.seek(message["offset"])
            stream.write(bytes(message["bytes"]))

    def test_recovers_missing_datatype_and_group_link_without_touching_input(self):
        self.corrupt_message("/science/values", 3)
        self.corrupt_message("/science", 6)
        changed_source = hashlib.sha256(self.damaged.read_bytes()).hexdigest()
        self.assertNotEqual(changed_source, self.expected)
        self.assertEqual(inspect_metadata_local(self.damaged)["decision"], "reject_corrupt")
        report = restore_metadata_snapshot(self.damaged, self.snapshot,
                    self.capture["archive_sha256"], self.output, self.report)
        self.assertEqual(report["restored_sha256"], self.expected)
        self.assertTrue(report["changed_ranges"])
        self.assertEqual(hashlib.sha256(self.output.read_bytes()).hexdigest(), self.expected)
        self.assertEqual(hashlib.sha256(self.damaged.read_bytes()).hexdigest(), changed_source)
        self.assertEqual(json.loads(self.report.read_text())["changed_ranges"], report["changed_ranges"])
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["science/values"][:], np.arange(240).reshape(20, 12))
            self.assertEqual(handle["science"].attrs["unit"], "count")

    def test_rejects_changed_measurement_and_preserves_new_destinations(self):
        self.corrupt_message("/science/values", 3)
        with h5py.File(self.healthy, "r") as handle:
            offset = handle["other"].id.get_offset()
        with self.damaged.open("r+b") as stream:
            stream.seek(offset)
            old = stream.read(1)
            stream.seek(offset)
            stream.write(bytes([old[0] ^ 0x40]))
        with self.assertRaisesRegex(RecoveryError, "uncaptured bytes changed"):
            restore_metadata_snapshot(self.damaged, self.snapshot,
                self.capture["archive_sha256"], self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_requires_independent_hash_and_never_overwrites(self):
        self.corrupt_message("/science/values", 3)
        with self.assertRaisesRegex(RecoveryError, "independently retained digest"):
            restore_metadata_snapshot(self.damaged, self.snapshot, "0" * 64,
                                      self.output, self.report)
        self.output.write_bytes(b"prior result")
        with self.assertRaisesRegex(RecoveryError, "destination already exists"):
            restore_metadata_snapshot(self.damaged, self.snapshot,
                self.capture["archive_sha256"], self.output, self.report)
        self.assertEqual(self.output.read_bytes(), b"prior result")
        self.assertFalse(self.report.exists())

    def test_cli_capture_and_restore_with_missing_superblock_signature(self):
        second = self.root / "cli-snapshot.zip"
        self.assertEqual(main(["capture-metadata", str(self.healthy), "--output", str(second)]), 0)
        digest = hashlib.sha256(second.read_bytes()).hexdigest()
        with self.damaged.open("r+b") as stream:
            stream.write(bytes(8))
        self.assertEqual(main(["restore-metadata", str(self.damaged), str(second),
                               "--snapshot-sha256", digest, "--output", str(self.output),
                               "--report", str(self.report)]), 0)
        self.assertEqual(hashlib.sha256(self.output.read_bytes()).hexdigest(), self.expected)

    def test_legacy_userblock_is_preserved_while_its_header_is_repaired(self):
        healthy = self.root / "legacy.h5"
        damaged = self.root / "legacy-damaged.h5"
        snapshot = self.root / "legacy-metadata.zip"
        output = self.root / "legacy-result.h5"
        report_path = self.root / "legacy-report.json"
        with h5py.File(healthy, "w", libver="earliest", userblock_size=512) as handle:
            handle["reading"] = np.arange(64, dtype="<i8")
        with healthy.open("r+b") as stream:
            stream.write(b"retained user block\n")
        record = capture_metadata_snapshot(healthy, snapshot)
        self.assertEqual(record["metadata_ranges"][0][0], 512)
        shutil.copyfile(healthy, damaged)
        with damaged.open("r+b") as stream:
            stream.seek(512)
            stream.write(bytes(8))
        restore_metadata_snapshot(damaged, snapshot, record["archive_sha256"], output, report_path)
        self.assertEqual(output.read_bytes(), healthy.read_bytes())
        with h5py.File(output, "r") as handle:
            np.testing.assert_array_equal(handle["reading"][:], np.arange(64))


if __name__ == "__main__":
    unittest.main()
