"""Modern index address attribution against independently generated HDF5."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError, UnsupportedFormat
from h5reclaim.modern_indexes import ModernH5File, lookup3


def _modern(path: Path, *, implicit: bool = False, filtered: bool = False) -> None:
    if implicit:
        access = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
        access.set_libver_bounds(h5py.h5f.LIBVER_LATEST, h5py.h5f.LIBVER_LATEST)
        handle = h5py.h5f.create(bytes(path), h5py.h5f.ACC_TRUNC, fapl=access)
        space = h5py.h5s.create_simple((8, 12), (8, 12))
        dtype = h5py.h5t.py_create(np.dtype("<u4"))
        creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        creation.set_chunk((2, 3))
        creation.set_alloc_time(h5py.h5d.ALLOC_TIME_EARLY)
        dataset = h5py.h5d.create(handle, b"science", dtype, space, dcpl=creation)
        dataset.write(h5py.h5s.ALL, h5py.h5s.ALL,
                      np.arange(96, dtype="<u4").reshape(8, 12))
        dataset.close(); handle.close()
    else:
        with h5py.File(path, "w", libver="latest") as handle:
            data = np.arange(16, dtype="<u4").reshape(4, 4)
            handle.create_dataset("science", data=data, chunks=(4, 4),
                                  **({"compression": "gzip"} if filtered else {}))
            handle.create_dataset("distractor", data=np.ones((4, 4), dtype="<u4"))


class ModernIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "science.h5"

    def _check_native(self, *, implicit=False, filtered=False):
        _modern(self.path, implicit=implicit, filtered=filtered)
        with h5py.File(self.path, "r") as handle, ModernH5File(self.path) as reader:
            dataset = handle["science"]
            obj = h5py.h5o.get_info(dataset.id).addr
            pipeline = dataset.id.get_create_plist()
            filters = tuple(pipeline.get_filter(i)[0]
                            for i in range(pipeline.get_nfilters()))
            index = reader.read_index(obj, dataset.shape, dataset.chunks,
                                      dataset.dtype.itemsize, maxshape=dataset.maxshape,
                                      filters=filters)
            self.assertEqual(index.object_address, obj)
            self.assertEqual(index.index_type, "implicit" if implicit else "single_chunk")
            self.assertIn(index.layout_version, (4, 5))
            self.assertEqual(len(index.chunks), dataset.id.get_num_chunks())
            for chunk in index.chunks:
                native = dataset.id.get_chunk_info_by_coord(chunk.coordinate)
                self.assertEqual((chunk.coordinate, chunk.address,
                                  chunk.size, chunk.filter_mask),
                                 (native.chunk_offset, native.byte_offset,
                                  native.size, native.filter_mask))
                self.assertEqual(reader.read_at(chunk.address, chunk.size),
                                 dataset.id.read_direct_chunk(chunk.coordinate)[1])
            self.assertTrue(reader.metadata_ranges)
        return obj

    def test_single_direct_address_matches_native(self):
        self._check_native()

    def test_single_filtered_size_and_mask_match_native(self):
        self._check_native(filtered=True)

    def test_implicit_grid_and_physical_ranges_match_native(self):
        self._check_native(implicit=True)

    def test_userblock_relative_address_conversion(self):
        with h5py.File(self.path, "w", libver="latest", userblock_size=512) as handle:
            handle.create_dataset("science", data=np.arange(16, dtype="<u4").reshape(4, 4),
                                  chunks=(4, 4))
        with h5py.File(self.path, "r") as handle, ModernH5File(self.path) as reader:
            dataset = handle["science"]
            obj = h5py.h5o.get_info(dataset.id).addr
            index = reader.read_index(obj, dataset.shape, dataset.chunks,
                                      dataset.dtype.itemsize, maxshape=dataset.maxshape)
            self.assertEqual(reader.superblock.signature_offset, 512)
            from h5reclaim.native_addresses import chunk_address
            self.assertEqual(reader.absolute(index.chunks[0].address),
                             chunk_address(dataset, dataset.id.get_chunk_info(0).byte_offset))

    def test_checksum_detects_corrupt_superblock(self):
        self._check_native()
        raw = bytearray(self.path.read_bytes())
        raw[15] ^= 0x80
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "superblock checksum"):
            ModernH5File(self.path)

    def test_checksum_detects_corrupt_selected_object_header(self):
        obj = self._check_native()
        raw = bytearray(self.path.read_bytes())
        raw[obj + 30] ^= 0x80
        self.path.write_bytes(raw)
        with ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(FormatError, "object header checksum"):
                reader.read_index(obj, (4, 4), (4, 4), 4,
                                  maxshape=(4, 4))

    def test_nonpaged_fixed_array_entries_match_native(self):
        with h5py.File(self.path, "w", libver="latest") as handle:
            handle.create_dataset("science", data=np.arange(96, dtype="<u4").reshape(8, 12),
                                  chunks=(2, 3))
        with h5py.File(self.path, "r") as handle, ModernH5File(self.path) as reader:
            dataset = handle["science"]
            obj = h5py.h5o.get_info(dataset.id).addr
            index = reader.read_index(obj, (8, 12), (2, 3), 4, maxshape=(8, 12))
            self.assertEqual(index.index_type, "fixed_array")
            self.assertEqual(len(index.chunks), 16)
            self.assertIsNotNone(index.data_block_address)
            self.assertEqual(sum(kind.startswith("fixed-array")
                                 for _, _, kind in reader.metadata_ranges), 2)
            for i, chunk in enumerate(index.chunks):
                native = dataset.id.get_chunk_info(i)
                self.assertEqual((chunk.coordinate, reader.absolute(chunk.address), chunk.size),
                                 (native.chunk_offset, native.byte_offset, native.size))
                self.assertEqual(reader.read_at(chunk.address, chunk.size),
                                 dataset.id.read_direct_chunk(chunk.coordinate)[1])

    def test_fixed_array_data_block_checksum_detects_mutation(self):
        self.test_nonpaged_fixed_array_entries_match_native()
        with h5py.File(self.path, "r") as handle, ModernH5File(self.path) as reader:
            obj = h5py.h5o.get_info(handle["science"].id).addr
            index = reader.read_index(obj, (8, 12), (2, 3), 4, maxshape=(8, 12))
            block = reader.absolute(index.data_block_address)
        raw = bytearray(self.path.read_bytes())
        raw[block + 20] ^= 0x80
        self.path.write_bytes(raw)
        with ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(FormatError, "fixed-array data block checksum"):
                reader.read_index(obj, (8, 12), (2, 3), 4, maxshape=(8, 12))

    def test_sparse_fixed_array_only_attributes_allocated_entries(self):
        with h5py.File(self.path, "w", libver="latest") as handle:
            data = handle.create_dataset("science", shape=(8, 12), dtype="<u4", chunks=(2, 3))
            data[:2, :3] = 7
        with h5py.File(self.path, "r") as handle, ModernH5File(self.path) as reader:
            obj = h5py.h5o.get_info(handle["science"].id).addr
            index = reader.read_index(obj, (8, 12), (2, 3), 4, maxshape=(8, 12))
            self.assertEqual(len(index.chunks), 1)
            self.assertEqual(index.chunks[0].coordinate, (0, 0))
            native = handle["science"].id.get_chunk_info(0)
            self.assertEqual(reader.absolute(index.chunks[0].address), native.byte_offset)

    def test_paged_fixed_array_uses_checked_page_slots(self):
        with h5py.File(self.path, "w", libver="latest") as handle:
            handle.create_dataset("science", data=np.arange(1089, dtype="<u4").reshape(33, 33),
                                  chunks=(1, 1))
        with h5py.File(self.path, "r") as handle, ModernH5File(self.path) as reader:
            obj = h5py.h5o.get_info(handle["science"].id).addr
            index = reader.read_index(obj, (33, 33), (1, 1), 4, maxshape=(33, 33))
            self.assertEqual(len(index.chunks), 1089)
            self.assertTrue(any(kind == "fixed-array data block page"
                                for _, _, kind in reader.metadata_ranges))
            self.assertEqual(reader.absolute(index.chunks[-1].address),
                             handle["science"].id.get_chunk_info_by_coord((32, 32)).byte_offset)

    def test_unallocated_single_address_has_no_attributed_chunk(self):
        obj = self._check_native()
        with ModernH5File(self.path) as reader:
            index = reader.read_index(obj, (4, 4), (4, 4), 4, maxshape=(4, 4))
            undefined = reader.superblock.undefined_address
        raw = bytearray(self.path.read_bytes())
        flags = raw[obj + 5]
        size_width = 1 << (flags & 3)
        prefix_length = 6 + (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0) + size_width
        size_start = obj + prefix_length - size_width
        message_bytes = int.from_bytes(raw[size_start : size_start + size_width], "little")
        checksum_offset = obj + prefix_length + message_bytes
        pointer = index.base_address.to_bytes(8, "little")
        positions = [i for i in range(obj + prefix_length, checksum_offset - 7)
                     if raw[i:i+8] == pointer]
        self.assertEqual(len(positions), 1)
        raw[positions[0]:positions[0]+8] = undefined.to_bytes(8, "little")
        raw[checksum_offset:checksum_offset+4] = lookup3(raw[obj:checksum_offset]).to_bytes(4, "little")
        self.path.write_bytes(raw)
        with ModernH5File(self.path) as reader:
            index = reader.read_index(obj, (4, 4), (4, 4), 4, maxshape=(4, 4))
            self.assertEqual(index.chunks, ())

    def test_observed_shape_and_filter_contradictions_refuse(self):
        obj = self._check_native()
        with ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(FormatError, "extents"):
                reader.read_index(obj, (8, 4), (4, 4), 4,
                                  maxshape=(8, 4))
        with ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(FormatError, "filtered single chunk"):
                reader.read_index(obj, (4, 4), (4, 4), 4,
                                  maxshape=(4, 4), filters=(1,))

    def test_superblock_checksum_known_from_native_file(self):
        self._check_native()
        raw = self.path.read_bytes()
        self.assertEqual(lookup3(raw[:44]), int.from_bytes(raw[44:48], "little"))
        self.assertEqual(lookup3(b""), 0xDEADBEEF)

    def test_equivalent_version_two_superblock_with_recomputed_checksum(self):
        obj = self._check_native()
        raw = bytearray(self.path.read_bytes())
        self.assertEqual(raw[8], 3)
        self.assertEqual(raw[11], 0)
        raw[8] = 2
        raw[44:48] = lookup3(raw[:44]).to_bytes(4, "little")
        self.path.write_bytes(raw)
        with ModernH5File(self.path) as reader:
            self.assertEqual(reader.superblock.version, 2)
            index = reader.read_index(obj, (4, 4), (4, 4), 4, maxshape=(4, 4))
            self.assertEqual(reader.read_at(index.chunks[0].address, 64),
                             np.arange(16, dtype="<u4").tobytes())

    def test_continuation_is_anchored_and_checksum_checked(self):
        with h5py.File(self.path, "w", libver="latest") as handle:
            space = h5py.h5s.create_simple((4, 4))
            dtype = h5py.h5t.py_create(np.dtype("<u4"))
            creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            creation.set_chunk((4, 4))
            creation.set_attr_phase_change(100, 100)
            low_level = h5py.h5d.create(handle.id, b"science", dtype, space, dcpl=creation)
            dataset = h5py.Dataset(low_level)
            dataset[:] = np.arange(16, dtype="<u4").reshape(4, 4)
            for i in range(50):
                dataset.attrs[f"attribute-{i}"] = b"x" * 40
        with h5py.File(self.path, "r") as handle, ModernH5File(self.path) as reader:
            dataset = handle["science"]
            obj = h5py.h5o.get_info(dataset.id).addr
            index = reader.read_index(obj, dataset.shape, dataset.chunks,
                                      dataset.dtype.itemsize, maxshape=dataset.maxshape)
            continuations = [(a, b) for a, b, kind in reader.metadata_ranges
                             if kind == "object header continuation"]
            self.assertEqual(len(continuations), 1)
            self.assertEqual(index.chunks[0].address,
                             dataset.id.get_chunk_info(0).byte_offset)
        raw = bytearray(self.path.read_bytes())
        raw[continuations[0][0] + 8] ^= 0x80
        self.path.write_bytes(raw)
        with ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(FormatError, "continuation signature or checksum"):
                reader.read_index(obj, (4, 4), (4, 4), 4, maxshape=(4, 4))


if __name__ == "__main__":
    unittest.main()
