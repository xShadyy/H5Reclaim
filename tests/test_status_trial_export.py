"""Status-only native trial must preserve the source and label current values."""

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

from h5reclaim.format import FormatError
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.modern_indexes import lookup3
from h5reclaim.status_trial_export import export_status_trial


def _change_superblock(path: Path, *, flags: int | None = None,
                       eoa: int | None = None, checksum: bool = True) -> None:
    raw = bytearray(path.read_bytes())
    assert raw[:8] == b"\x89HDF\r\n\x1a\n" and raw[8] == 3
    osize = raw[9]
    size = 16 + 4 * osize
    if flags is not None:
        raw[11] = flags
    if eoa is not None:
        raw[12 + 2 * osize:12 + 3 * osize] = eoa.to_bytes(osize, "little")
    if checksum:
        raw[size - 4:size] = lookup3(raw[:size - 4]).to_bytes(4, "little")
    path.write_bytes(raw)


class StatusTrialExportTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "damaged.h5"
        self.truth = np.arange(24, dtype="<i4").reshape(6, 4)
        with h5py.File(self.source, "w", libver="latest") as file:
            file.create_dataset("science", data=self.truth, chunks=(2, 4),
                                compression="gzip", fletcher32=True)
        self.assertEqual(self.source.read_bytes()[8], 3)

    def test_public_rescue_exports_only_from_disposable_status_trial(self) -> None:
        _change_superblock(self.source, flags=1)
        original = self.source.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        with self.assertRaises(OSError):
            with h5py.File(self.source, "r"):
                pass
        output, report = self.root / "result.h5", self.root / "result.json"
        result = subprocess.run([
            sys.executable, "-m", "h5reclaim", "rescue", str(self.source),
            "--dataset", "/science", "--status-trial", "--output", str(output),
            "--report", str(report),
        ], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status-only trial", result.stdout)
        self.assertEqual(self.source.read_bytes(), original)
        evidence = json.loads(report.read_text())
        self.assertEqual(evidence["mode"], "status_trial_readable_export")
        self.assertEqual(evidence["source"]["sha256_before"], digest)
        self.assertEqual(evidence["accepted_elements"], 24)
        self.assertFalse(evidence["status_trial"]["historical_measurements_verified"])
        self.assertTrue(evidence["status_trial"]["all_other_source_bytes_identical"])
        with h5py.File(output, "r") as file:
            np.testing.assert_array_equal(file["science"][:], self.truth)
            embedded = json.loads(file["/_h5reclaim/report_json"][()])
            self.assertEqual(embedded, evidence)
            self.assertEqual(file["/_h5reclaim"].attrs["source_sha256"], digest)

    def test_invalid_checksum_refuses_before_copy(self) -> None:
        _change_superblock(self.source, flags=1, checksum=False)
        output, report = self.root / "no.h5", self.root / "no.json"
        with self.assertRaisesRegex(FormatError, "checksum"):
            export_status_trial(self.source, "/science", output, report)
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    def test_reserved_flag_and_eoa_past_eof_refuse(self) -> None:
        _change_superblock(self.source, flags=3)
        output, report = self.root / "no.h5", self.root / "no.json"
        with self.assertRaisesRegex(UnsupportedCase, "status trial requires"):
            export_status_trial(self.source, "/science", output, report)
        self.assertFalse(output.exists())
        with h5py.File(self.root / "healthy.h5", "w", libver="latest") as file:
            file.create_dataset("science", data=self.truth)
        self.source.write_bytes((self.root / "healthy.h5").read_bytes())
        _change_superblock(self.source, flags=1, eoa=self.source.stat().st_size + 100)
        with self.assertRaisesRegex(UnsupportedCase, "status trial requires"):
            export_status_trial(self.source, "/science", output, report)
        self.assertFalse(output.exists())

    def test_no_status_flag_is_not_a_repair_case(self) -> None:
        output, report = self.root / "no.h5", self.root / "no.json"
        with self.assertRaisesRegex(UnsupportedCase, "status trial requires"):
            export_status_trial(self.source, "/science", output, report)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
