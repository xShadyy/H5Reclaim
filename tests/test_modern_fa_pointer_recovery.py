"""Controlled FAHD-link damage with known truth outside recovery inputs."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.evidence import load_evidence_report
from h5reclaim.format import FormatError
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.recovery import recover


class ModernFixedArrayPointerRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.source = base / "damaged.h5"
        self.output = base / "recovered.h5"
        self.report = base / "evidence.json"

    def _fixture(self, shape: tuple[int, int], *, filtered: bool = False):
        values = np.arange(np.prod(shape), dtype="<u4").reshape(shape)
        with h5py.File(self.source, "w", libver="latest") as handle:
            options = {"compression": "gzip", "fletcher32": True} if filtered else {}
            handle.create_dataset("science", data=values, chunks=(1, 1), **options)
        with h5py.File(self.source) as handle, ModernH5File(self.source) as reader:
            dataset = handle["science"]
            object_address = h5py.h5o.get_info(dataset.id).addr
            filters = tuple(dataset.id.get_create_plist().get_filter(i)[0]
                            for i in range(dataset.id.get_create_plist().get_nfilters()))
            index = reader.read_index(object_address, dataset.shape, dataset.chunks,
                                      dataset.dtype.itemsize, maxshape=dataset.maxshape,
                                      filters=filters)
            self.assertEqual(index.index_type, "fixed_array")
            return values, index, reader.superblock.offset_size, reader.superblock.length_size

    def _damage_pointer(self, index) -> str:
        raw = bytearray(self.source.read_bytes())
        raw[index.data_block_pointer_offset] ^= 0x67
        self.source.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def test_exact_filtered_chunks_after_one_broken_header_pointer(self) -> None:
        values, index, _, _ = self._fixture((4, 4), filtered=True)
        damaged_hash = self._damage_pointer(index)
        with self.assertRaises((OSError, RuntimeError, KeyError)):
            with h5py.File(self.source) as handle:
                _ = handle["science"][:]
        result = recover(self.source, "/science", self.output, self.report)
        self.assertEqual(result["operation"], "modern_fixed_array_pointer_recovery")
        self.assertEqual(result["reconstructed_chunks"], 16)
        self.assertEqual(result["counts"]["recovered"], 16)
        self.assertEqual(result["index"]["broken_links"], 1)
        self.assertEqual(len(result["unresolved_links"]), 1)
        self.assertEqual(result["evidence_ledger"]["links"][1]["kind"], "bridged_index")
        self.assertTrue(all(item["route"] == "reconstructed_fa_header_link"
                            for item in result["mappings"]))
        self.assertTrue(all(item["evidence"]["metadata_checksums"]
                            ["fahd_checksum_restored_by_pointer_substitution"]
                            for item in result["mappings"]))
        self.assertEqual(len(load_evidence_report(self.report).proposals), 16)
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle["science"][:], values)
            self.assertTrue(np.all(handle["/_h5reclaim/chunk_status"][:] == 1))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), damaged_hash)

    def test_paged_data_block_recovery_preserves_each_checked_page(self) -> None:
        values, index, _, _ = self._fixture((33, 33))
        self._damage_pointer(index)
        result = recover(self.source, "/science", self.output, self.report)
        self.assertEqual(result["counts"]["recovered"], 1089)
        self.assertEqual(result["reconstructed_chunks"], 1089)
        self.assertTrue(result["mappings"][-1]["evidence"]["computed_page"]
                        ["bitmap_initialized"])
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle["science"][:], values)

    def test_checksum_field_damage_alone_does_not_fake_pointer_recovery(self) -> None:
        _, index, _, _ = self._fixture((4, 4))
        raw = bytearray(self.source.read_bytes())
        header_start = index.base_address
        header_size = index.data_block_pointer_offset - header_start + 8 + 4
        raw[header_start + header_size - 1] ^= 0x80
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "no uniquely verified data-block pointer"):
            recover(self.source, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())

    def test_broken_child_checksum_refuses_to_repair_even_when_header_restores(self) -> None:
        _, index, _, _ = self._fixture((4, 4))
        raw = bytearray(self.source.read_bytes())
        raw[index.data_block_pointer_offset] ^= 0x67
        raw[index.data_block_address + 6] ^= 0x40
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "no uniquely verified data-block pointer"):
            recover(self.source, "/science", self.output, self.report)
        self.assertFalse(self.report.exists())

    def test_rechecksummed_redirect_does_not_trigger_pointer_reconstruction(self) -> None:
        _, index, _, lensize = self._fixture((4, 4))
        raw = bytearray(self.source.read_bytes())
        raw[index.data_block_pointer_offset] ^= 0x67
        header_start = index.base_address
        header_size = 12 + lensize + 8
        raw[header_start + header_size - 4:header_start + header_size] = lookup3(
            raw[header_start:header_start + header_size - 4]).to_bytes(4, "little")
        self.source.write_bytes(raw)
        with self.assertRaises(FormatError):
            recover(self.source, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
