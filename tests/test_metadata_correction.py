"""Checksum-preserving modern metadata trials never mutate their source."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.format import FormatError
from h5reclaim.metadata import UnsupportedCase, read_dataset_spec
from h5reclaim.metadata_correction import (
    _unique_one_byte, create_layout_pointer_trial, create_root_address_trial,
)
from h5reclaim.modern_indexes import ModernH5File, lookup3


def _layout_pointer(raw: bytearray, address: int) -> int:
    flags = raw[address+5]
    width = 1 << (flags & 3)
    first = address+6+(16 if flags & 0x20 else 0)+(4 if flags & 0x10 else 0)+width
    content_size = int.from_bytes(raw[first-width:first], "little")
    cursor = first
    found: list[int] = []
    while cursor < first+content_size:
        kind, length = raw[cursor], int.from_bytes(raw[cursor+1:cursor+3], "little")
        cursor += 4
        if kind == 8:
            found.append(cursor+length-8)
        cursor += length
    assert len(found) == 1
    return found[0]


class ModernMetadataCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.source = self.directory / "damaged.h5"
        self.trial = self.directory / "trial.h5"
        self.values = np.arange(32, dtype="<u4")
        with h5py.File(self.source, "w", libver="latest") as handle:
            dataset = handle.create_dataset("science", data=self.values, chunks=(8,))
            self.selected_address = int(h5py.h5o.get_info(dataset.id).addr)
        raw = self.source.read_bytes()
        self.healthy = raw
        self.assertEqual(raw[8], 3)
        self.assertEqual(int.from_bytes(raw[36:44], "little"), 48)

    def _mutate(self, *offsets: int, rewrite_super_checksum: bool = False) -> bytes:
        raw = bytearray(self.source.read_bytes())
        for offset in offsets:
            raw[offset] ^= 4
        if rewrite_super_checksum:
            raw[44:48] = lookup3(raw[:44]).to_bytes(4, "little")
        self.source.write_bytes(raw)
        return bytes(raw)

    def test_root_address_one_byte_restores_original_checksum_only_on_private_trial(self) -> None:
        damaged = self._mutate(36)
        expected_hash = hashlib.sha256(damaged).hexdigest()
        with self.assertRaises(OSError):
            with h5py.File(self.source, "r"):
                pass
        proposed = create_root_address_trial(self.source, "/science", self.trial)
        self.assertEqual(self.source.read_bytes(), damaged)
        self.assertEqual(proposed.source_sha256, expected_hash)
        self.assertEqual(proposed.kind, "superblock_root")
        self.assertEqual((proposed.physical_offset, proposed.before_byte, proposed.after_byte),
                         (36, damaged[36], 48))
        self.assertEqual((proposed.changed_bytes, proposed.checksum_bytes_changed), (1, 0))
        self.assertFalse(proposed.historical_payload_verified)
        self.assertEqual(self.trial.read_bytes()[44:48], damaged[44:48])
        with h5py.File(self.trial, "r") as handle:
            np.testing.assert_array_equal(handle["science"][:], self.values)

    def test_selected_header_index_pointer_restore_from_rooted_compact_link(self) -> None:
        raw = bytearray(self.source.read_bytes())
        pointer = _layout_pointer(raw, self.selected_address)
        original_pointer = int.from_bytes(raw[pointer:pointer+8], "little")
        damaged = self._mutate(pointer)
        proposed = create_layout_pointer_trial(self.source, "/science", self.trial)
        self.assertEqual(proposed.kind, "selected_layout_pointer")
        self.assertEqual(proposed.pointer_after, original_pointer)
        self.assertEqual(proposed.rooted_object_address, self.selected_address)
        self.assertEqual(proposed.physical_offset, pointer)
        self.assertEqual(self.source.read_bytes(), damaged)
        self.assertEqual(self.trial.read_bytes()[-1], damaged[-1])
        with h5py.File(self.trial, "r") as handle:
            np.testing.assert_array_equal(handle["science"][:], self.values)

    def test_selected_index_pointer_in_extensible_array_and_v2_btree(self) -> None:
        for shape, maxshape, chunks, expected_kind in (
            ((32,), (None,), (8,), 4),
            ((16, 16), (None, None), (4, 4), 5),
        ):
            with self.subTest(index_type=expected_kind):
                source = self.directory / f"kind-{expected_kind}.h5"
                trial = self.directory / f"kind-{expected_kind}.trial.h5"
                expected = np.arange(np.prod(shape), dtype="<u4").reshape(shape)
                with h5py.File(source, "w", libver="latest") as handle:
                    selected = handle.create_dataset("science", data=expected,
                                                     chunks=chunks, maxshape=maxshape)
                    address = int(h5py.h5o.get_info(selected.id).addr)
                raw = bytearray(source.read_bytes())
                pointer = _layout_pointer(raw, address)
                spec = read_dataset_spec(source, "/science")
                with ModernH5File(source) as reader:
                    family = reader.read_index(spec.object_address, spec.shape, spec.chunks,
                                               np.dtype(spec.dtype).itemsize, maxshape=spec.maxshape,
                                               filters=spec.filters).index_type
                self.assertEqual(family, {4: "extensible_array", 5: "v2_btree"}[expected_kind])
                raw[pointer] ^= 4
                source.write_bytes(raw)
                proposed = create_layout_pointer_trial(source, "/science", trial)
                self.assertEqual(proposed.physical_offset, pointer)
                with h5py.File(trial, "r") as handle:
                    np.testing.assert_array_equal(handle["science"][:], expected)

    def test_checksum_only_and_two_faults_never_publish_trial(self) -> None:
        raw = bytearray(self.source.read_bytes())
        raw[44] ^= 4
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "no unique original-checksum"):
            create_root_address_trial(self.source, "/science", self.trial)
        self.assertFalse(self.trial.exists())
        # Two distinct bytes in the permitted root field need two corrections.
        self.source.write_bytes(self.healthy)
        self._mutate(36, 37)
        with self.assertRaisesRegex(FormatError, "no unique original-checksum"):
            create_root_address_trial(self.source, "/science", self.trial)
        self.assertFalse(self.trial.exists())

    def test_rechecksummed_redirect_cannot_be_called_a_correction(self) -> None:
        raw = bytearray(self.source.read_bytes())
        raw[36:44] = self.selected_address.to_bytes(8, "little")
        raw[44:48] = lookup3(raw[:44]).to_bytes(4, "little")
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(UnsupportedCase, "checksum is intact"):
            create_root_address_trial(self.source, "/science", self.trial)
        self.assertFalse(self.trial.exists())

    def test_selected_header_refuses_wrong_path_and_rechecksummed_redirect(self) -> None:
        raw = bytearray(self.source.read_bytes())
        pointer = _layout_pointer(raw, self.selected_address)
        self._mutate(pointer)
        with self.assertRaisesRegex(FormatError, "selected name"):
            create_layout_pointer_trial(self.source, "/other", self.trial)
        self.assertFalse(self.trial.exists())
        raw = bytearray(self.source.read_bytes())
        raw[pointer] ^= 8
        flags = raw[self.selected_address+5]
        width = 1 << (flags & 3)
        size = int.from_bytes(raw[self.selected_address+6:self.selected_address+6+width], "little")
        end = self.selected_address+6+width+size+4
        raw[end-4:end] = lookup3(raw[self.selected_address:end-4]).to_bytes(4, "little")
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(UnsupportedCase, "checksum is intact"):
            create_layout_pointer_trial(self.source, "/science", self.trial)
        self.assertFalse(self.trial.exists())

    def test_selected_header_checksum_only_and_two_byte_pointer_refuse(self) -> None:
        raw = bytearray(self.healthy)
        pointer = _layout_pointer(raw, self.selected_address)
        flags = raw[self.selected_address+5]
        width = 1 << (flags & 3)
        size = int.from_bytes(raw[self.selected_address+6:self.selected_address+6+width], "little")
        checksum = self.selected_address+6+width+size
        raw[checksum] ^= 4
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "no unique original-checksum"):
            create_layout_pointer_trial(self.source, "/science", self.trial)
        self.assertFalse(self.trial.exists())
        raw = bytearray(self.healthy)
        raw[pointer] ^= 4
        raw[pointer+1] ^= 1
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "no unique original-checksum"):
            create_layout_pointer_trial(self.source, "/science", self.trial)
        self.assertFalse(self.trial.exists())

    def test_ambiguous_checksum_candidates_are_not_selected_by_structure(self) -> None:
        with patch("h5reclaim.metadata_correction.lookup3", return_value=7):
            with self.assertRaisesRegex(FormatError, "ambiguous"):
                _unique_one_byte(b"\x00\x00\x00\x00\x00", range(0, 1), 7)


if __name__ == "__main__":
    unittest.main()
