"""Automatic whole-file selection of checksum-constrained dimension trials."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.header_dimension_trial import _selected_dimension_field
from h5reclaim.modern_indexes import ModernH5File
from h5reclaim.recovery import RecoveryError
from h5reclaim.whole_file import rescue_all


class AutomaticDimensionRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def _source(self):
        source = self.directory / "damaged.h5"
        values = np.arange(77, dtype="<i4").reshape(11, 7)
        with h5py.File(source, "w", libver="latest") as handle:
            handle.attrs["experiment"] = "One complete scientific context attribute"
            selected = handle.create_dataset("science", data=values, chunks=(4, 3), fletcher32=True)
            selected.attrs["units"] = "arbitrary"
            address = int(h5py.h5o.get_info(selected.id).addr)
        with ModernH5File(source) as reader:
            start, raw, field, _, _ = _selected_dimension_field(reader, address, require_mismatch=False)
        return source, values, start + field.start, start + len(raw) - 4

    @staticmethod
    def _flip(source, offset):
        with source.open("r+b") as handle:
            handle.seek(offset)
            previous = handle.read(1)
            handle.seek(offset)
            handle.write(bytes((previous[0] ^ 1,)))

    def test_whole_file_recovers_dimension_values_and_context_from_a_private_trial(self):
        source, values, dimension, _ = self._source()
        self._flip(source, dimension)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        output, report = self.directory / "recovered.h5", self.directory / "report.json"

        result = rescue_all(source, output, report)

        self.assertEqual((result["datasets_exported"], result["datasets_failed"]), (1, 0))
        evidence = result["datasets"][0]["report"]["metadata_correction"]
        self.assertEqual(evidence["kind"], "selected_layout_chunk_dimension")
        self.assertEqual(evidence["physical_offset"], dimension)
        self.assertEqual(evidence["checksum_bytes_changed"], 0)
        with h5py.File(output, "r") as handle:
            np.testing.assert_array_equal(handle["science"][...], values)
            self.assertEqual(handle["science"].attrs["units"], "arbitrary")
            self.assertEqual(handle.attrs["experiment"], "One complete scientific context attribute")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_corrupt_stored_checksum_does_not_become_an_automatic_correction(self):
        source, _, _, checksum = self._source()
        self._flip(source, checksum)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        output, report = self.directory / "recovered.h5", self.directory / "report.json"

        with self.assertRaisesRegex(RecoveryError, "no dataset could be discovered"):
            rescue_all(source, output, report)

        self.assertFalse(output.exists())
        self.assertFalse(report.exists())
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
