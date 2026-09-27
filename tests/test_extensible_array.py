"""Differential and damaged-metadata checks for the rooted EA index parser."""

from __future__ import annotations

import tempfile
import unittest
from itertools import product
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.extensible_array import read_extensible_array
from h5reclaim.format import FormatError, UnsupportedFormat
from h5reclaim.modern_indexes import ModernH5File, lookup3


def _created(path: Path, shape: tuple[int, ...], chunks: tuple[int, ...],
             maxshape: tuple[int | None, ...], *, filtered: bool = False,
             sparse: bool = False, version: str = "latest") -> None:
    opts = {"compression": "gzip", "shuffle": True, "fletcher32": True} if filtered else {}
    initial = tuple(0 if maximum is None else dim for dim, maximum in zip(shape, maxshape))
    with h5py.File(path, "w", libver=("v110", "v110") if version == "v4" else "latest") as file:
        dataset = file.create_dataset("data", shape=initial, maxshape=maxshape,
                                      chunks=chunks, dtype="<u4", **opts)
        dataset.resize(shape)
        if sparse:
            dataset[tuple(slice(0, width) for width in chunks)] = 7
            last = tuple(slice(dim-width, dim) for dim, width in zip(shape, chunks))
            dataset[last] = 9
        else:
            dataset[...] = np.arange(np.prod(shape), dtype="<u4").reshape(shape)


def _parse(path: Path, *, sparse: bool = False, selected_metadata_from: Path | None = None):
    with h5py.File(selected_metadata_from or path, "r") as file:
        dataset = file["data"]
        object_address = h5py.h5o.get_info(dataset.id).addr
        shape, chunks, maxshape = dataset.shape, dataset.chunks, dataset.maxshape
        filters = tuple(dataset.id.get_create_plist().get_filter(i)[0]
                        for i in range(dataset.id.get_create_plist().get_nfilters()))
    with ModernH5File(path) as reader:
        layout, offset = reader._parse_object_layout(object_address)
        index_offset = 5 + layout[3] * layout[4]
        assert layout[index_offset] == 4, "fixture must use extensible-array index"
        pointer = index_offset + 6
        root = int.from_bytes(layout[pointer:pointer+reader.superblock.offset_size], "little")
        index = read_extensible_array(
            reader, root, shape, chunks, 4, maxshape=maxshape,
            layout_version=layout[0], layout_pointer_offset=offset+pointer,
            layout_params=tuple(layout[index_offset+1:pointer]),
            object_address=object_address, filters=filters, allow_sparse=sparse,
        )
        return index, reader.metadata_ranges[:]


class ExtensibleArrayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "source.h5"

    def test_hdf5_native_raw_chunk_differential_across_direct_data_and_secondary_blocks(self):
        for n in (3, 5, 40, 200, 1000):
            with self.subTest(chunks=n):
                _created(self.path, (n*10,), (10,), (None,))
                index, metadata = _parse(self.path)
                self.assertEqual(len(index.chunks), n)
                self.assertEqual(index.index_type, "extensible_array")
                self.assertGreaterEqual(len(metadata), 4)
                with h5py.File(self.path, "r") as file, ModernH5File(self.path) as reader:
                    dataset = file["data"]
                    for chunk in index.chunks:
                        mask, data = dataset.id.read_direct_chunk(chunk.coordinate)
                        self.assertEqual(mask, chunk.filter_mask)
                        self.assertEqual(data, reader.read_at(chunk.address, chunk.size))
                        self.assertTrue(chunk.evidence["index_chain"])
                        self.assertIsNotNone(chunk.pointer_offset)

    def test_filtered_v4_and_v5_with_order_and_size(self):
        for version in ("v4", "latest"):
            with self.subTest(version=version):
                _created(self.path, (2000,), (10,), (None,), filtered=True, version=version)
                index, _ = _parse(self.path)
                self.assertEqual(index.layout_version, 4 if version == "v4" else 5)
                with h5py.File(self.path, "r") as file, ModernH5File(self.path) as reader:
                    dataset = file["data"]
                    for chunk in index.chunks:
                        mask, data = dataset.id.read_direct_chunk(chunk.coordinate)
                        self.assertEqual((mask, data),
                                         (chunk.filter_mask, reader.read_at(chunk.address, chunk.size)))

    def test_swizzled_unlimited_dimension_and_fixed_maximum_extent(self):
        cases = (
            ((35, 7), (5, 3), (None, 10)),
            ((7, 35), (3, 5), (10, None)),
            ((5, 9, 11), (2, 3, 4), (5, None, 12)),
        )
        for shape, chunks, maximum in cases:
            with self.subTest(shape=shape, maximum=maximum):
                _created(self.path, shape, chunks, maximum)
                index, _ = _parse(self.path)
                grid = tuple((n+c-1)//c for n, c in zip(shape, chunks))
                expected = {tuple(i*c for i,c in zip(cell,chunks))
                            for cell in product(*(range(n) for n in grid))}
                self.assertEqual({item.coordinate for item in index.chunks}, expected)
                with h5py.File(self.path, "r") as file, ModernH5File(self.path) as reader:
                    dataset = file["data"]
                    for item in index.chunks:
                        mask, raw = dataset.id.read_direct_chunk(item.coordinate)
                        self.assertEqual((item.filter_mask, reader.read_at(item.address, item.size)),
                                         (mask, raw))

    def test_sparse_slots_are_unknown_when_explicitly_enabled(self):
        _created(self.path, (1000,), (10,), (None,), sparse=True)
        with self.assertRaises(UnsupportedFormat):
            _parse(self.path)
        index, _ = _parse(self.path, sparse=True)
        self.assertEqual([item.coordinate for item in index.chunks], [(0,), (990,)])

    def test_header_index_data_and_secondary_checksum_fail_closed(self):
        _created(self.path, (10000,), (10,), (None,))
        good = self.path.read_bytes()
        for sig in (b"EAHD", b"EAIB", b"EADB", b"EASB"):
            with self.subTest(signature=sig):
                bad = bytearray(good)
                offset = bad.find(sig)
                self.assertGreater(offset, 0)
                bad[offset+6] ^= 1
                modified = Path(self.temp.name) / (sig.decode() + ".h5")
                modified.write_bytes(bad)
                with self.assertRaises(FormatError):
                    _parse(modified, selected_metadata_from=self.path)

    def test_rechecks_back_pointer_even_when_index_checksum_is_recomputed(self):
        _created(self.path, (400,), (10,), (None,))
        index, ranges = _parse(self.path)
        index_address = index.data_block_address
        self.assertIsNotNone(index_address)
        block = next((start, end) for start, end, kind in ranges
                     if kind == "extensible-array index block")
        bad = bytearray(self.path.read_bytes())
        bad[index_address+6] ^= 1
        bad[block[1]-4:block[1]] = lookup3(bytes(bad[block[0]:block[1]-4])).to_bytes(4, "little")
        modified = Path(self.temp.name) / "rechecksum.h5"
        modified.write_bytes(bad)
        with self.assertRaisesRegex(FormatError, "back-pointer"):
            _parse(modified)

    def test_relinked_data_block_has_wrong_owned_row_even_with_fresh_checksum(self):
        _created(self.path, (400,), (10,), (None,))
        index, ranges = _parse(self.path)
        data_blocks = [start for start, _, kind in ranges
                       if kind == "extensible-array data block"]
        self.assertGreaterEqual(len(data_blocks), 2)
        iblock = next((start, end) for start, end, kind in ranges
                      if kind == "extensible-array index block")
        # EAIB prefix 6 + eight-byte header address + four eight-byte direct chunks.
        first_data_pointer = iblock[0] + 6 + 8 + 4*8
        bad = bytearray(self.path.read_bytes())
        bad[first_data_pointer:first_data_pointer+8] = data_blocks[1].to_bytes(8, "little")
        bad[iblock[1]-4:iblock[1]] = lookup3(bytes(bad[iblock[0]:iblock[1]-4])).to_bytes(4, "little")
        modified = Path(self.temp.name) / "relink.h5"
        modified.write_bytes(bad)
        with self.assertRaises(FormatError):
            _parse(modified)

    def test_two_rows_cannot_share_one_data_block(self):
        _created(self.path, (1200,), (10,), (None,))
        index, ranges = _parse(self.path)
        iblock = next((start, end) for start, end, kind in ranges
                      if kind == "extensible-array index block")
        first_data_pointer = iblock[0] + 6 + 8 + 4*8
        bad = bytearray(self.path.read_bytes())
        # Direct data-pointer 1 and 2 have the same capacity (32 slots).
        # Alias the latter to the former and rechecksum the index block.
        bad[first_data_pointer+2*8:first_data_pointer+3*8] = (
            bad[first_data_pointer+8:first_data_pointer+2*8]
        )
        bad[iblock[1]-4:iblock[1]] = lookup3(bytes(bad[iblock[0]:iblock[1]-4])).to_bytes(4, "little")
        modified = Path(self.temp.name) / "aliased_rows.h5"
        modified.write_bytes(bad)
        with self.assertRaises(FormatError):
            _parse(modified)


if __name__ == "__main__":
    unittest.main()
