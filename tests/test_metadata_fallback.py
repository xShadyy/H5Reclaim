"""Rooted path attribution and damaged-native-open metadata fallback."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import h5py
import numpy as np

from h5reclaim.format import FormatError, UnsupportedFormat
from h5reclaim.metadata_fallback import _messages, _old_messages, read_dataset_spec_fallback
from h5reclaim.format import H5File
from h5reclaim.modern_indexes import ModernH5File, lookup3


def _patch_header(path: Path, object_address: int, byte_offset: int, replacement: int) -> None:
    raw = bytearray(path.read_bytes())
    flags = raw[object_address + 5]
    width = 1 << (flags & 3)
    extra = (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0)
    content_start = object_address + 6 + extra + width
    count = int.from_bytes(raw[content_start - width:content_start], "little")
    checksum = content_start + count
    assert object_address <= byte_offset < checksum
    raw[byte_offset] = replacement
    raw[checksum:checksum + 4] = lookup3(raw[object_address:checksum]).to_bytes(4, "little")
    path.write_bytes(raw)


class MetadataFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "science.h5"

    def _create_rank_two(self) -> tuple[np.ndarray, int, int]:
        truth = np.arange(16, dtype="<u4").reshape(4, 4)
        with h5py.File(self.path, "w", libver="latest") as handle:
            group = handle.create_group("lab")
            selected = group.create_dataset("science", data=truth, chunks=(4, 4))
            group.create_dataset("distractor", data=np.full((4, 4), 99, dtype="<u4"),
                                 chunks=(4, 4))
            return truth, h5py.h5o.get_info(selected.id).addr, h5py.h5o.get_info(group.id).addr

    def test_nested_hard_links_anchor_exact_source_and_coordinates(self):
        truth, address, _ = self._create_rank_two()
        original = self.path.read_bytes()
        record = read_dataset_spec_fallback(
            self.path, "/lab/science", expected_sha256=hashlib.sha256(original).hexdigest())
        self.assertEqual(record.spec.object_address, address)
        self.assertEqual(record.spec.shape, truth.shape)
        self.assertEqual(record.spec.chunks, truth.shape)
        self.assertEqual([step.name for step in record.link_chain], ["lab", "science"])
        self.assertEqual(record.source_sha256, hashlib.sha256(original).hexdigest())
        with ModernH5File(self.path) as reader:
            index = reader.read_index(record.spec.object_address, record.spec.shape,
                                      record.spec.chunks, 4, maxshape=record.spec.maxshape)
            self.assertEqual(reader.read_at(index.chunks[0].address, index.chunks[0].size),
                             truth.tobytes())
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_optional_fill_causes_native_open_failure_but_rooted_data_survives(self):
        truth, address, _ = self._create_rank_two()
        with ModernH5File(self.path) as reader:
            fill = [m for m in _messages(reader, address) if m.kind == 5]
            self.assertEqual(len(fill), 1)
        # Current HDF5 rejects version 255 of the fill-value message even
        # though the allocated selected chunk and mandatory schema survive.
        _patch_header(self.path, address, fill[0].absolute_offset, 255)
        with h5py.File(self.path, "r") as handle:
            with self.assertRaises((KeyError, OSError)):
                _ = handle["/lab/science"]
        damaged = self.path.read_bytes()
        fallback = read_dataset_spec_fallback(self.path, "/lab/science")
        self.assertTrue(any("auxiliary message type 5" in m
                            for m in fallback.omitted_auxiliary_metadata))
        with ModernH5File(self.path) as reader:
            index = reader.read_index(fallback.spec.object_address, fallback.spec.shape,
                                      fallback.spec.chunks, 4, maxshape=fallback.spec.maxshape)
            self.assertEqual(len(index.chunks), 1)
            self.assertEqual(reader.read_at(index.chunks[0].address, index.chunks[0].size),
                             truth.tobytes())
        self.assertEqual(self.path.read_bytes(), damaged)

    def test_rooted_link_name_and_source_hash_are_required(self):
        self._create_rank_two()
        with self.assertRaisesRegex(UnsupportedFormat, "no verified compact hard link"):
            read_dataset_spec_fallback(self.path, "/lab/unknown")
        with self.assertRaisesRegex(FormatError, "SHA-256 disagrees"):
            read_dataset_spec_fallback(self.path, "/lab/science", expected_sha256="0" * 64)

    def test_unselected_soft_link_is_skipped_but_selected_soft_link_refused(self):
        self._create_rank_two()
        with h5py.File(self.path, "r+") as handle:
            handle["lab/alias"] = h5py.SoftLink("/lab/science")
        record = read_dataset_spec_fallback(self.path, "/lab/science")
        self.assertEqual(record.spec.path, "/lab/science")
        with self.assertRaisesRegex(UnsupportedFormat, "not a local hard link"):
            read_dataset_spec_fallback(self.path, "/lab/alias")

    def test_duplicate_link_name_is_contradiction_even_with_valid_group_checksum(self):
        self._create_rank_two()
        with h5py.File(self.path, "r+") as handle:
            handle["lab/sciencf"] = handle["lab/science"]
        with ModernH5File(self.path) as reader:
            group = reader.superblock.root_object_address
            group = int.from_bytes([m for m in _messages(reader, group)
                                    if m.kind == 6 and b"lab" in m.data][0].data[-8:], "little")
            other = [m for m in _messages(reader, group)
                     if m.kind == 6 and b"sciencf" in m.data][0]
        # bytes: version, flags, one-byte length, seven-byte name.
        index = other.absolute_offset + 3 + len("scienc")
        _patch_header(self.path, group, index, ord("e"))
        with self.assertRaisesRegex(FormatError, "duplicate compact group link name"):
            read_dataset_spec_fallback(self.path, "/lab/science")

    def test_selected_object_checksum_mutation_refuses(self):
        _, address, _ = self._create_rank_two()
        raw = bytearray(self.path.read_bytes())
        raw[address + 23] ^= 1
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "object-header checksum"):
            read_dataset_spec_fallback(self.path, "/lab/science")

    def test_modern_wrong_hard_link_to_another_valid_object_refuses(self):
        _, address, group_address = self._create_rank_two()
        with h5py.File(self.path, "r") as handle:
            distractor_address = h5py.h5o.get_info(handle["/lab/distractor"].id).addr
        with ModernH5File(self.path) as reader:
            link = [m for m in _messages(reader, group_address)
                    if m.kind == 6 and b"science" in m.data][0]
        for i, octet in enumerate(distractor_address.to_bytes(8, "little")):
            _patch_header(self.path, group_address, link.absolute_offset + len(link.data) - 8 + i,
                          octet)
        self.assertNotEqual(address, distractor_address)
        with self.assertRaisesRegex(FormatError, "hard-link count contradicts"):
            read_dataset_spec_fallback(self.path, "/lab/science")

    def test_valid_multiple_hard_links_in_one_modern_group(self):
        self._create_rank_two()
        with h5py.File(self.path, "r+") as handle:
            handle["lab/alias"] = handle["lab/science"]
        a = read_dataset_spec_fallback(self.path, "/lab/science")
        b = read_dataset_spec_fallback(self.path, "/lab/alias")
        self.assertEqual(a.spec.object_address, b.spec.object_address)

    def test_source_changed_during_fallback_is_refused(self):
        self._create_rank_two()
        actual = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with mock.patch("h5reclaim.metadata_fallback._hash_bounded_source",
                        side_effect=[actual, "0" * 64]):
            with self.assertRaisesRegex(FormatError, "source changed"):
                read_dataset_spec_fallback(self.path, "/lab/science")

    def test_dense_group_refuses_instead_of_scanning_for_matching_name(self):
        self._create_rank_two()
        with h5py.File(self.path, "r+") as handle:
            for i in range(20):
                handle["lab"].create_dataset(f"other_{i}", data=np.ones((2, 2), dtype="<u4"))
        with self.assertRaisesRegex(UnsupportedFormat, "dense group"):
            read_dataset_spec_fallback(self.path, "/lab/science")

    def test_rank_one_declared_filter_order_is_observed(self):
        fapl = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
        fapl.set_libver_bounds(h5py.h5f.LIBVER_LATEST, h5py.h5f.LIBVER_LATEST)
        handle = h5py.h5f.create(bytes(self.path), h5py.h5f.ACC_TRUNC, fapl=fapl)
        creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        creation.set_chunk((32,))
        creation.set_fletcher32()
        creation.set_deflate(4)
        dataspace = h5py.h5s.create_simple((32,))
        dtype = h5py.h5t.py_create(np.dtype("<f8"))
        selected = h5py.h5d.create(handle, b"signal", dtype, dataspace, dcpl=creation)
        selected.write(h5py.h5s.ALL, h5py.h5s.ALL, np.arange(32, dtype="<f8"))
        selected.close(); handle.close()
        record = read_dataset_spec_fallback(self.path, "/signal")
        self.assertEqual(record.spec.filters, (3, 1))
        self.assertEqual([x.values for x in record.spec.filter_pipeline], [(), (4,)])
        self.assertEqual(record.spec.maxshape, (32,))

    def test_modern_canonical_numeric_type_width_sign_and_endian_are_preserved(self):
        datatypes = ("u1", "<u2", ">u2", "<i4", ">i8", "<f4", ">f4", "<f8", ">f8")
        for n, dtype in enumerate(datatypes):
            path = self.path.with_name(f"science_{n}.h5")
            with h5py.File(path, "w", libver="latest") as handle:
                handle.create_dataset("science", data=np.arange(8, dtype=dtype), chunks=(8,))
            record = read_dataset_spec_fallback(path, "/science")
            self.assertEqual(np.dtype(record.spec.dtype), np.dtype(dtype))
            self.assertEqual(record.spec.shape, (8,))

    def test_modern_filter_order_and_shuffle_width_are_observed(self):
        with h5py.File(self.path, "w", libver="latest") as handle:
            handle.create_dataset("science", data=np.arange(16, dtype="<i4"),
                                  chunks=(16,), shuffle=True, compression="gzip",
                                  fletcher32=True)
        record = read_dataset_spec_fallback(self.path, "/science")
        self.assertEqual(record.spec.filters, (2, 1, 3))
        self.assertEqual(record.spec.filter_pipeline[0].values, (4,))

    def test_modern_growing_edge_and_two_dynamic_index_families(self):
        extensible = self.path.with_name("extensible.h5")
        with h5py.File(extensible, "w", libver="latest") as handle:
            dataset = handle.create_dataset("growth", shape=(23,), maxshape=(None,),
                                            chunks=(4,), dtype="<i2")
            dataset[:] = np.arange(23, dtype="<i2")
        ea = read_dataset_spec_fallback(extensible, "/growth")
        self.assertEqual((ea.spec.shape, ea.spec.chunks, ea.spec.maxshape),
                         ((23,), (4,), (None,)))
        two = self.path.with_name("dynamic_two_axis.h5")
        with h5py.File(two, "w", libver="latest") as handle:
            dataset = handle.create_dataset("growth", shape=(12, 15), maxshape=(None, None),
                                            chunks=(3, 5), dtype="<u2", compression="gzip")
            dataset[:] = np.arange(180, dtype="<u2").reshape(12, 15)
        btree = read_dataset_spec_fallback(two, "/growth")
        self.assertEqual((btree.spec.shape, btree.spec.maxshape, btree.spec.filters),
                         ((12, 15), (None, None), (1,)))

    def test_older_symbol_table_tree_with_multiple_leaves_resolves_exact_object(self):
        with h5py.File(self.path, "w", libver="earliest") as handle:
            group = handle.create_group("lab")
            for i in range(400):
                group.create_dataset(f"sensor_{i:03}",
                                     data=np.arange(4, dtype="<u4").reshape(2, 2) + i,
                                     chunks=(2, 2))
            address = h5py.h5o.get_info(group["sensor_300"].id).addr
        record = read_dataset_spec_fallback(self.path, "/lab/sensor_300")
        self.assertEqual(record.spec.object_address, address)
        self.assertIn("unchecksummed", record.route)
        self.assertGreater(sum(kind == "older group symbol node"
                               for _, _, kind in record.metadata_ranges), 5)

    def test_older_corrupt_auxiliary_fill_refuses_native_but_anchors_metadata(self):
        expected = np.arange(16, dtype="<u4").reshape(4, 4)
        with h5py.File(self.path, "w", libver="earliest") as handle:
            selected = handle.create_group("lab").create_dataset("science", data=expected,
                                                                    chunks=(4, 4))
            address = h5py.h5o.get_info(selected.id).addr
        with H5File(self.path) as reader:
            fill = [m for m in _old_messages(reader, address) if m.kind == 5][0]
        damaged = bytearray(self.path.read_bytes())
        damaged[fill.absolute_offset] = 255
        self.path.write_bytes(damaged)
        with h5py.File(self.path, "r") as handle:
            with self.assertRaises((KeyError, OSError)):
                _ = handle["/lab/science"]
        record = read_dataset_spec_fallback(self.path, "/lab/science")
        self.assertEqual(record.spec.object_address, address)
        self.assertEqual(record.spec.shape, expected.shape)
        with H5File(self.path) as reader:
            layout = reader.read_dataset_layout(address, rank=2)
            leaf = reader.read_tree(layout.root_address, rank=2)
            self.assertEqual(len(leaf.entries), 1)
            raw = reader.read_at(leaf.entries[0].address, leaf.entries[0].key.stored_size)
            self.assertEqual(raw, expected.tobytes())
        self.assertEqual(self.path.read_bytes(), damaged)

    def test_older_cached_root_addresses_must_agree_with_group_header(self):
        with h5py.File(self.path, "w", libver="earliest") as handle:
            handle.create_dataset("science", data=np.arange(16, dtype="<u4").reshape(4, 4),
                                  chunks=(4, 4))
        with H5File(self.path) as reader:
            sb = reader.superblock
            entry_start = sb.signature_offset + (28 if sb.version else 24) + 4 * sb.offset_size
            cached_tree_start = entry_start + sb.length_size + sb.offset_size + 8
        raw = bytearray(self.path.read_bytes())
        raw[cached_tree_start] ^= 1
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "cached group addresses contradict"):
            read_dataset_spec_fallback(self.path, "/science")

    def test_older_wrong_hard_link_to_another_valid_object_refuses(self):
        with h5py.File(self.path, "w", libver="earliest") as handle:
            group = handle.create_group("lab")
            group.create_dataset("science", data=np.arange(16, dtype="<u4").reshape(4, 4),
                                 chunks=(4, 4))
            distractor = group.create_dataset("distractor", data=np.ones((4, 4), dtype="<u4"),
                                              chunks=(4, 4))
            distractor_address = h5py.h5o.get_info(distractor.id).addr
        baseline = read_dataset_spec_fallback(self.path, "/lab/science")
        step = baseline.link_chain[-1]
        with H5File(self.path) as reader:
            osize, lsize = reader.superblock.offset_size, reader.superblock.length_size
        raw = bytearray(self.path.read_bytes())
        raw[step.link_message_offset + lsize:step.link_message_offset + lsize + osize] = (
            distractor_address.to_bytes(osize, "little"))
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "hard-link count contradicts"):
            read_dataset_spec_fallback(self.path, "/lab/science")


if __name__ == "__main__":
    unittest.main()
