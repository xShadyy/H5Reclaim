"""Differential and negative tests for HDF5 fixed-array slot parsing."""

from __future__ import annotations

import itertools
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.filtered_fixed_array import read_fixed_array_variants
from h5reclaim.format import FormatError, UnsupportedFormat
from h5reclaim.modern_indexes import ModernH5File, lookup3


def _create(path: Path, shape: tuple[int, int], *, sparse: bool = False,
            filters: bool = True, libver: str = "latest") -> None:
    rng = np.random.default_rng(82743)
    with h5py.File(path, "w", libver=libver if libver == "latest" else (libver, libver)) as handle:
        kwargs = {"compression": "gzip", "fletcher32": True} if filters else {}
        selected = handle.create_dataset("science", shape=shape, chunks=(1, 1),
                                         dtype="<u4", **kwargs)
        if sparse:
            selected[0, 0] = 111
        else:
            selected[...] = rng.integers(0, 2**31, size=shape, dtype="<u4")


def _parse(reader: ModernH5File, dataset: h5py.Dataset,
           *, allow_sparse: bool = False):
    obj = h5py.h5o.get_info(dataset.id).addr
    layout, position = reader._parse_object_layout(obj)
    assert layout[0] in (4, 5) and layout[1] == 2
    rank = len(dataset.shape)
    width = layout[4]
    kind_offset = 5 + (rank + 1) * width
    assert layout[kind_offset] == 3
    page_bits = layout[kind_offset + 1]
    pointer_offset = position + kind_offset + 2
    root = int.from_bytes(layout[kind_offset + 2:], "little")
    filters = tuple(int(dataset.id.get_create_plist().get_filter(i)[0])
                    for i in range(dataset.id.get_create_plist().get_nfilters()))
    coords = tuple(itertools.product(*(range(n) for n in dataset.shape)))
    result = read_fixed_array_variants(
        reader, root, len(coords), dataset.dtype.itemsize,
        coords, layout[0], obj, pointer_offset, tuple(dataset.chunks),
        dataset.dtype.itemsize, page_bits, shape=tuple(dataset.shape), filters=filters,
        allow_sparse=allow_sparse,
    )
    return result


class FilteredFixedArrayTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "science.h5"

    def test_filtered_nonpaged_slots_match_native_addresses_sizes_masks_and_bytes(self) -> None:
        _create(self.path, (4, 4))
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            dataset = handle["science"]
            index = _parse(reader, dataset)
            self.assertEqual(index.index_type, "fixed_array")
            self.assertEqual(len(index.chunks), 16)
            self.assertEqual(len([kind for _, _, kind in reader.metadata_ranges
                                  if kind.startswith("fixed-array")]), 2)
            for position, record in enumerate(index.chunks):
                native = dataset.id.get_chunk_info(position)
                self.assertEqual((record.coordinate, reader.absolute(record.address),
                                  record.size, record.filter_mask),
                                 (native.chunk_offset, native.byte_offset,
                                  native.size, native.filter_mask))
                self.assertEqual(reader.read_at(record.address, record.size),
                                 dataset.id.read_direct_chunk(record.coordinate)[1])
                self.assertEqual(int.from_bytes(reader._read_absolute(record.pointer_offset, 8),
                                                "little"), record.address)

    def test_version_four_filtered_record_uses_compact_chunk_size_width(self) -> None:
        _create(self.path, (4, 4), libver="v110")
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            dataset = handle["science"]
            index = _parse(reader, dataset)
            self.assertEqual(index.layout_version, 4)
            header = reader.read_at(index.base_address, 28)
            self.assertEqual(header[6], 14)  # address 8 + size 2 + mask 4
            for record in index.chunks:
                native = dataset.id.get_chunk_info_by_coord(record.coordinate)
                self.assertEqual((reader.absolute(record.address), record.size, record.filter_mask),
                                 (native.byte_offset, native.size, native.filter_mask))

    def test_unfiltered_paged_array_uses_same_checked_page_ownership(self) -> None:
        _create(self.path, (33, 33), filters=False)
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            selected = handle["science"]
            index = _parse(reader, selected)
            self.assertEqual(len(index.chunks), 1089)
            self.assertEqual(index.chunks[1024].size, 4)
            self.assertEqual(index.chunks[1024].filter_mask, 0)
            self.assertEqual(reader.absolute(index.chunks[1024].address),
                             selected.id.get_chunk_info_by_coord((31, 1)).byte_offset)

    def test_wrong_row_major_coordinate_list_cannot_relabel_slots(self) -> None:
        _create(self.path, (4, 4))
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            selected = handle["science"]
            index = _parse(reader, selected)
            coordinates = [chunk.coordinate for chunk in index.chunks]
            coordinates[0], coordinates[1] = coordinates[1], coordinates[0]
        with ModernH5File(self.path) as reader:
            _layout, _position = reader._parse_object_layout(index.object_address)
            with self.assertRaisesRegex(FormatError, "row-major grid"):
                read_fixed_array_variants(
                    reader, index.base_address, 16, 4, tuple(coordinates), index.layout_version,
                    index.object_address, index.layout_pointer_offset,
                    (1, 1), 4, 10, shape=(4, 4), filters=(1, 3),
                )

    def test_optional_filter_skip_mask_is_read_from_each_slot(self) -> None:
        with h5py.File(self.path, "w", libver="latest") as handle:
            selected = handle.create_dataset("science", shape=(4, 4), chunks=(1, 1),
                                             dtype="<u4", compression="gzip")
            selected[...] = 9
            selected.id.write_direct_chunk((0, 0), np.array([123], dtype="<u4").tobytes(),
                                           filter_mask=1)
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            selected = handle["science"]
            index = _parse(reader, selected)
            first = index.chunks[0]
            self.assertEqual(first.filter_mask, 1)
            self.assertEqual(first.size, 4)
            self.assertEqual(selected[0, 0], 123)
            self.assertEqual((first.filter_mask, first.size),
                             (selected.id.get_chunk_info_by_coord((0, 0)).filter_mask,
                              selected.id.get_chunk_info_by_coord((0, 0)).size))

    def test_filtered_paged_slots_follow_checked_page_bitmap_and_checksums(self) -> None:
        _create(self.path, (33, 33))
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            dataset = handle["science"]
            index = _parse(reader, dataset)
            self.assertEqual(len(index.chunks), 1089)
            pages = [(start, end) for start, end, kind in reader.metadata_ranges
                     if kind == "fixed-array data block page"]
            self.assertEqual(len(pages), 2)
            self.assertEqual([end-start for start, end in pages], [1024*20+4, 65*20+4])
            for linear in (0, 1, 1023, 1024, 1088):
                record = index.chunks[linear]
                native = dataset.id.get_chunk_info_by_coord(record.coordinate)
                self.assertEqual((reader.absolute(record.address), record.size, record.filter_mask),
                                 (native.byte_offset, native.size, native.filter_mask))
                self.assertEqual(record.evidence["page_index"], linear // 1024)
                self.assertEqual(record.evidence["slot_owner_kind"], "fixed-array data block page")
                self.assertEqual([link["kind"] for link in record.evidence["link_path"]],
                                 ["layout_to_fahd", "fahd_to_fadb", "fixed_array_slot_to_chunk"])
                self.assertTrue(record.evidence["computed_page"]["bitmap_initialized"])
                self.assertTrue(record.evidence["metadata_checksums"]["validated"])
                self.assertTrue(any(start <= record.pointer_offset < end for start, end in pages))

    def test_sparse_uninitialized_page_and_unallocated_slots_are_unknown(self) -> None:
        _create(self.path, (33, 33), sparse=True)
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(UnsupportedFormat, "sparse fixed array"):
                _parse(reader, handle["science"])
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            index = _parse(reader, handle["science"], allow_sparse=True)
            self.assertEqual([chunk.coordinate for chunk in index.chunks], [(0, 0)])
            self.assertIn("fixed-array uninitialized page",
                          [kind for _, _, kind in reader.metadata_ranges])

    def test_first_paged_region_missing_is_not_read_as_chunk_slots(self) -> None:
        with h5py.File(self.path, "w", libver="latest") as handle:
            selected = handle.create_dataset("science", shape=(33, 33), chunks=(1, 1),
                                             dtype="<u4", compression="gzip")
            selected[32, 32] = 22
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(UnsupportedFormat, "page has not been initialized"):
                _parse(reader, handle["science"])
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            index = _parse(reader, handle["science"], allow_sparse=True)
            self.assertEqual([chunk.coordinate for chunk in index.chunks], [(32, 32)])

    def test_paged_checksum_mutation_refuses_all_coordinate_assignment(self) -> None:
        _create(self.path, (33, 33))
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            index = _parse(reader, handle["science"])
            target = index.chunks[1024].pointer_offset + 8
        content = bytearray(self.path.read_bytes())
        content[target] ^= 0x01
        self.path.write_bytes(content)
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            with self.assertRaisesRegex(FormatError, "page checksum mismatch"):
                _parse(reader, handle["science"])

    def test_checksum_valid_filter_mask_contradiction_is_refused(self) -> None:
        _create(self.path, (4, 4))
        with h5py.File(self.path) as handle, ModernH5File(self.path) as reader:
            index = _parse(reader, handle["science"])
            block = reader.absolute(index.data_block_address)
            object_address = index.object_address
            layout_pointer_offset = index.layout_pointer_offset
            end = next(end for start, end, kind in reader.metadata_ranges
                       if start == block and kind == "fixed-array data block")
            mask_at = index.chunks[0].pointer_offset + 16
        content = bytearray(self.path.read_bytes())
        content[mask_at:mask_at+4] = (4).to_bytes(4, "little")
        content[end-4:end] = lookup3(content[block:end-4]).to_bytes(4, "little")
        self.path.write_bytes(content)
        with ModernH5File(self.path) as reader:
            # Native h5py may refuse this altered index; use its prior object anchor.
            with self.assertRaisesRegex(FormatError, "filter mask"):
                layout, _ = reader._parse_object_layout(object_address)
                root = int.from_bytes(layout[-8:], "little")
                read_fixed_array_variants(reader, root, 16, 4,
                                          tuple(itertools.product(range(4), range(4))),
                                          layout[0], object_address, layout_pointer_offset,
                                          (1, 1), 4, layout[-9], shape=(4, 4), filters=(1, 3))


if __name__ == "__main__":
    unittest.main()
