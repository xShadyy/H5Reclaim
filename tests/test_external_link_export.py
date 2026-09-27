"""Pinned HDF5 external links are materialized without path auto-resolution."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.external_link_export import export_external_link
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.metadata_fallback import _messages
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.recovery import RecoveryError


class ExternalLinkExportTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.parent = self.folder / "main.h5"
        self.related = self.folder / "actual-file.h5"
        self.output = self.folder / "result.h5"
        self.report = self.folder / "evidence.json"
        with h5py.File(self.related, "x") as handle:
            group = handle.create_group("instrument/experiment")
            dataset = group.create_dataset("science", shape=(12,), dtype="<u4", chunks=(4,))
            dataset[:4] = np.arange(4, dtype="<u4")
            dataset[8:] = np.arange(8, 12, dtype="<u4")
        with h5py.File(self.parent, "x") as handle:
            handle["entry"] = h5py.ExternalLink("not-present-by-that-name.h5", "/instrument")
        self.dataset_path = "/entry/experiment/science"
        self.manifest = {"schema_version": 1, "files": [{
            "declared_name": "not-present-by-that-name.h5", "path": str(self.related),
            "sha256": hashlib.sha256(self.related.read_bytes()).hexdigest(),
        }]}

    def test_group_link_materializes_sparse_target_at_requested_path(self) -> None:
        main_before = self.parent.read_bytes()
        related_before = self.related.read_bytes()
        report = export_external_link(self.parent, self.dataset_path, self.manifest,
                                      self.output, self.report)
        self.assertEqual(report["mode"], "external_link_export")
        self.assertEqual(report["outcome"], "partial")
        self.assertEqual(report["accepted_elements"], 8)
        self.assertEqual(report["external_link"]["resolved_local_dataset_path"],
                         "/instrument/experiment/science")
        self.assertEqual(report["dataset"]["path"], self.dataset_path)
        with h5py.File(self.output, "r") as handle:
            exported = handle[self.dataset_path]
            np.testing.assert_array_equal(exported[:4], np.arange(4, dtype="<u4"))
            np.testing.assert_array_equal(exported[8:], np.arange(8, 12, dtype="<u4"))
            np.testing.assert_array_equal(handle["/_h5reclaim/validity"][:], [1, 0, 1])
            self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()])["mode"],
                             "external_link_export")
            self.assertNotIn("instrument", handle)
        self.assertEqual(self.parent.read_bytes(), main_before)
        self.assertEqual(self.related.read_bytes(), related_before)

    def test_direct_dataset_link_is_supported(self) -> None:
        with h5py.File(self.parent, "r+") as handle:
            handle["direct"] = h5py.ExternalLink(
                "not-present-by-that-name.h5", "/instrument/experiment/science")
        report = export_external_link(self.parent, "/direct", self.manifest,
                                      self.output, self.report)
        self.assertEqual(report["dataset"]["path"], "/direct")
        self.assertEqual(report["accepted_elements"], 8)

    def test_wrong_pin_refuses_without_publishing(self) -> None:
        self.manifest["files"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(RecoveryError, "pinned SHA-256"):
            export_external_link(self.parent, self.dataset_path, self.manifest,
                                 self.output, self.report)
        self.assertFalse(self.output.exists() or self.report.exists())

    def test_missing_or_extra_manifest_name_refuses(self) -> None:
        self.manifest["files"][0]["declared_name"] = "different.h5"
        with self.assertRaisesRegex(UnsupportedCase, "exactly the file"):
            export_external_link(self.parent, self.dataset_path, self.manifest,
                                 self.output, self.report)
        self.assertFalse(self.output.exists() or self.report.exists())

    def test_target_soft_link_cannot_be_followed(self) -> None:
        with h5py.File(self.related, "r+") as handle:
            handle["instrument/experiment/soft"] = h5py.SoftLink("/instrument/experiment/science")
        self.manifest["files"][0]["sha256"] = hashlib.sha256(self.related.read_bytes()).hexdigest()
        with self.assertRaisesRegex(UnsupportedCase, "only local hard links"):
            export_external_link(self.parent, "/entry/experiment/soft", self.manifest,
                                 self.output, self.report)
        self.assertFalse(self.output.exists() or self.report.exists())

    def test_target_external_raw_dependency_cannot_be_followed(self) -> None:
        with h5py.File(self.related, "r+") as handle:
            handle.create_dataset("instrument/experiment/external", shape=(4,), dtype="<u4",
                                  external=[("unavailable.raw", 0, 16)])
        self.manifest["files"][0]["sha256"] = hashlib.sha256(self.related.read_bytes()).hexdigest()
        with self.assertRaisesRegex(UnsupportedCase, "external and virtual"):
            export_external_link(self.parent, "/entry/experiment/external", self.manifest,
                                 self.output, self.report)
        self.assertFalse(self.output.exists() or self.report.exists())

    def test_link_cannot_alias_selected_container(self) -> None:
        self.manifest["files"][0]["path"] = str(self.parent)
        self.manifest["files"][0]["sha256"] = hashlib.sha256(self.parent.read_bytes()).hexdigest()
        with self.assertRaisesRegex(UnsupportedCase, "aliases"):
            export_external_link(self.parent, self.dataset_path, self.manifest,
                                 self.output, self.report)

    def test_selected_payload_pointer_claiming_sibling_is_refused(self) -> None:
        with h5py.File(self.related, "w", libver="latest") as handle:
            selected = handle.create_dataset("selected", data=np.arange(12, dtype="<i4"))
            sibling = handle.create_dataset("sibling", data=np.full(12, 555, dtype="<i4"))
            selected_address = int(h5py.h5o.get_info(selected.id).addr)
            sibling_offset = int(sibling.id.get_offset())
        with h5py.File(self.parent, "r+") as handle:
            handle["redirected"] = h5py.ExternalLink("not-present-by-that-name.h5", "/selected")
        with ModernH5File(self.related) as reader:
            layout = next(message for message in _messages(reader, selected_address)
                          if message.kind == 8)
        raw = bytearray(self.related.read_bytes())
        flags = raw[selected_address + 5]
        width = 1 << (flags & 3)
        chunk_start = selected_address + 6 + (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0) + width
        chunk_length = int.from_bytes(raw[chunk_start - width:chunk_start], "little")
        raw[layout.absolute_offset + 2:layout.absolute_offset + 10] = sibling_offset.to_bytes(8, "little")
        checksum_at = chunk_start + chunk_length
        raw[checksum_at:checksum_at + 4] = lookup3(raw[selected_address:checksum_at]).to_bytes(4, "little")
        self.related.write_bytes(raw)
        self.manifest["files"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(UnsupportedCase, "sibling|ownership|overlap|claim"):
            export_external_link(self.parent, "/redirected", self.manifest,
                                 self.output, self.report)
        self.assertFalse(self.output.exists() or self.report.exists())

    def test_duplicate_manifest_key_is_refused(self) -> None:
        manifest = self.folder / "related.json"
        manifest.write_text('{"schema_version":1,"files":[{"declared_name":"not-present-by-that-name.h5",'
                            '"path":"/tmp/a","path":"/tmp/b","sha256":"' + '0' * 64 + '"}]}',
                            encoding="utf-8")
        with self.assertRaisesRegex(Exception, "duplicate.*path"):
            export_external_link(self.parent, self.dataset_path, manifest,
                                 self.output, self.report)
        self.assertFalse(self.output.exists() or self.report.exists())


if __name__ == "__main__":
    unittest.main()
