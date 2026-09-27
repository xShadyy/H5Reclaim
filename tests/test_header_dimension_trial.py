"""Selected modern chunk-dimension trials require the original checksum."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError, UnsupportedFormat
from h5reclaim.header_dimension_trial import (
    _selected_dimension_field, create_chunk_dimension_trial,
)
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.modern_indexes import ModernH5File, lookup3


class HeaderDimensionTrialTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def _create(self, shape=(32,), chunks=(8,), maxshape=None):
        source, trial = self.directory / "damaged.h5", self.directory / "trial.h5"
        values = np.arange(np.prod(shape), dtype="<u4").reshape(shape)
        with h5py.File(source, "w", libver="latest") as file:
            dataset = file.create_dataset("science", data=values, chunks=chunks, maxshape=maxshape)
            address = int(h5py.h5o.get_info(dataset.id).addr)
        return source, trial, address, values

    def _dimension_range(self, source: Path, address: int):
        with ModernH5File(source) as reader:
            return _selected_dimension_field(reader, address, require_mismatch=False)

    def _damage_dimension(self, source: Path, address: int) -> tuple[bytes, int, int]:
        start, raw, field, _rank, _width = self._dimension_range(source, address)
        physical = start + field.start
        original_checksum = int.from_bytes(raw[-4:], "little")
        content = bytearray(source.read_bytes())
        content[physical] ^= 4
        source.write_bytes(content)
        return bytes(content), physical, original_checksum

    def test_fixed_array_one_byte_preserves_source_and_original_header_checksum(self) -> None:
        source, trial, address, values = self._create()
        healthy = source.read_bytes()
        damaged, physical, checksum = self._damage_dimension(source, address)
        evidence = create_chunk_dimension_trial(source, "/science", trial)
        self.assertEqual(evidence.kind, "selected_layout_chunk_dimension")
        self.assertEqual(evidence.physical_offset, physical)
        self.assertEqual(evidence.original_checksum, checksum)
        self.assertEqual((evidence.dimension_index, evidence.dimension_before,
                          evidence.dimension_after), (0, 12, 8))
        self.assertEqual((evidence.changed_bytes, evidence.checksum_bytes_changed), (1, 0))
        self.assertFalse(evidence.historical_payload_verified)
        self.assertEqual(evidence.source_sha256, hashlib.sha256(damaged).hexdigest())
        self.assertEqual(evidence.trial_sha256, hashlib.sha256(healthy).hexdigest())
        self.assertEqual(source.read_bytes(), damaged)
        self.assertEqual(trial.read_bytes(), healthy)
        with h5py.File(trial, "r") as file:
            np.testing.assert_array_equal(file["science"][...], values)

    def test_extensible_array_and_v2_btree_one_byte(self) -> None:
        for kind, shape, chunks, maxshape in (
            ("extensible-array", (32,), (8,), (None,)),
            ("v2-btree", (16, 16), (4, 4), (None, None)),
        ):
            with self.subTest(kind=kind):
                source, trial, address, values = self._create(shape, chunks, maxshape)
                damaged, offset, _checksum = self._damage_dimension(source, address)
                evidence = create_chunk_dimension_trial(source, "/science", trial)
                self.assertEqual(evidence.physical_offset, offset)
                self.assertEqual(source.read_bytes(), damaged)
                with h5py.File(trial, "r") as file:
                    np.testing.assert_array_equal(file["science"][...], values)
                trial.unlink()
                source.unlink()

    def test_checksum_only_and_two_dimension_bytes_refuse_without_trial(self) -> None:
        source, trial, address, _ = self._create()
        start, raw, field, _rank, _width = self._dimension_range(source, address)
        healthy = source.read_bytes()
        checksum_offset = start + len(raw) - 4
        content = bytearray(healthy)
        content[checksum_offset] ^= 1
        source.write_bytes(content)
        with self.assertRaisesRegex(FormatError, "no unique original-checksum"):
            create_chunk_dimension_trial(source, "/science", trial)
        self.assertFalse(trial.exists())
        content = bytearray(healthy)
        content[start + field.start] ^= 4
        content[start + field.start + 1] ^= 1
        source.write_bytes(content)
        with self.assertRaisesRegex(FormatError, "no unique original-checksum"):
            create_chunk_dimension_trial(source, "/science", trial)
        self.assertFalse(trial.exists())

    def test_intact_and_rechecksummed_schema_change_are_not_corrections(self) -> None:
        source, trial, address, _ = self._create()
        with self.assertRaisesRegex(UnsupportedCase, "checksum is intact"):
            create_chunk_dimension_trial(source, "/science", trial)
        start, raw, field, _rank, _width = self._dimension_range(source, address)
        changed = bytearray(source.read_bytes())
        changed[start + field.start] ^= 4
        end = start + len(raw)
        changed[end - 4:end] = lookup3(changed[start:end - 4]).to_bytes(4, "little")
        source.write_bytes(changed)
        with self.assertRaisesRegex(UnsupportedCase, "checksum is intact"):
            create_chunk_dimension_trial(source, "/science", trial)
        self.assertFalse(trial.exists())

    def test_missing_root_anchor_and_wrong_path_refuse(self) -> None:
        source, trial, address, _ = self._create()
        damaged, _, _ = self._damage_dimension(source, address)
        with self.assertRaisesRegex(FormatError, "selected name"):
            create_chunk_dimension_trial(source, "/not-science", trial)
        with self.assertRaisesRegex(UnsupportedCase, "direct checked root"):
            create_chunk_dimension_trial(source, "/parent/science", trial)
        self.assertEqual(source.read_bytes(), damaged)
        self.assertFalse(trial.exists())

    def test_wrong_field_and_second_header_fault_refuse(self) -> None:
        source, trial, address, _ = self._create()
        start, raw, field, _rank, width = self._dimension_range(source, address)
        healthy = source.read_bytes()
        # The final raw dimension denotes datatype size, outside the declared trial field.
        content = bytearray(healthy)
        content[start + field.stop] ^= 4
        source.write_bytes(content)
        with self.assertRaisesRegex(FormatError, "no unique original-checksum"):
            create_chunk_dimension_trial(source, "/science", trial)
        content = bytearray(healthy)
        content[start + field.start] ^= 4
        content[start + field.stop + width] ^= 1  # The index-kind discriminator.
        source.write_bytes(content)
        with self.assertRaises(UnsupportedFormat):
            create_chunk_dimension_trial(source, "/science", trial)
        self.assertFalse(trial.exists())


if __name__ == "__main__":
    unittest.main()
