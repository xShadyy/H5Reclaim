"""Whole-file recovery of checked chunk-dimension faults across data layouts."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.header_dimension_trial import _selected_dimension_field
from h5reclaim.modern_indexes import ModernH5File
from h5reclaim.whole_file import rescue_all


class DimensionMoreFamiliesTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.source = self.directory / "damaged.h5"
        self.output = self.directory / "recovered.h5"
        self.report = self.directory / "report.json"

    def _damage_first_chunk_dimension(self, *, require_continuation: bool = False) -> int:
        with h5py.File(self.source, "r") as handle:
            address = int(h5py.h5o.get_info(handle["science"].id).addr)
        with ModernH5File(self.source) as reader:
            start, raw, field, _, _ = _selected_dimension_field(
                reader, address, require_mismatch=False,
            )
            if require_continuation:
                # This fixture covers a layout in the checked first header
                # chunk with an independently checked continuation attached.
                self.assertIn(b"\x10\x10\x00\x00", raw)
        offset = start + field.start
        with self.source.open("r+b") as handle:
            handle.seek(offset)
            before = handle.read(1)
            handle.seek(offset)
            handle.write(bytes((before[0] ^ 1,)))
        return offset

    def _assert_recovered(self, values: np.ndarray, offset: int) -> None:
        damaged_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = rescue_all(self.source, self.output, self.report)
        self.assertEqual((result["datasets_exported"], result["datasets_failed"]), (1, 0))
        evidence = result["datasets"][0]["report"]["metadata_correction"]
        self.assertEqual(evidence["kind"], "selected_layout_chunk_dimension")
        self.assertEqual(evidence["physical_offset"], offset)
        self.assertEqual(evidence["checksum_bytes_changed"], 0)
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["science"][...], values)
            self.assertEqual(handle["science"].attrs["units"], "arbitrary")
            self.assertEqual(handle.attrs["experiment"], "checked dimension recovery")
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), damaged_hash)

    def test_rank_five_fixed_array_chunk_dimension(self) -> None:
        values = np.arange(64, dtype="<i4").reshape(2, 2, 2, 2, 4)
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.attrs["experiment"] = "checked dimension recovery"
            dataset = handle.create_dataset(
                "science", data=values, chunks=(1, 1, 1, 1, 4),
                compression="gzip", fletcher32=True,
            )
            dataset.attrs["units"] = "arbitrary"
        offset = self._damage_first_chunk_dimension()
        self._assert_recovered(values, offset)

    def test_scaleoffset_with_checked_object_header_continuation(self) -> None:
        values = np.arange(-37, 40, dtype="<i4").reshape(11, 7)
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.attrs["experiment"] = "checked dimension recovery"
            dataset = handle.create_dataset(
                "science", data=values, chunks=(4, 3), scaleoffset=0,
            )
            dataset.attrs["units"] = "arbitrary"
        offset = self._damage_first_chunk_dimension(require_continuation=True)
        self._assert_recovered(values, offset)


if __name__ == "__main__":
    unittest.main()
