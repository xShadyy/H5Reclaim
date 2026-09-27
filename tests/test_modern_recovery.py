"""Public recovery and survey using current HDF5 single/implicit indexes."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.recovery import recover
from h5reclaim.survey import survey
from h5reclaim.modern_indexes import ModernH5File, lookup3


def _implicit(path: Path) -> None:
    access = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
    access.set_libver_bounds(h5py.h5f.LIBVER_LATEST, h5py.h5f.LIBVER_LATEST)
    handle = h5py.h5f.create(bytes(path), h5py.h5f.ACC_TRUNC, fapl=access)
    space = h5py.h5s.create_simple((8, 12), (8, 12))
    dtype = h5py.h5t.py_create(np.dtype("<u4"))
    creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
    creation.set_chunk((2, 3))
    creation.set_alloc_time(h5py.h5d.ALLOC_TIME_EARLY)
    selected = h5py.h5d.create(handle, b"science", dtype, space, dcpl=creation)
    selected.write(h5py.h5s.ALL, h5py.h5s.ALL,
                   np.arange(96, dtype="<u4").reshape(8, 12))
    selected.close(); handle.close()


def _filtered_single(path: Path) -> None:
    access = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
    access.set_libver_bounds(h5py.h5f.LIBVER_LATEST, h5py.h5f.LIBVER_LATEST)
    handle = h5py.h5f.create(bytes(path), h5py.h5f.ACC_TRUNC, fapl=access)
    space = h5py.h5s.create_simple((32,), (32,))
    dtype = h5py.h5t.py_create(np.dtype("<f8"))
    creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
    creation.set_chunk((32,))
    creation.set_fletcher32()
    creation.set_deflate(6)
    selected = h5py.h5d.create(handle, b"science", dtype, space, dcpl=creation)
    selected.write(h5py.h5s.ALL, h5py.h5s.ALL,
                   np.linspace(-3.0, 7.0, 32, dtype="<f8"))
    selected.close(); handle.close()


class ModernRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.source = base / "source.h5"
        self.output = base / "result.h5"
        self.report = base / "report.json"

    def _verify(self, expected_index: str, expected_chunks: int) -> None:
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        overview = survey(self.source)
        selected = next(item for item in overview["datasets"]
                        if item["selected_path"] == "/science")
        self.assertEqual(selected["support"]["status"], "candidate")
        self.assertEqual(selected["index"]["type"], expected_index)
        result = recover(self.source, "/science", self.output, self.report)
        self.assertEqual(result["index"]["type"], expected_index)
        self.assertEqual(result["counts"]["recovered"], expected_chunks)
        self.assertEqual(result["reconstructed_chunks"], 0)
        ledger = result["evidence_ledger"]
        self.assertEqual(len(ledger["proposals"]), expected_chunks)
        self.assertEqual(len(ledger["links"]), expected_chunks +
                         (2 if expected_index == "fixed_array" else 0))
        self.assertFalse(ledger["contradictions"])
        self.assertTrue(all(item["status"] == "accepted" for item in ledger["decisions"]))
        self.assertTrue(all(item["integrity"] == (
            "stored_checksum_passed" if result["dataset"]["filters"]
            else "not_independently_verified"
        ) for item in ledger["decisions"]))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        self.assertEqual(json.loads(self.report.read_text()), result)
        with h5py.File(self.source, "r") as original, h5py.File(self.output, "r") as recovered:
            data = original["science"][:]
            copy = recovered["science"][:]
            self.assertEqual(data.tobytes(), copy.tobytes())
            status = recovered["/_h5reclaim/chunk_status"][:]
            self.assertTrue(np.all(status == 1))

    def test_modern_single_unfiltered(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("science", data=np.arange(16, dtype="<u4").reshape(4, 4),
                                  chunks=(4, 4))
        self._verify("single_chunk", 1)

    def test_modern_implicit_early_allocated_grid(self):
        _implicit(self.source)
        self._verify("implicit", 16)

    def test_modern_single_filtered_float_exact_bits(self):
        _filtered_single(self.source)
        self._verify("single_chunk", 1)
        self.assertEqual(json.loads(self.report.read_text())["mappings"][0]["integrity"],
                         "fletcher32_verified")

    def test_modern_nonpaged_fixed_array_exact_indexed_export(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("science", data=np.arange(96, dtype="<u4").reshape(8, 12),
                                  chunks=(2, 3))
        self._verify("fixed_array", 16)
        ledger = json.loads(self.report.read_text())["evidence_ledger"]
        self.assertTrue(all(len(p["index_link_ids"]) == 3 for p in ledger["proposals"]))

    def test_sparse_fixed_array_has_no_published_coordinates(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            data = handle.create_dataset("science", shape=(8, 12), dtype="<u4", chunks=(2, 3))
            data[:2, :3] = 7
        selected = survey(self.source)["datasets"][0]
        self.assertEqual(selected["support"]["status"], "unsupported")
        self.assertTrue(any(item["code"] == "index_unsupported"
                            for item in selected["support"]["reasons"]))
        with self.assertRaisesRegex(ValueError, "sparse fixed array"):
            recover(self.source, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_paged_fixed_array_is_explicitly_unsupported(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("science", data=np.arange(1089, dtype="<u4").reshape(33, 33),
                                  chunks=(1, 1))
        selected = survey(self.source)["datasets"][0]
        self.assertEqual(selected["support"]["status"], "unsupported")
        self.assertIn("paged fixed arrays", selected["support"]["reasons"][-1]["detail"])
        with self.assertRaisesRegex(ValueError, "paged fixed arrays"):
            recover(self.source, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_two_array_slots_pointing_to_same_chunk_refuse_even_with_valid_checksum(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("science", data=np.arange(96, dtype="<u4").reshape(8, 12),
                                  chunks=(2, 3))
        with h5py.File(self.source, "r") as handle, ModernH5File(self.source) as reader:
            selected = handle["science"]
            index = reader.read_index(
                h5py.h5o.get_info(selected.id).addr, selected.shape, selected.chunks,
                selected.dtype.itemsize, maxshape=selected.maxshape,
            )
            block_start, block_end, _ = next(
                item for item in reader.metadata_ranges if item[2] == "fixed-array data block"
            )
            first, second = index.chunks[:2]
        raw = bytearray(self.source.read_bytes())
        raw[second.pointer_offset:second.pointer_offset+8] = first.address.to_bytes(8, "little")
        raw[block_end-4:block_end] = lookup3(raw[block_start:block_end-4]).to_bytes(4, "little")
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "payload ranges overlap"):
            recover(self.source, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_corrupt_filtered_single_chunk_is_unassigned_not_silent_fill(self):
        _filtered_single(self.source)
        with h5py.File(self.source, "r") as handle, ModernH5File(self.source) as reader:
            selected = handle["science"]
            obj = h5py.h5o.get_info(selected.id).addr
            pipeline = selected.id.get_create_plist()
            filters = tuple(pipeline.get_filter(i)[0]
                            for i in range(pipeline.get_nfilters()))
            index = reader.read_index(obj, selected.shape, selected.chunks,
                                      selected.dtype.itemsize, maxshape=selected.maxshape,
                                      filters=filters)
            offset = reader.absolute(index.chunks[0].address)
        raw = bytearray(self.source.read_bytes())
        raw[offset + 20] ^= 0x80
        self.source.write_bytes(raw)
        result = recover(self.source, "/science", self.output, self.report)
        self.assertEqual(result["counts"]["decode_failed"], 1)
        self.assertEqual(result["counts"]["recovered"], 0)
        self.assertEqual(result["evidence_ledger"]["decisions"][0]["status"], "unassigned")
        with h5py.File(self.output, "r") as handle:
            self.assertEqual(int(handle["/_h5reclaim/chunk_status"][0]), 6)


if __name__ == "__main__":
    unittest.main()
