"""Committed datatype and SOHM list fixtures from HDF5, plus corrupt pointers."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError
from h5reclaim.metadata_fallback import _messages, read_dataset_spec_fallback
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.recovery import recover


def _patch_header(path: Path, address: int, offset: int, replacement: bytes) -> None:
    raw = bytearray(path.read_bytes())
    flags = raw[address + 5]
    width = 1 << (flags & 3)
    extra = (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0)
    content = address + 6 + extra + width
    end = content + int.from_bytes(raw[content-width:content], "little")
    assert content <= offset and offset + len(replacement) <= end
    raw[offset:offset+len(replacement)] = replacement
    raw[end:end+4] = lookup3(raw[address:end]).to_bytes(4, "little")
    path.write_bytes(raw)


def _patch_checked_block(path: Path, address: int, size: int,
                         offset: int, replacement: bytes) -> None:
    raw = bytearray(path.read_bytes())
    assert address <= offset and offset + len(replacement) <= address + size - 4
    raw[offset:offset + len(replacement)] = replacement
    raw[address + size - 4:address + size] = lookup3(raw[address:address+size-4]).to_bytes(4, "little")
    path.write_bytes(raw)


class SharedMessageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "shared.h5"

    def _committed(self):
        truth = np.arange(8, dtype="<i4")
        with h5py.File(self.path, "w", libver="latest") as file:
            dtype = h5py.h5t.py_create(truth.dtype)
            dtype.commit(file.id, b"measurement_type")
            space = h5py.h5s.create_simple((8,))
            dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            dcpl.set_chunk((8,))
            dataset = h5py.h5d.create(file.id, b"science", dtype, space, dcpl=dcpl)
            dataset.write(h5py.h5s.ALL, h5py.h5s.ALL, truth)
            return truth, h5py.h5o.get_info(dataset).addr, h5py.h5o.get_info(dtype).addr

    def _sohm(self):
        fixture = Path(__file__).parent / "fixtures" / "sohm_shared_dataspace.h5"
        source = fixture.read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(),
                         "0ab54ef33d52617630ce8c6e28128d2e0eb3723c81251e664cf2ca4c0ecbbd47")
        self.path.write_bytes(source)
        truth = np.arange(8, dtype="<u4")
        with h5py.File(self.path, "r") as handle:
            address = h5py.h5o.get_info(handle["d1"].id).addr
        with ModernH5File(self.path) as reader:
            selected = next(m for m in _messages(reader, address) if m.kind == 1)
            self.assertTrue(selected.flags & 2)
            self.assertEqual(selected.data[:2], b"\x03\x01")
        return truth, address, selected

    def test_committed_numeric_dtype_is_resolved_from_checked_target(self):
        truth, dataset_address, type_address = self._committed()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with ModernH5File(self.path) as reader:
            dtype = next(m for m in _messages(reader, dataset_address) if m.kind == 3)
            self.assertTrue(dtype.flags & 2)
            self.assertEqual(int.from_bytes(dtype.data[2:], "little"), type_address)
        result = read_dataset_spec_fallback(self.path, "/science", expected_sha256=before)
        self.assertEqual(result.spec.dtype, "<i4")
        self.assertEqual(result.spec.shape, truth.shape)
        self.assertIn("with_shared_schema", result.route)
        self.assertTrue(any("committed_datatype" in description
                            for description in result.resolved_shared_messages))
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_committed_reference_to_wrong_object_refuses(self):
        _, dataset_address, _ = self._committed()
        with ModernH5File(self.path) as reader:
            message = next(m for m in _messages(reader, dataset_address) if m.kind == 3)
            root = reader.superblock.root_object_address
        _patch_header(self.path, dataset_address, message.absolute_offset + 2,
                      root.to_bytes(8, "little"))
        with self.assertRaisesRegex(FormatError, "group or dataset object"):
            read_dataset_spec_fallback(self.path, "/science")

    def test_committed_reference_to_valid_dataset_is_not_a_datatype_object(self):
        _, selected_address, _ = self._committed()
        with h5py.File(self.path, "r+", libver="latest") as handle:
            target = handle.create_dataset("decoy", data=np.arange(8, dtype="<i4"),
                                           chunks=(8,))
            target_address = h5py.h5o.get_info(target.id).addr
        with ModernH5File(self.path) as reader:
            shared = next(m for m in _messages(reader, selected_address) if m.kind == 3)
        _patch_header(self.path, selected_address, shared.absolute_offset + 2,
                      target_address.to_bytes(8, "little"))
        with self.assertRaisesRegex(FormatError, "group or dataset object"):
            read_dataset_spec_fallback(self.path, "/science")

    def test_committed_target_checksum_contradiction_refuses(self):
        _, _, target = self._committed()
        raw = bytearray(self.path.read_bytes())
        raw[target + 8] ^= 1
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "checksum"):
            read_dataset_spec_fallback(self.path, "/science")

    def test_sohm_list_and_managed_heap_resolve_dataspace(self):
        truth, address, _ = self._sohm()
        before = self.path.read_bytes()
        result = read_dataset_spec_fallback(self.path, "/d1")
        self.assertEqual(result.spec.object_address, address)
        self.assertEqual(result.spec.shape, truth.shape)
        self.assertIn("with_shared_schema", result.route)
        self.assertTrue(any("SOHM master table" in kind for _, _, kind in result.metadata_ranges))
        self.assertTrue(any("SOHM record list" in kind for _, _, kind in result.metadata_ranges))
        self.assertTrue(any("SOHM FHDB" in kind for _, _, kind in result.metadata_ranges))
        self.assertEqual(self.path.read_bytes(), before)

    def test_sohm_list_and_managed_heap_resolve_datatype(self):
        fixture = Path(__file__).parent / "fixtures" / "sohm_shared_datatype.h5"
        source = fixture.read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(),
                         "59bc1829a4ab4958a65e0175d016f4d6a8665dc67a95661a378dd96d139f7f21")
        self.path.write_bytes(source)
        with ModernH5File(self.path) as reader, h5py.File(self.path, "r") as file:
            address = h5py.h5o.get_info(file["d1"].id).addr
            datatype = next(m for m in _messages(reader, address) if m.kind == 3)
            self.assertTrue(datatype.flags & 2)
            self.assertEqual(datatype.data[:2], b"\x03\x01")
        result = read_dataset_spec_fallback(self.path, "/d1")
        self.assertEqual(result.spec.dtype, "<u4")
        self.assertEqual(result.spec.shape, (128,))
        self.assertEqual(result.spec.filters, (2, 1, 3))
        self.assertTrue(any("type 3: checksummed_sohm_list_managed_heap" in item
                            for item in result.resolved_shared_messages))
        self.assertEqual(self.path.read_bytes(), source)

    def test_sohm_list_and_managed_heap_resolve_filter_pipeline(self):
        fixture = Path(__file__).parent / "fixtures" / "sohm_shared_filter_pipeline.h5"
        source = fixture.read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(),
                         "327e3f17fc47337df48da06611d5f8923e3597d03bdbe4148ac3ee9d5fa9a87c")
        self.path.write_bytes(source)
        with ModernH5File(self.path) as reader, h5py.File(self.path, "r") as file:
            address = h5py.h5o.get_info(file["d1"].id).addr
            pipeline = next(m for m in _messages(reader, address) if m.kind == 11)
            self.assertTrue(pipeline.flags & 2)
        result = read_dataset_spec_fallback(self.path, "/d1")
        self.assertEqual(result.spec.shape, (128,))
        self.assertEqual(result.spec.filters, (2, 1, 3))
        self.assertTrue(any("type 11: checksummed_sohm_list_managed_heap" in item
                            for item in result.resolved_shared_messages))
        self.assertEqual(self.path.read_bytes(), source)

    def test_sohm_schema_recovers_values_from_copy_native_cannot_open(self):
        truth, address, _ = self._sohm()
        with ModernH5File(self.path) as reader:
            fill = next(m for m in _messages(reader, address) if m.kind == 5)
        _patch_header(self.path, address, fill.absolute_offset, b"\xff")
        damaged = self.path.read_bytes()
        with h5py.File(self.path, "r") as handle:
            with self.assertRaises((KeyError, OSError)):
                _ = handle["d1"]
        output = self.path.with_name("recovered.h5")
        report = recover(self.path, "/d1", output,
                         self.path.with_name("recovered.json"))
        self.assertEqual(report["counts"]["recovered"], 1)
        with h5py.File(output, "r") as result:
            np.testing.assert_array_equal(result["d1"][:], truth)
        self.assertEqual(self.path.read_bytes(), damaged)

    def _sohm_locations(self):
        self._sohm()
        raw = self.path.read_bytes()
        return {signature: raw.index(signature) for signature in (b"SMTB", b"SMLI", b"FHDB")}

    def test_sohm_master_table_checksum_refuses(self):
        location = self._sohm_locations()[b"SMTB"]
        raw = bytearray(self.path.read_bytes())
        raw[location + 10] ^= 1
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "master-table signature or checksum"):
            read_dataset_spec_fallback(self.path, "/d1")

    def test_sohm_recomputed_list_with_wrong_heap_id_refuses(self):
        location = self._sohm_locations()[b"SMLI"]
        # One indexed shared dataspace. Recompute SMLI checksum to test the
        # pointer relationship, instead of relying on a checksum mismatch.
        _patch_checked_block(self.path, location, 25, location + 4 + 9 + 1, b"\xfe")
        with self.assertRaisesRegex(FormatError, "no unique index owner"):
            read_dataset_spec_fallback(self.path, "/d1")

    def test_sohm_direct_heap_corruption_refuses(self):
        location = self._sohm_locations()[b"FHDB"]
        raw = bytearray(self.path.read_bytes())
        raw[location + 10] ^= 1
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "FHDB"):
            read_dataset_spec_fallback(self.path, "/d1")

    def test_sohm_unknown_heap_id_class_refuses(self):
        _, address, message = self._sohm()
        self.assertEqual(message.data[2], 0)
        _patch_header(self.path, address, message.absolute_offset + 2, b"\x80")
        with self.assertRaisesRegex(FormatError, "no unique index owner"):
            read_dataset_spec_fallback(self.path, "/d1")

    def _btree_fixture(self, internal: bool):
        name = "sohm_btree_internal.h5" if internal else "sohm_btree_leaf.h5"
        expected_hash = (
            "18d2197ba52c9c3037ed73689533a4cd4fb406622fced16b552f2a4bcccf56ea"
            if internal else
            "bf3a44df4aa1f001e0002c5a9ad0fdc7512b6d56f65cf58d5ba3bf9649ac1e59"
        )
        source = (Path(__file__).parent / "fixtures" / name).read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(), expected_hash)
        self.path.write_bytes(source)
        return source

    def test_sohm_btree_leaf_and_internal_nodes_resolve_owned_dataspace(self):
        for internal in (False, True):
            with self.subTest(internal=internal):
                source = self._btree_fixture(internal)
                record = read_dataset_spec_fallback(self.path, "/shape_10_1")
                self.assertEqual(record.spec.shape, (10,))
                self.assertEqual(record.spec.dtype, "<u4")
                self.assertEqual(record.spec.chunks, (10,))
                self.assertTrue(any("checksummed_sohm_btree_managed_heap" in item
                                    for item in record.resolved_shared_messages))
                nodes = [item for item in record.metadata_ranges
                         if item[2] == "SOHM B-tree node"]
                self.assertGreaterEqual(len(nodes), 2 if internal else 1)
                if internal:
                    # This dataset's heap ID is stored in the BTIN root
                    # record, rather than in either child leaf.
                    separator = read_dataset_spec_fallback(self.path, "/shape_21_1")
                    self.assertEqual(separator.spec.shape, (21,))
                    self.assertTrue(any("sohm_btree" in item
                                        for item in separator.resolved_shared_messages))
                self.assertEqual(self.path.read_bytes(), source)

    def test_sohm_internal_repeated_child_with_new_checksum_refuses(self):
        self._btree_fixture(internal=True)
        raw = bytearray(self.path.read_bytes())
        table = raw.index(b"SMTB")
        index_address = int.from_bytes(raw[table+18:table+26], "little")
        self.assertEqual(raw[index_address:index_address+4], b"BTHD")
        self.assertEqual(int.from_bytes(raw[index_address+12:index_address+14], "little"), 1)
        root = int.from_bytes(raw[index_address+16:index_address+24], "little")
        root_count = int.from_bytes(raw[index_address+24:index_address+26], "little")
        node_size = int.from_bytes(raw[index_address+6:index_address+10], "little")
        self.assertEqual(raw[root:root+4], b"BTIN")
        first = root + 6 + root_count*17
        second = first + 9  # 8-byte address and one-byte child count.
        raw[first:first+9] = raw[second:second+9]
        checksum = root + 6 + root_count*17 + (root_count+1)*9
        raw[checksum:checksum+4] = lookup3(raw[root:checksum]).to_bytes(4, "little")
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "repeated"):
            read_dataset_spec_fallback(self.path, "/shape_10_1")

    def test_sohm_btree_leaf_checksum_corruption_refuses(self):
        self._btree_fixture(internal=False)
        raw = bytearray(self.path.read_bytes())
        leaf = raw.index(b"BTLF", raw.index(b"SMTB"))
        raw[leaf+10] ^= 1
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "SOHM B-tree node checksum"):
            read_dataset_spec_fallback(self.path, "/shape_10_1")

    def test_sohm_btree_reordered_records_with_new_checksum_refuses(self):
        self._btree_fixture(internal=False)
        raw = bytearray(self.path.read_bytes())
        table = raw.index(b"SMTB")
        index_address = int.from_bytes(raw[table+18:table+26], "little")
        leaf = int.from_bytes(raw[index_address+16:index_address+24], "little")
        count = int.from_bytes(raw[index_address+24:index_address+26], "little")
        self.assertEqual(raw[leaf:leaf+4], b"BTLF")
        first = bytes(raw[leaf+6:leaf+23])
        second = bytes(raw[leaf+23:leaf+40])
        raw[leaf+6:leaf+23] = second
        raw[leaf+23:leaf+40] = first
        checksum = leaf + 6 + count*17
        raw[checksum:checksum+4] = lookup3(raw[leaf:checksum]).to_bytes(4, "little")
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "key order"):
            read_dataset_spec_fallback(self.path, "/shape_10_1")

    def test_sohm_btree_internal_recovers_from_damaged_native_open(self):
        self._btree_fixture(internal=True)
        with ModernH5File(self.path) as reader, h5py.File(self.path, "r") as native:
            address = h5py.h5o.get_info(native["shape_10_1"].id).addr
            fill = next(m for m in _messages(reader, address) if m.kind == 5)
        _patch_header(self.path, address, fill.absolute_offset, b"\xff")
        damaged = self.path.read_bytes()
        with h5py.File(self.path, "r") as native:
            with self.assertRaises((KeyError, OSError)):
                _ = native["shape_10_1"]
        report = recover(self.path, "/shape_10_1", self.path.with_name("out.h5"),
                         self.path.with_name("out.json"))
        self.assertEqual(report["counts"]["recovered"], 1)
        self.assertTrue(any("sohm_btree" in item for item in
                            report["metadata_resolution"]["resolved_shared_messages"]))
        with h5py.File(self.path.with_name("out.h5"), "r") as result:
            np.testing.assert_array_equal(result["shape_10_1"][:],
                                          np.arange(10, dtype="<u4"))
        self.assertEqual(self.path.read_bytes(), damaged)
