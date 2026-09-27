"""Pre-incident capture is all-or-nothing and verifiable with an external pin."""

from __future__ import annotations

import tempfile
import unittest
import warnings
import zipfile
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.protection_bundle import (
    capture_protection_bundle, drill_protection_bundle,
    restore_from_protection_bundle, verify_protection_bundle,
)
from h5reclaim.recovery import RecoveryError, sha256_file


class ProtectionBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.source = self.base / "original.h5"
        self.bundle = self.base / "independently-retained.zip"
        with h5py.File(self.source, "x", libver="latest") as file:
            selected = file.create_dataset("science", shape=(8 * 16,), chunks=(16,), dtype="<u4")
            selected[...] = np.arange(128, dtype="<u4") * 3 + 5

    def test_capture_verify_and_disposable_root_loss_drill(self) -> None:
        before = sha256_file(self.source)
        captured = capture_protection_bundle(self.source, "/science", self.bundle)
        self.assertEqual(sha256_file(self.source), before)
        self.assertEqual(set(captured["components"]),
                         {"capsule.zip", "baseline.json", "erasure.zip"})
        self.assertEqual(verify_protection_bundle(
            self.bundle, captured["manifest_sha256"], source=self.source,
        )["captured_source"]["sha256"], before)
        drilled = drill_protection_bundle(self.bundle, captured["manifest_sha256"], self.source)
        self.assertEqual(drilled["restored_allocated_chunks"], 8)
        self.assertEqual(drilled["erasure_restored_chunks"], 2)
        self.assertEqual(sha256_file(self.source), before)
        self.assertEqual(list(self.base.glob(".h5reclaim-drill-*")), [])

    def test_capsule_only_accepts_sparse_fixed_records(self) -> None:
        compound = self.base / "records.h5"
        dtype = np.dtype([("epoch", "<i8"), ("flux", "<f4")])
        with h5py.File(compound, "x", libver="latest") as file:
            selected = file.create_dataset("science", shape=(4, 16), chunks=(1, 16), dtype=dtype)
            selected[0] = np.zeros(16, dtype=dtype)
            selected[2] = np.zeros(16, dtype=dtype)
        captured = capture_protection_bundle(compound, "/science", self.bundle,
                                              include_erasure=False)
        self.assertEqual(set(captured["components"]), {"capsule.zip"})
        self.assertEqual(drill_protection_bundle(
            self.bundle, captured["manifest_sha256"], compound,
        )["restored_allocated_chunks"], 2)

    def test_wrong_pin_tampered_component_and_changed_source_refuse(self) -> None:
        captured = capture_protection_bundle(self.source, "/science", self.bundle)
        with self.assertRaisesRegex(RecoveryError, "manifest disagrees"):
            verify_protection_bundle(self.bundle, "0" * 64)
        with self.source.open("r+b") as stream:
            stream.seek(250)
            previous = stream.read(1)
            stream.seek(250)
            stream.write(bytes([previous[0] ^ 1]))
        with self.assertRaisesRegex(RecoveryError, "current source differs"):
            verify_protection_bundle(self.bundle, captured["manifest_sha256"], source=self.source)

        corrupted = self.base / "tampered.zip"
        with zipfile.ZipFile(self.bundle) as original, zipfile.ZipFile(
            corrupted, "x", compression=zipfile.ZIP_STORED,
        ) as out:
            for info in original.infolist():
                payload = bytearray(original.read(info.filename))
                if info.filename == "capsule.zip":
                    payload[-8] ^= 0x1
                out.writestr(info.filename, bytes(payload), compress_type=zipfile.ZIP_STORED)
        with self.assertRaisesRegex(RecoveryError, "component differs"):
            verify_protection_bundle(corrupted, captured["manifest_sha256"])

    def test_capture_failure_never_publishes_partial_bundle(self) -> None:
        with patch("h5reclaim.protection_bundle.capture_erasure_sidecar",
                   side_effect=RecoveryError("injected erasure failure")):
            with self.assertRaisesRegex(RecoveryError, "injected erasure"):
                capture_protection_bundle(self.source, "/science", self.bundle)
        self.assertFalse(self.bundle.exists())
        self.assertEqual(list(self.base.glob(".h5reclaim-protect-*")), [])

    def test_existing_destination_never_replaced(self) -> None:
        self.bundle.write_bytes(b"existing")
        with self.assertRaisesRegex(RecoveryError, "must be a new file"):
            capture_protection_bundle(self.source, "/science", self.bundle)
        self.assertEqual(self.bundle.read_bytes(), b"existing")

    def test_duplicate_archive_member_refuses(self) -> None:
        captured = capture_protection_bundle(self.source, "/science", self.bundle,
                                              include_erasure=False)
        duplicate = self.base / "duplicate.zip"
        with zipfile.ZipFile(self.bundle) as original, zipfile.ZipFile(
            duplicate, "x", compression=zipfile.ZIP_STORED,
        ) as out:
            for name in original.namelist():
                out.writestr(name, original.read(name))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                out.writestr("capsule.zip", b"duplicate")
        with self.assertRaisesRegex(RecoveryError, "duplicate entries"):
            verify_protection_bundle(duplicate, captured["manifest_sha256"])

    def test_bundle_root_loss_restore_has_durable_provenance(self) -> None:
        captured = capture_protection_bundle(self.source, "/science", self.bundle)
        damaged = self.base / "broken-root.h5"
        shutil.copyfile(self.source, damaged)
        with damaged.open("r+b") as stream:
            stream.write(b"\0" * 8)
        damaged_digest = sha256_file(damaged)
        out, report_path = self.base / "capsule-result.h5", self.base / "capsule-report.json"
        report = restore_from_protection_bundle(
            damaged, "/science", self.bundle, captured["manifest_sha256"], out, report_path,
        )
        self.assertTrue(report["complete"])
        self.assertEqual(report["capsule"]["path"], str(self.bundle))
        self.assertEqual(report["capsule"]["bundle_member"], "capsule.zip")
        self.assertEqual(report["protection_bundle"]["sha256"], captured["bundle_sha256"])
        self.assertNotIn(".h5reclaim-bundle-restore-", json.dumps(report))
        self.assertEqual(json.loads(report_path.read_text()), report)
        self.assertEqual(sha256_file(damaged), damaged_digest)
        with h5py.File(out, "r") as file:
            np.testing.assert_array_equal(file["/science"][...], np.arange(128) * 3 + 5)
            self.assertEqual(json.loads(file["/_h5reclaim/report_json"][()]), report)

    def test_bundle_two_chunk_erasure_restore_has_durable_provenance(self) -> None:
        captured = capture_protection_bundle(self.source, "/science", self.bundle)
        damaged = self.base / "two-lost.h5"
        shutil.copyfile(self.source, damaged)
        with h5py.File(self.source, "r") as file:
            offsets = [file["/science"].id.get_chunk_info(index).byte_offset for index in (0, 2)]
        with damaged.open("r+b") as stream:
            for offset in offsets:
                stream.seek(offset)
                prior = stream.read(1)
                stream.seek(offset)
                stream.write(bytes([prior[0] ^ 0x7f]))
        damaged_digest = sha256_file(damaged)
        out, report_path = self.base / "parity-result.h5", self.base / "parity-report.json"
        report = restore_from_protection_bundle(
            damaged, "/science", self.bundle, captured["manifest_sha256"], out, report_path,
            method="erasure",
        )
        self.assertTrue(report["complete"])
        self.assertEqual(report["reconstructed_from_erasure"], 2)
        self.assertEqual(report["baseline"]["bundle_member"], "baseline.json")
        self.assertEqual(report["erasure"]["bundle_member"], "erasure.zip")
        self.assertEqual(report["manifest"]["kind"], "derived_from_protection_bundle")
        self.assertNotIn(".h5reclaim-bundle-restore-", json.dumps(report))
        self.assertEqual(json.loads(report_path.read_text()), report)
        self.assertEqual(sha256_file(damaged), damaged_digest)
        with h5py.File(out, "r") as file:
            np.testing.assert_array_equal(file["/science"][...], np.arange(128) * 3 + 5)
            self.assertEqual(json.loads(file["/_h5reclaim/report_json"][()]), report)

    def test_bundle_restore_wrong_pin_or_missing_method_refuses_publication(self) -> None:
        captured = capture_protection_bundle(self.source, "/science", self.bundle,
                                              include_erasure=False)
        output, report_path = self.base / "absent.h5", self.base / "absent.json"
        with self.assertRaisesRegex(RecoveryError, "manifest disagrees"):
            restore_from_protection_bundle(
                self.source, "/science", self.bundle, "0" * 64, output, report_path,
            )
        with self.assertRaisesRegex(RecoveryError, "no erasure sidecar"):
            restore_from_protection_bundle(
                self.source, "/science", self.bundle, captured["manifest_sha256"],
                output, report_path, method="erasure",
            )
        self.assertFalse(output.exists())
        self.assertFalse(report_path.exists())


if __name__ == "__main__":
    unittest.main()
