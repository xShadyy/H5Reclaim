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
from h5reclaim.large_streaming import LargeBudget
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.modern_indexes import lookup3
from h5reclaim.status_trial_export import _status_only_trial, export_status_trial


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

    def _rescue(self, source: Path, name: str, *extra: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        output, report = self.root / f"{name}.h5", self.root / f"{name}.json"
        result = subprocess.run([
            sys.executable, "-m", "h5reclaim", "rescue", str(source),
            "--output", str(output), "--report", str(report), *extra,
        ], capture_output=True, text=True, check=False, timeout=60)
        return result, output, report

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

    def test_rank_five_fixed_records_use_checked_native_stream(self) -> None:
        source = self.root / "rank5.h5"
        truth = np.arange(96, dtype=">i4").reshape(2, 2, 2, 2, 6)
        with h5py.File(source, "w", libver="latest") as handle:
            handle.create_dataset("science", data=truth, chunks=(1, 1, 1, 1, 3),
                                  compression="gzip", fletcher32=True)
        _change_superblock(source, flags=5)
        original = source.read_bytes()
        for name, options in (("rank5-auto", []), ("rank5-explicit", ["--status-trial"])):
            result, output, report = self._rescue(source, name, "--dataset", "/science", *options)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(report.read_text())
            self.assertEqual(evidence["mode"], "status_trial_readable_export")
            self.assertEqual(evidence["accepted_elements"], truth.size)
            self.assertIsNone(evidence["checked_root_correction"])
            self.assertEqual(evidence["source"]["sha256_before"], hashlib.sha256(original).hexdigest())
            with h5py.File(output) as handle:
                np.testing.assert_array_equal(handle["science"][:], truth)
                self.assertEqual(handle["science"].dtype.str, truth.dtype.str)
                self.assertTrue(np.all(handle[evidence["validity_map"]][:] == 1))
            self.assertEqual(source.read_bytes(), original)

    def test_variable_unicode_strings_recover_after_checked_status_trial(self) -> None:
        source = self.root / "unicode.h5"
        truth = ["Maastricht", "Zażółć gęślą jaźń", "測定", "", "αβγ"]
        with h5py.File(source, "w", libver="latest") as handle:
            handle.create_dataset("science", data=np.array(truth, dtype=object),
                                  dtype=h5py.string_dtype("utf-8"), chunks=(2,), compression="gzip")
        _change_superblock(source, flags=1)
        original = source.read_bytes()
        for name, options in (("strings-auto", []), ("strings-explicit", ["--status-trial"])):
            result, output, report = self._rescue(source, name, "--dataset", "/science", *options)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(report.read_text())
            self.assertEqual(evidence["accepted_elements"], len(truth))
            self.assertEqual(evidence["unknown_elements"], 0)
            self.assertFalse(evidence["status_trial"]["historical_measurements_verified"])
            with h5py.File(output) as handle:
                self.assertEqual(handle["science"].asstr()[:].tolist(), truth)
                self.assertTrue(np.all(handle[evidence["element_status"]][:] == 1))
                self.assertEqual(json.loads(handle[evidence["metadata_group"] + "/report_json"][()]), evidence)
            self.assertEqual(source.read_bytes(), original)

    def test_source_owned_metadata_namespace_is_preserved_in_auto_and_explicit_rescue(self) -> None:
        source = self.root / "reserved.h5"
        with h5py.File(source, "w", libver="latest") as handle:
            handle.create_dataset("/_h5reclaim/science", data=self.truth, chunks=(2, 4))
            handle["/_h5reclaim"].attrs["owner"] = "instrument"
        _change_superblock(source, flags=1)
        original = source.read_bytes()
        for name, options in (("reserved-auto", []), ("reserved-explicit", ["--status-trial"])):
            result, output, report = self._rescue(source, name, "--dataset", "/_h5reclaim/science", *options)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(report.read_text())
            self.assertEqual(evidence["metadata_group"], "/_h5reclaim_metadata")
            with h5py.File(output) as handle:
                np.testing.assert_array_equal(handle["/_h5reclaim/science"][:], self.truth)
                self.assertEqual(json.loads(handle[evidence["metadata_group"] + "/report_json"][()]), evidence)
                self.assertEqual(handle[evidence["metadata_group"]].attrs["source_sha256"],
                                 hashlib.sha256(original).hexdigest())
            self.assertEqual(source.read_bytes(), original)

        # The default whole-file workflow also restores the source group's attributes.
        result, output, report = self._rescue(source, "reserved-whole")
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(report.read_text())
        self.assertEqual(evidence["datasets_exported"], 1)
        self.assertNotEqual(evidence["metadata_group"], "/_h5reclaim")
        with h5py.File(output) as handle:
            np.testing.assert_array_equal(handle["/_h5reclaim/science"][:], self.truth)
            self.assertEqual(handle["/_h5reclaim"].attrs["owner"], "instrument")
        self.assertEqual(source.read_bytes(), original)

    def test_status_wrapper_and_public_explicit_route_honor_logical_budget(self) -> None:
        _change_superblock(self.source, flags=1)
        original = self.source.read_bytes()
        output, report = self.root / "limited.h5", self.root / "limited.json"
        with self.assertRaisesRegex(UnsupportedCase, "logical records"):
            export_status_trial(self.source, "/science", output, report,
                                budget=LargeBudget(max_logical_bytes=16))
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())
        budget = self.root / "budget.json"
        budget.write_text(json.dumps({"max_logical_bytes": 16}), encoding="utf-8")
        result, output, report = self._rescue(self.source, "public-limited", "--dataset", "/science",
                                            "--status-trial", "--streaming-budget", str(budget))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("logical records", result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())
        self.assertEqual(self.source.read_bytes(), original)

    def test_status_trial_copy_is_independently_checked_for_only_allowed_byte_changes(self) -> None:
        _change_superblock(self.source, flags=1)
        original = self.source.read_bytes()
        trial = self.root / "private-trial.h5"
        details = _status_only_trial(self.source, trial, len(original))
        changed = {position for position, (left, right) in enumerate(zip(original, trial.read_bytes()))
                   if left != right}
        self.assertEqual(len(changed), details["changed_bytes"])
        self.assertTrue(changed <= set(details["allowed_changed_offsets"]))
        self.assertIn(11, changed)
        self.assertEqual(details["original_source_sha256"], hashlib.sha256(original).hexdigest())
        raw = trial.read_bytes()
        header_size = 16 + 4 * raw[9]
        self.assertEqual(int.from_bytes(raw[header_size - 4:header_size], "little"), lookup3(raw[:header_size - 4]))
        self.assertEqual(raw[11], 0)
        self.assertEqual(self.source.read_bytes(), original)

    def test_optional_packaged_codec_is_registered_for_explicit_status_route(self) -> None:
        try:
            import hdf5plugin
        except ImportError:
            self.skipTest("hdf5plugin is not installed")
        source = self.root / "zstd.h5"
        with h5py.File(source, "w", libver="latest") as handle:
            handle.create_dataset("science", data=self.truth, chunks=(2, 4), **hdf5plugin.Zstd())
        _change_superblock(source, flags=1)
        original = source.read_bytes()
        result, output, report = self._rescue(source, "codec", "--dataset", "/science", "--status-trial")
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(report.read_text())
        self.assertIn(32015, evidence["dataset"]["filters_in_order"])
        with h5py.File(output) as handle:
            np.testing.assert_array_equal(handle["science"][:], self.truth)
        self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
