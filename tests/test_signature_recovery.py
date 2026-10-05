"""Modern magic restoration needs retained checksums and a unique rooted view."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.checked_view import checked_root_view
from h5reclaim.format import FormatError, SIGNATURE
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.metadata_correction import MAX_SIGNATURE_OFFSET, _read_checked_header, _superblock_raw
from h5reclaim.modern_indexes import ModernH5File, lookup3


class SignatureRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="h5reclaim-signature-test-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.values = np.arange(32, dtype="<u4").reshape(8, 4)

    def _source(self, name: str, *, libver="latest", userblock: int = 0) -> Path:
        source = self.directory / f"{name}.h5"
        with h5py.File(source, "w", libver=libver, userblock_size=userblock) as handle:
            handle.attrs["experiment"] = "independent signature test"
            group = handle.create_group("experiment")
            group.attrs["units"] = "counts"
            group.create_dataset("readings", data=self.values, chunks=(2, 4),
                                 compression="gzip", shuffle=True, fletcher32=True)
            handle.create_dataset("labels", data=["alpha", "μ sample"], dtype=h5py.string_dtype())
        if userblock:
            with source.open("r+b") as handle:
                handle.write(b"Application metadata retained before HDF5\n".ljust(userblock, b"\x00"))
        return source

    @staticmethod
    def _damage(source: Path, *offsets: int) -> bytes:
        raw = bytearray(source.read_bytes())
        for offset in offsets:
            raw[offset] ^= 1
        source.write_bytes(raw)
        return bytes(raw)

    def _rescue(self, source: Path, *options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "h5reclaim", "rescue", str(source), *options],
            capture_output=True, text=True, timeout=60,
        )

    @staticmethod
    def _destinations(source: Path) -> tuple[Path, Path]:
        return (source.with_name(source.stem + ".recovered.h5"),
                source.with_name(source.stem + ".recovered.report.json"))

    def test_each_signature_byte_in_v2_and_v3_restores_only_that_byte(self) -> None:
        for bounds, expected_version in ((('v108', 'v108'), 2), ('latest', 3)):
            source = self._source(f"all-bytes-v{expected_version}", libver=bounds)
            healthy = source.read_bytes()
            self.assertEqual(healthy[8], expected_version)
            for index in range(8):
                with self.subTest(version=expected_version, byte=index):
                    source.write_bytes(healthy)
                    damaged = self._damage(source, index)
                    with checked_root_view(source) as (view, evidence):
                        self.assertNotEqual(view, source)
                        self.assertEqual(view.read_bytes(), healthy)
                        self.assertEqual(evidence["kind"], "unique_superblock_signature_checksum_correction")
                        self.assertEqual(evidence["physical_byte"], index)
                        self.assertEqual((evidence["before"], evidence["after"]),
                                         (damaged[index], SIGNATURE[index]))
                        self.assertEqual(evidence["checksum_bytes_changed"], 0)
                        self.assertEqual(view.read_bytes()[44:48], damaged[44:48])
                        with h5py.File(view, "r") as handle:
                            np.testing.assert_array_equal(handle["experiment/readings"][:], self.values)
                    self.assertFalse(view.exists())
                    self.assertEqual(source.read_bytes(), damaged)

    def test_default_whole_rescue_preserves_userblocks_values_and_context(self) -> None:
        for bounds, userblock in ((('v108', 'v108'), 512), ('latest', 1024)):
            with self.subTest(libver=bounds, userblock=userblock):
                source = self._source(f"whole block {userblock}", libver=bounds, userblock=userblock)
                damaged = self._damage(source, userblock + 7)
                result = self._rescue(source)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                output, report_path = self._destinations(source)
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(report["outcome"], "complete", report)
                self.assertEqual(report["datasets_exported"], 2)
                self.assertEqual(report["userblock"]["size_bytes"], userblock)
                evidence = report["inventory"]["inspection_view"]
                self.assertEqual(evidence["kind"], "unique_superblock_signature_checksum_correction")
                self.assertEqual(evidence["physical_byte"], userblock + 7)
                self.assertEqual(report["historical_integrity"]["status"], "unknown")
                with h5py.File(output, "r") as handle:
                    np.testing.assert_array_equal(handle["experiment/readings"][:], self.values)
                    self.assertEqual(handle["labels"].asstr()[:].tolist(), ["alpha", "μ sample"])
                    self.assertEqual(handle.attrs["experiment"], "independent signature test")
                    self.assertEqual(handle["experiment"].attrs["units"], "counts")
                self.assertEqual(output.read_bytes()[:userblock], damaged[:userblock])
                self.assertEqual(source.read_bytes(), damaged)

    def test_selected_default_rescue_reports_signature_evidence(self) -> None:
        source = self._source("selected")
        damaged = self._damage(source, 3)
        result = self._rescue(source, "--dataset", "/experiment/readings")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output, report_path = self._destinations(source)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["outcome"], "complete", report)
        evidence = report["checked_root_correction"]
        self.assertEqual(evidence["kind"], "unique_superblock_signature_checksum_correction")
        self.assertEqual(evidence["physical_byte"], 3)
        self.assertEqual(evidence["checksum_bytes_changed"], 0)
        with h5py.File(output, "r") as handle:
            np.testing.assert_array_equal(handle["experiment/readings"][:], self.values)
        self.assertEqual(source.read_bytes(), damaged)

    def test_intact_modern_files_use_the_original_without_correction(self) -> None:
        for bounds, userblock in ((('v108', 'v108'), 0), ('latest', 512)):
            with self.subTest(libver=bounds, userblock=userblock):
                source = self._source(f"intact-{userblock}", libver=bounds, userblock=userblock)
                original = source.read_bytes()
                with checked_root_view(source) as (view, evidence):
                    self.assertEqual(view, source)
                    self.assertIsNone(evidence)
                self.assertEqual(source.read_bytes(), original)

    def test_legacy_two_signature_bytes_and_changed_checksum_never_export(self) -> None:
        for kind in ("legacy", "two-bytes", "changed-checksum"):
            with self.subTest(kind=kind):
                source = self._source(kind, libver="earliest" if kind == "legacy" else "latest")
                offsets = (0, 1) if kind == "two-bytes" else (0, 44) if kind == "changed-checksum" else (0,)
                damaged = self._damage(source, *offsets)
                with self.assertRaises(UnsupportedCase):
                    _superblock_raw(source)
                with checked_root_view(source) as (view, evidence):
                    self.assertEqual(view, source)
                    self.assertIsNone(evidence)
                result = self._rescue(source)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                output, report = self._destinations(source)
                self.assertFalse(output.exists())
                self.assertFalse(report.exists())
                self.assertEqual(source.read_bytes(), damaged)

    def test_two_checksum_justified_candidates_are_refused_before_root_selection(self) -> None:
        for second_kind in ("near-signature", "exact-signature"):
            with self.subTest(second_kind=second_kind):
                source = self._source(f"ambiguous-{second_kind}", userblock=512)
                raw = bytearray(source.read_bytes())
                second = bytearray(raw[512:560])
                second[12:20] = (0).to_bytes(8, "little")
                root = int.from_bytes(second[36:44], "little")
                second[36:44] = (512 + root).to_bytes(8, "little")
                second[44:48] = lookup3(second[:44]).to_bytes(4, "little")
                second[2] ^= 1
                raw[:48] = second
                if second_kind == "near-signature":
                    raw[512] ^= 1
                source.write_bytes(raw)
                damaged = bytes(raw)
                with self.assertRaisesRegex(FormatError, "ambiguous"):
                    _superblock_raw(source)
                with self.assertRaisesRegex(FormatError, "ambiguous"):
                    with checked_root_view(source):
                        self.fail("ambiguous view must not be yielded")
                result = self._rescue(source)
                output, report_path = self._destinations(source)
                if second_kind == "near-signature":
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertFalse(output.exists())
                    self.assertFalse(report_path.exists())
                else:
                    # An independently intact signature can still support
                    # detached values, without accepting an ambiguous repair
                    # or claiming the original namespace is complete.
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    self.assertEqual(report["outcome"], "partial")
                    self.assertEqual(report["inventory"]["inspection_view"],
                                     "detached checksummed dataset headers")
                    self.assertNotIn("unique_superblock_signature_checksum_correction", json.dumps(report))
                    self.assertEqual(report["datasets_exported"], 2)
                    with h5py.File(output, "r") as handle:
                        for item in report["datasets"]:
                            dataset = handle[item["path"]]
                            if dataset.shape == self.values.shape:
                                np.testing.assert_array_equal(dataset[:], self.values)
                            else:
                                self.assertEqual(dataset.asstr()[:].tolist(), ["alpha", "μ sample"])
                self.assertEqual(source.read_bytes(), damaged)

    def test_signature_checksum_alone_does_not_establish_a_checked_root_group(self) -> None:
        for kind in ("dataset-root", "damaged-root-header"):
            with self.subTest(kind=kind):
                source = self._source(kind)
                raw = bytearray(source.read_bytes())
                if kind == "dataset-root":
                    with h5py.File(source, "r") as handle:
                        address = int(h5py.h5o.get_info(handle["experiment/readings"].id).addr)
                    raw[36:44] = address.to_bytes(8, "little")
                    raw[44:48] = lookup3(raw[:44]).to_bytes(4, "little")
                else:
                    with ModernH5File(source) as reader:
                        root = reader.superblock.root_object_address
                        header, _prefix_length = _read_checked_header(reader, root)
                    raw[root + len(header) - 1] ^= 1
                raw[0] ^= 1
                source.write_bytes(raw)
                damaged = bytes(raw)
                with self.assertRaises(FormatError):
                    with checked_root_view(source):
                        self.fail("an unvalidated root must not be yielded")
                result = self._rescue(source, "--dataset", "/experiment/readings")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(self._destinations(source)[0].exists())
                self.assertEqual(source.read_bytes(), damaged)

    def test_non_documented_and_beyond_bound_signature_candidates_are_ignored(self) -> None:
        for offset in (256, 2 * MAX_SIGNATURE_OFFSET):
            with self.subTest(offset=offset):
                ordinary = self._source(f"offset-source-{offset}")
                healthy = ordinary.read_bytes()
                raw = bytearray(b"\x00" * offset + healthy)
                raw[offset + 12:offset + 20] = offset.to_bytes(8, "little")
                raw[offset + 28:offset + 36] = len(raw).to_bytes(8, "little")
                raw[offset + 44:offset + 48] = lookup3(raw[offset:offset + 44]).to_bytes(4, "little")
                raw[offset] ^= 1
                source = self.directory / f"outside-{offset}.h5"
                source.write_bytes(raw)
                with self.assertRaises(UnsupportedCase):
                    _superblock_raw(source)
                self.assertEqual(source.read_bytes(), bytes(raw))


if __name__ == "__main__":
    unittest.main()
