"""Public recovery integration for EA and v2 B-tree across schema and faults."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from itertools import product
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.recovery import recover
from h5reclaim.schema_codec import fletcher32


SELECTED = "/measurements/signal"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create(path: Path, *, family: str, shape: tuple[int, int],
            chunks: tuple[int, int], filtered: bool = False,
            nondefault: bool = False, sparse: bool = False,
            skip_optional: bool = False) -> None:
    if family not in ("extensible_array", "v2_btree"):
        raise ValueError("invalid tested index family")
    maxshape = (None, shape[1]) if family == "extensible_array" else (None, None)
    if nondefault:
        access = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
        access.set_libver_bounds(h5py.h5f.LIBVER_LATEST, h5py.h5f.LIBVER_LATEST)
        file_id = h5py.h5f.create(bytes(path), h5py.h5f.ACC_TRUNC, fapl=access)
        group_id = h5py.h5g.create(file_id, b"measurements")
        dims = tuple(h5py.h5s.UNLIMITED if item is None else item for item in maxshape)
        space = h5py.h5s.create_simple(shape, dims)
        creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        creation.set_chunk(chunks)
        creation.set_fletcher32()
        creation.set_shuffle()
        creation.set_deflate(6)
        dataset_id = h5py.h5d.create(group_id, b"signal", h5py.h5t.py_create(np.dtype("<u4")),
                                     space, dcpl=creation)
        dataset_id.close()
        group_id.close()
        file_id.close()
    else:
        with h5py.File(path, "x", libver="latest") as file:
            group = file.create_group("measurements")
            group.create_dataset("signal", shape=shape, maxshape=maxshape, chunks=chunks,
                                 dtype="<u4", compression="gzip" if filtered else None,
                                 shuffle=filtered, fletcher32=filtered, fillvalue=777)
    rng = np.random.default_rng(9147 + shape[0])
    with h5py.File(path, "r+") as file:
        ds = file[SELECTED]
        if sparse:
            ds[tuple(slice(0, size) for size in chunks)] = rng.integers(
                0, 2**32, size=chunks, dtype="<u4")
            last = tuple(((dim - 1) // size) * size for dim, size in zip(shape, chunks))
            extent = tuple(slice(start, dim) for start, dim in zip(last, shape))
            ds[extent] = rng.integers(0, 2**32,
                                      size=tuple(dim - start for start, dim in zip(last, shape)),
                                      dtype="<u4")
        else:
            ds[...] = rng.integers(0, 2**32, size=shape, dtype="<u4")
        if skip_optional:
            if not nondefault or sparse:
                raise ValueError("optional-mask test needs complete nondefault pipeline")
            plain = np.ascontiguousarray(ds[tuple(slice(0, size) for size in chunks)]).tobytes()
            # Filters are [Fletcher32, shuffle, DEFLATE]. Both optional filters
            # are skipped for one chunk, while the mandatory checksum remains.
            raw = plain + fletcher32(plain).to_bytes(4, "little")
            ds.id.write_direct_chunk((0, 0), raw, filter_mask=0b110)
        file.create_dataset("distractor", data=np.zeros(shape, dtype="<u4"))


class ModernEndToEndMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)

    def _recover_and_check(self, *, family: str, shape: tuple[int, int],
                           chunks: tuple[int, int], filtered: bool = False,
                           nondefault: bool = False, sparse: bool = False,
                           skip_optional: bool = False) -> dict:
        source, output, report_file = (self.folder / name for name in
                                        ("source.h5", "output.h5", "report.json"))
        _create(source, family=family, shape=shape, chunks=chunks, filtered=filtered,
                nondefault=nondefault, sparse=sparse, skip_optional=skip_optional)
        source_hash = _sha(source)
        report = recover(source, SELECTED, output, report_file)
        self.assertEqual(_sha(source), source_hash)
        self.assertEqual(json.loads(report_file.read_text()), report)
        self.assertEqual(report["index"]["type"], family)
        self.assertFalse(report["structural_repair"])
        self.assertEqual(report["reconstructed_chunks"], 0)
        self.assertFalse(report["evidence_ledger"]["contradictions"])
        with h5py.File(source, "r") as original, h5py.File(output, "r") as restored:
            before, after = original[SELECTED], restored[SELECTED]
            self.assertEqual((after.shape, after.chunks, after.dtype),
                             (before.shape, before.chunks, before.dtype))
            status = restored["/_h5reclaim/chunk_status"][...]
            native = {tuple(before.id.get_chunk_info(i).chunk_offset):
                      before.id.get_chunk_info(i)
                      for i in range(before.id.get_num_chunks())}
            self.assertEqual(report["counts"]["recovered"], len(native))
            self.assertEqual(len(report["mappings"]), len(native))
            seen = set()
            for mapping in report["mappings"]:
                origin = tuple(mapping["coordinate"])
                self.assertIn(origin, native)
                self.assertNotIn(origin, seen)
                seen.add(origin)
                info = native[origin]
                self.assertEqual((mapping["source_absolute_offset"], mapping["size_bytes"],
                                  mapping["filter_mask"]),
                                 (info.byte_offset, info.size, info.filter_mask))
                slices = tuple(slice(start, min(start + size, dim))
                               for start, size, dim in zip(origin, chunks, shape))
                self.assertEqual(after[slices].tobytes(), before[slices].tobytes())
            self.assertEqual(seen, set(native))
            for cell in product(*(range((dim + size - 1) // size)
                                  for dim, size in zip(shape, chunks))):
                origin = tuple(i * size for i, size in zip(cell, chunks))
                self.assertEqual(int(status[cell]), 1 if origin in native else 2)
            if sparse:
                self.assertEqual(report["outcome"], "partial")
            else:
                self.assertEqual(report["outcome"], "complete")
            if skip_optional:
                self.assertEqual(report["dataset"]["filters"], [3, 2, 1])
                self.assertEqual(next(x for x in report["mappings"]
                                      if x["coordinate"] == [0, 0])["filter_mask"], 6)

        # Every ledger hop in these two index families is a literal pointer.
        # Re-read its bytes and ensure the claimed child is what the file says.
        ledger = report["evidence_ledger"]
        links = {item["link_id"]: item for item in ledger["links"]}
        self.assertEqual(len(ledger["proposals"]), len(native))
        with ModernH5File(source) as reader:
            for proposal in ledger["proposals"]:
                path = proposal["index_link_ids"]
                self.assertGreaterEqual(len(path), 3)
                self.assertEqual(links[path[0]]["parent_offset"],
                                 report["dataset"]["object_address"])
                self.assertEqual(links[path[-1]]["child_offset"],
                                 proposal["extent"]["offset"])
                for left, right in zip(path, path[1:]):
                    self.assertEqual(links[left]["child_offset"], links[right]["parent_offset"])
                for identifier in path:
                    link = links[identifier]
                    extent = link["pointer_extent"]
                    with source.open("rb") as stream:
                        stream.seek(extent["offset"])
                        raw = stream.read(extent["length"])
                    self.assertEqual(len(raw), extent["length"])
                    self.assertEqual(reader.absolute(int.from_bytes(raw, "little")),
                                     link["child_offset"])
        return report

    def test_full_unfiltered_and_filtered_in_both_index_families(self) -> None:
        for family in ("extensible_array", "v2_btree"):
            for filtered in (False, True):
                with self.subTest(family=family, filtered=filtered):
                    # setUp provides a folder; each subcase needs distinct paths.
                    with tempfile.TemporaryDirectory() as directory:
                        self.folder = Path(directory)
                        self._recover_and_check(family=family, shape=(12, 15), chunks=(3, 3),
                                                filtered=filtered)

    def test_sparse_and_partial_edge_in_both_index_families(self) -> None:
        for family in ("extensible_array", "v2_btree"):
            with self.subTest(family=family):
                with tempfile.TemporaryDirectory() as directory:
                    self.folder = Path(directory)
                    self._recover_and_check(family=family, shape=(11, 7), chunks=(4, 3), sparse=True)

    def test_nondefault_order_and_per_chunk_optional_mask(self) -> None:
        for family in ("extensible_array", "v2_btree"):
            with self.subTest(family=family):
                with tempfile.TemporaryDirectory() as directory:
                    self.folder = Path(directory)
                    report = self._recover_and_check(
                        family=family, shape=(12, 15), chunks=(3, 3),
                        nondefault=True, skip_optional=True,
                    )
                    self.assertEqual(report["dataset"]["filters"], [3, 2, 1])

    def test_deep_v2_and_secondary_ea_link_paths(self) -> None:
        for family, shape, chunks in (
            ("v2_btree", (60, 60), (2, 2)),
            ("extensible_array", (80, 8), (2, 2)),
        ):
            with self.subTest(family=family):
                with tempfile.TemporaryDirectory() as directory:
                    self.folder = Path(directory)
                    report = self._recover_and_check(family=family, shape=shape, chunks=chunks)
                    self.assertTrue(any(len(proposal["index_link_ids"]) > 3
                                        for proposal in report["evidence_ledger"]["proposals"]))

    def test_corrupt_ea_index_checksum_and_rechecksummed_v2_duplicate_refuse(self) -> None:
        for family, shape, chunks in (
            ("extensible_array", (20, 5), (4, 5)),
            ("v2_btree", (11, 7), (4, 3)),
        ):
            with self.subTest(family=family):
                with tempfile.TemporaryDirectory() as directory:
                    folder = Path(directory)
                    source, damaged, output, report_file = (
                        folder / item for item in ("source.h5", "damaged.h5", "output.h5", "report.json"))
                    _create(source, family=family, shape=shape, chunks=chunks,
                            sparse=(family == "v2_btree"))
                    with h5py.File(source, "r") as native, ModernH5File(source) as reader:
                        selected = native[SELECTED]
                        index = reader.read_index(h5py.h5o.get_info(selected.id).addr,
                                                  selected.shape, selected.chunks,
                                                  selected.dtype.itemsize, maxshape=selected.maxshape)
                        if family == "extensible_array":
                            block = next((start, end) for start, end, kind in reader.metadata_ranges
                                         if kind == "extensible-array index block")
                        else:
                            first, second = index.chunks
                            self.assertEqual(first.evidence["node_address"],
                                             second.evidence["node_address"])
                    pristine_hash = _sha(source)
                    shutil.copyfile(source, damaged)
                    raw = bytearray(damaged.read_bytes())
                    if family == "extensible_array":
                        raw[block[0] + 15] ^= 0x20
                    else:
                        # Root leaf contains two type-10 records, 24 bytes each.
                        first, second = index.chunks
                        raw[second.pointer_offset + 8:second.pointer_offset + 24] = (
                            raw[first.pointer_offset + 8:first.pointer_offset + 24]
                        )
                        start = first.evidence["node_address"]
                        checksum = start + 6 + 2 * 24
                        raw[checksum:checksum + 4] = lookup3(raw[start:checksum]).to_bytes(4, "little")
                    damaged.write_bytes(raw)
                    input_hash = _sha(damaged)
                    with self.assertRaises(ValueError):
                        recover(damaged, SELECTED, output, report_file)
                    self.assertEqual((_sha(source), _sha(damaged)), (pristine_hash, input_hash))
                    self.assertFalse(output.exists())
                    self.assertFalse(report_file.exists())


if __name__ == "__main__":
    unittest.main()
