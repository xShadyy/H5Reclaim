"""Differential and damaged-metadata checks for the rooted EA index parser."""

from __future__ import annotations

import tempfile
import unittest
import hashlib
from itertools import product
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.extensible_array import read_extensible_array
from h5reclaim.format import FormatError, UnsupportedFormat
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.recovery import recover


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

    def test_single_eahd_to_eaib_pointer_damage_restores_original_checksum(self):
        _created(self.path, (400,), (10,), (None,), filtered=True)
        healthy, ranges = _parse(self.path)
        before = self.path.read_bytes()
        header = next((start, end) for start, end, kind in ranges
                      if kind == "extensible-array header")
        pointer = header[1] - 4 - 8
        damaged = bytearray(before)
        damaged[pointer] ^= 0x20
        broken = Path(self.temp.name) / "eahd_pointer.h5"
        broken.write_bytes(damaged)
        repaired, _ = _parse(broken, selected_metadata_from=self.path)
        self.assertEqual(healthy.chunks, tuple(
            item for item in healthy.chunks))
        self.assertEqual([(x.coordinate, x.address, x.size) for x in repaired.chunks],
                         [(x.coordinate, x.address, x.size) for x in healthy.chunks])
        self.assertEqual(len(repaired.reconstructed_links), 1)
        evidence = repaired.reconstructed_links[0]
        self.assertEqual((evidence["kind"], evidence["pointer_offset"],
                          evidence["target_address"]),
                         ("eahd_to_eaib", pointer, healthy.data_block_address))
        self.assertEqual(broken.read_bytes(), bytes(damaged))

        report = recover(broken, "/data", Path(self.temp.name) / "ea_export.h5",
                         Path(self.temp.name) / "ea_report.json")
        self.assertEqual(report["counts"]["recovered"], 40)
        self.assertEqual(broken.read_bytes(), bytes(damaged))

    def test_eahd_link_does_not_cover_other_header_or_child_damage(self):
        _created(self.path, (400,), (10,), (None,))
        healthy, ranges = _parse(self.path)
        header = next((start, end) for start, end, kind in ranges
                      if kind == "extensible-array header")
        index = next((start, end) for start, end, kind in ranges
                     if kind == "extensible-array index block")
        pointer = header[1] - 4 - 8
        cases = (
            {header[1]-4: 0x01},  # Checksum field damaged, no original to restore.
            {pointer: 0x01, header[0]+18: 0x01},  # Two distinct fields.
            {pointer: 0x01, index[0]+6: 0x01},  # Candidate back-pointer damaged.
        )
        for number, changes in enumerate(cases):
            with self.subTest(case=number):
                damaged = bytearray(self.path.read_bytes())
                for position, mask in changes.items():
                    damaged[position] ^= mask
                broken = Path(self.temp.name) / f"ea_refusal_{number}.h5"
                broken.write_bytes(damaged)
                with self.assertRaises(FormatError):
                    _parse(broken, selected_metadata_from=self.path)

        # A deliberately rechecksummed pointer redirect must not be treated
        # as the original-checksum-restoring repair.
        damaged = bytearray(self.path.read_bytes())
        damaged[pointer:pointer+8] = healthy.chunks[0].address.to_bytes(8, "little")
        damaged[header[1]-4:header[1]] = lookup3(
            bytes(damaged[header[0]:header[1]-4])).to_bytes(4, "little")
        broken = Path(self.temp.name) / "ea_rechecksum.h5"
        broken.write_bytes(damaged)
        with self.assertRaises(FormatError):
            _parse(broken, selected_metadata_from=self.path)

    def test_eaib_direct_data_block_link_restores_original_checksum(self):
        _created(self.path, (400,), (10,), (None,))
        healthy, ranges = _parse(self.path)
        target = next(step for item in healthy.chunks for step in item.evidence["index_chain"]
                      if step["kind"] == "eaib_to_eadb")
        pointer = target["pointer_offset"]
        before = self.path.read_bytes()
        raw = bytearray(before)
        raw[pointer] ^= 0x40
        broken = Path(self.temp.name) / "eaib_pointer.h5"
        broken.write_bytes(raw)
        repaired, _ = _parse(broken, selected_metadata_from=self.path)
        self.assertEqual(repaired.reconstructed_links[0]["kind"], "eaib_to_child")
        self.assertEqual(repaired.reconstructed_links[0]["target_address"],
                         target["target_address"])
        self.assertEqual([x.address for x in repaired.chunks],
                         [x.address for x in healthy.chunks])
        report = recover(broken, "/data", Path(self.temp.name) / "eaib_export.h5",
                         Path(self.temp.name) / "eaib_report.json")
        self.assertEqual(report["counts"]["recovered"], 40)
        self.assertEqual(broken.read_bytes(), bytes(raw))

        # With an unrelated EAIB byte also damaged there is no valid original
        # checksum to restore by modifying the pointer alone.
        corrupt = bytearray(raw)
        corrupt[pointer+8] ^= 1
        extra = Path(self.temp.name) / "eaib_two_faults.h5"
        extra.write_bytes(corrupt)
        with self.assertRaises(FormatError):
            _parse(extra, selected_metadata_from=self.path)

    def test_eaib_secondary_block_link_repairs_paged_index(self):
        _created(self.path, (2, 2), (1, 1), (1_000_000, None), filtered=True)
        healthy, _ = _parse(self.path)
        target = next(step for item in healthy.chunks for step in item.evidence["index_chain"]
                      if step["kind"] == "eaib_to_easb")
        raw = bytearray(self.path.read_bytes())
        raw[target["pointer_offset"]] ^= 0x10
        broken = Path(self.temp.name) / "eaib_secondary.h5"
        broken.write_bytes(raw)
        recovered, _ = _parse(broken, selected_metadata_from=self.path)
        self.assertEqual(recovered.reconstructed_links[0]["target_address"], target["target_address"])
        self.assertEqual([item.address for item in recovered.chunks],
                         [item.address for item in healthy.chunks])
        report = recover(broken, "/data", Path(self.temp.name) / "eaib_secondary_export.h5",
                         Path(self.temp.name) / "eaib_secondary_report.json")
        self.assertEqual(report["counts"]["recovered"], 4)

    def test_easb_data_block_link_repairs_paged_index(self):
        _created(self.path, (2, 2), (1, 1), (1_000_000, None), filtered=True)
        healthy, _ = _parse(self.path)
        target = next(step for item in healthy.chunks for step in item.evidence["index_chain"]
                      if step["kind"] == "easb_to_eadb")
        raw = bytearray(self.path.read_bytes())
        raw[target["pointer_offset"]] ^= 0x20
        broken = Path(self.temp.name) / "easb_data.h5"
        broken.write_bytes(raw)
        recovered, _ = _parse(broken, selected_metadata_from=self.path)
        self.assertEqual(recovered.reconstructed_links[0]["target_address"], target["target_address"])
        self.assertEqual(recovered.reconstructed_links[0]["kind"], "easb_to_eadb")
        self.assertEqual([item.address for item in recovered.chunks],
                         [item.address for item in healthy.chunks])
        report = recover(broken, "/data", Path(self.temp.name) / "easb_export.h5",
                         Path(self.temp.name) / "easb_report.json")
        self.assertEqual(report["counts"]["recovered"], 4)
        self.assertGreater(report["reconstructed_chunks"], 0)
        self.assertLess(report["reconstructed_chunks"], 4)

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

    def test_paged_secondary_blocks_match_native_slots_and_public_export(self):
        # A large fixed maximum gives a high EA linear slot without requiring
        # a million selected chunks or expanding the current dataset extent.
        for filtered, version in ((False, "latest"), (True, "v4"), (True, "latest")):
            with self.subTest(filtered=filtered, version=version):
                _created(self.path, (2, 2), (1, 1), (1_000_000, None),
                         filtered=filtered, version=version)
                index, metadata = _parse(self.path)
                self.assertEqual({item.coordinate for item in index.chunks},
                                 {(0, 0), (0, 1), (1, 0), (1, 1)})
                paged = [item for item in index.chunks
                         if item.evidence["slot_owner_kind"] == "extensible-array data block page"]
                self.assertEqual(len(paged), 2)
                self.assertTrue(any(kind == "extensible-array data block page"
                                    for _, _, kind in metadata))
                with h5py.File(self.path, "r") as file, ModernH5File(self.path) as reader:
                    dataset = file["data"]
                    for item in index.chunks:
                        mask, raw = dataset.id.read_direct_chunk(item.coordinate)
                        self.assertEqual((item.filter_mask, reader.read_at(item.address, item.size)),
                                         (mask, raw))
                source_hash = hashlib.sha256(self.path.read_bytes()).digest()
                output = Path(self.temp.name) / f"export_{int(filtered)}_{version}.h5"
                report = recover(self.path, "/data", output,
                                 Path(self.temp.name) / f"report_{int(filtered)}_{version}.json")
                self.assertEqual(report["counts"]["recovered"], 4)
                self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), source_hash)
                with h5py.File(self.path) as source, h5py.File(output) as recovered:
                    self.assertEqual(source["data"][...].tobytes(), recovered["data"][...].tobytes())

    def test_paged_checksum_and_reserved_bitmap_refuse_without_attribution(self):
        _created(self.path, (2, 2), (1, 1), (1_000_000, None))
        index, metadata = _parse(self.path)
        page = next((start, end) for start, end, kind in metadata
                    if kind == "extensible-array data block page")
        sblock = next((start, end) for start, end, kind in metadata
                      if kind == "extensible-array secondary block")
        good = self.path.read_bytes()
        damaged = bytearray(good)
        damaged[page[0]+3] ^= 1
        bad_page = Path(self.temp.name) / "bad_page.h5"
        bad_page.write_bytes(damaged)
        with self.assertRaisesRegex(FormatError, "page checksum mismatch"):
            _parse(bad_page, selected_metadata_from=self.path)

        # A checked page cannot assign a chunk to the reserved but still
        # uninitialized next page in the same data-block allocation.
        damaged = bytearray(good)
        paged = next(item for item in index.chunks if item.coordinate == (0, 1))
        damaged[paged.pointer_offset:paged.pointer_offset+8] = page[1].to_bytes(8, "little")
        damaged[page[1]-4:page[1]] = lookup3(
            bytes(damaged[page[0]:page[1]-4])).to_bytes(4, "little")
        inside_uninitialized_page = Path(self.temp.name) / "inside_page.h5"
        inside_uninitialized_page.write_bytes(damaged)
        with self.assertRaisesRegex(FormatError, "overlaps a paged data block allocation"):
            _parse(inside_uninitialized_page, selected_metadata_from=self.path)

        # The EASB holds reserved bitmap bytes beyond the actual page count.
        # A fresh EASB checksum alone is insufficient to accept contradictory
        # page-allocation information.
        damaged = bytearray(good)
        damaged[sblock[0] + 18 + 100] |= 1
        damaged[sblock[1]-4:sblock[1]] = lookup3(
            bytes(damaged[sblock[0]:sblock[1]-4])).to_bytes(4, "little")
        bad_bitmap = Path(self.temp.name) / "bad_bitmap.h5"
        bad_bitmap.write_bytes(damaged)
        with self.assertRaisesRegex(FormatError, "reserved bit"):
            _parse(bad_bitmap, selected_metadata_from=self.path)

        # Erasing the initialization bit and rechecksumming the EASB removes
        # the page from accepted data. It must never promote its old bytes.
        damaged = bytearray(good)
        page_step = paged.evidence["index_chain"][-1]
        bitmap_offset = page_step["bitmap_offset"]
        damaged[bitmap_offset] &= ~(0x80 >> (page_step["bitmap_bit"] % 8))
        damaged[sblock[1]-4:sblock[1]] = lookup3(
            bytes(damaged[sblock[0]:sblock[1]-4])).to_bytes(4, "little")
        missing_page = Path(self.temp.name) / "missing_page.h5"
        missing_page.write_bytes(damaged)
        partial, _ = _parse(missing_page, sparse=True, selected_metadata_from=self.path)
        self.assertEqual({item.coordinate for item in partial.chunks}, {(0, 0), (1, 0)})
        with self.assertRaises(UnsupportedFormat):
            _parse(missing_page, selected_metadata_from=self.path)

    def test_sparse_paged_secondary_multiple_pages_remain_unknown_elsewhere(self):
        # The fixed maximum swizzles coordinates from the second, unlimited
        # dimension into distant EA rows. Selected first-axis cells land in
        # pages 1, 2, and 3 of the same data block.
        with h5py.File(self.path, "w", libver="latest") as file:
            dataset = file.create_dataset("data", shape=(1700, 2),
                                          maxshape=(1_001_000, None),
                                          chunks=(1, 1), dtype="<u4", fillvalue=777)
            for i, value in ((0, 17), (500, 29), (1500, 41)):
                dataset[i, 1] = value
        index, _ = _parse(self.path, sparse=True)
        self.assertEqual({item.coordinate for item in index.chunks},
                         {(0, 1), (500, 1), (1500, 1)})
        pages = {item.evidence["index_chain"][-1]["page_index"] for item in index.chunks}
        self.assertEqual(pages, {1, 2, 3})
        with h5py.File(self.path, "r") as file, ModernH5File(self.path) as reader:
            for item in index.chunks:
                mask, raw = file["data"].id.read_direct_chunk(item.coordinate)
                self.assertEqual((mask, raw),
                                 (item.filter_mask, reader.read_at(item.address, item.size)))
        result = recover(self.path, "/data", Path(self.temp.name) / "sparse_export.h5",
                         Path(self.temp.name) / "sparse_report.json")
        self.assertEqual(result["counts"]["recovered"], 3)
        self.assertEqual(result["counts"]["allocation_unknown"], 3397)
        self.assertEqual(result["outcome"], "partial")

    def test_paged_bitmap_address_changes_across_data_blocks(self):
        # Different fixed maxima put the same selected coordinate in another
        # page or the next data block of the same secondary block.
        for maximum, page, bit in ((1_000_000, 0, 464), (1_001_024, 1, 465),
                                   (1_003_072, 3, 467), (1_004_096, 0, 468)):
            with self.subTest(maximum=maximum):
                _created(self.path, (2, 2), (1, 1), (maximum, None))
                index, _ = _parse(self.path)
                chosen = next(item for item in index.chunks if item.coordinate == (0, 1))
                self.assertEqual((chosen.evidence["index_chain"][-1]["page_index"],
                                  chosen.evidence["index_chain"][-1]["bitmap_bit"]),
                                 (page, bit))
                with h5py.File(self.path, "r") as file, ModernH5File(self.path) as reader:
                    mask, raw = file["data"].id.read_direct_chunk(chosen.coordinate)
                    self.assertEqual((mask, raw),
                                     (chosen.filter_mask, reader.read_at(chosen.address, chosen.size)))


if __name__ == "__main__":
    unittest.main()
