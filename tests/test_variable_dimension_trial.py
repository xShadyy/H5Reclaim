"""Checksum-constrained dimension recovery of heap-backed records."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError
from h5reclaim.header_dimension_trial import _selected_dimension_field
from h5reclaim.metadata_trial_export import export_metadata_trial
from h5reclaim.modern_indexes import ModernH5File
from h5reclaim.whole_file import rescue_all


class VariableDimensionTrialTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def _source(self, kind: str) -> tuple[Path, int, int]:
        source = self.base / "source.h5"
        dtype = {
            "string": h5py.string_dtype("utf-8"),
            "ragged": h5py.vlen_dtype(np.dtype("<i4")),
            "compound_string": np.dtype([("id", "<i4"),
                                         ("payload", h5py.string_dtype("utf-8"))]),
            "compound_ragged": np.dtype([("id", "<i4"),
                                         ("payload", h5py.vlen_dtype(np.dtype("<i4")))]),
        }[kind]
        with h5py.File(source, "w", libver="latest") as handle:
            selected = handle.create_dataset("science", (9,), chunks=(4,), dtype=dtype)
            for i in range(9):
                if kind == "string":
                    selected[i] = f"sample-{i}"
                elif kind == "ragged":
                    selected[i] = np.arange(i + 1, dtype="<i4")
                elif kind == "compound_string":
                    selected[i] = (i, f"sample-{i}")
                else:
                    selected[i] = (i, np.arange(i + 1, dtype="<i4"))
            selected.attrs["units"] = "counts"
            address = int(h5py.h5o.get_info(selected.id).addr)
        with ModernH5File(source) as reader:
            start, raw, field, _, _ = _selected_dimension_field(reader, address,
                                                                require_mismatch=False)
        return source, start + field.start, start + len(raw) - 4

    def test_four_variable_families_retain_typed_values_and_source(self) -> None:
        for kind in ("string", "ragged", "compound_string", "compound_ragged"):
            with self.subTest(kind=kind):
                source, dimension, _ = self._source(kind)
                healthy = source.read_bytes()
                damaged = bytearray(healthy)
                damaged[dimension] ^= 4
                source.write_bytes(damaged)
                digest = hashlib.sha256(damaged).hexdigest()
                output, report = self.base / "output.h5", self.base / "report.json"
                result = export_metadata_trial(source, "/science", output, report,
                                               kind="dimension")
                self.assertEqual(result["mode"], "metadata_trial_native_stream_export")
                self.assertEqual(result["outcome"], "complete")
                self.assertEqual(result["accepted_elements"], 9)
                self.assertEqual(result["metadata_correction"]["checksum_bytes_changed"], 0)
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
                self.assertEqual(result["source"]["sha256_before"], digest)
                with h5py.File(output, "r") as recovered:
                    selected = recovered["science"]
                    self.assertEqual(selected.attrs["units"], "counts")
                    for i in range(9):
                        item = selected[i]
                        if kind == "string":
                            self.assertEqual(item, f"sample-{i}".encode())
                        elif kind == "ragged":
                            np.testing.assert_array_equal(item, np.arange(i + 1, dtype="<i4"))
                        elif kind == "compound_string":
                            self.assertEqual((item["id"], item["payload"]),
                                             (i, f"sample-{i}".encode()))
                        else:
                            self.assertEqual(item["id"], i)
                            np.testing.assert_array_equal(item["payload"],
                                                          np.arange(i + 1, dtype="<i4"))
                output.unlink()
                report.unlink()
                source.unlink()

    def test_unchanged_dimension_and_bad_checksum_are_not_repaired(self) -> None:
        source, dimension, checksum = self._source("compound_ragged")
        output, report = self.base / "output.h5", self.base / "report.json"
        with self.assertRaisesRegex(Exception, "checksum is intact"):
            export_metadata_trial(source, "/science", output, report, kind="dimension")
        damaged = bytearray(source.read_bytes())
        damaged[checksum] ^= 1
        source.write_bytes(damaged)
        with self.assertRaisesRegex(FormatError, "no unique original-checksum"):
            export_metadata_trial(source, "/science", output, report, kind="dimension")
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())
        self.assertEqual(source.read_bytes(), damaged)

    def test_automatic_whole_file_discovers_heap_backed_dimension_damage(self) -> None:
        source, dimension, _ = self._source("compound_ragged")
        damaged = bytearray(source.read_bytes())
        damaged[dimension] ^= 4
        source.write_bytes(damaged)
        output, report = self.base / "output.h5", self.base / "report.json"
        result = rescue_all(source, output, report)
        self.assertEqual((result["datasets_exported"], result["datasets_failed"]), (1, 0))
        self.assertEqual(result["datasets"][0]["report"]["mode"],
                         "metadata_trial_native_stream_export")
        self.assertEqual(source.read_bytes(), damaged)
        with h5py.File(output, "r") as recovered:
            for index in range(9):
                record = recovered["science"][index]
                self.assertEqual(record["id"], index)
                np.testing.assert_array_equal(record["payload"],
                                              np.arange(index + 1, dtype="<i4"))


if __name__ == "__main__":
    unittest.main()
