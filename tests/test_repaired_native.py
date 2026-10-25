"""Checked broken-index recovery of heap-backed and reference records."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.metadata import UnsupportedCase
from h5reclaim.modern_indexes import ModernH5File
from h5reclaim.repaired_native import _file_element_width, recover_relocated_index


class CheckedNativeRelocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source, self.output, self.report = (root / name for name in
                                                ("source.h5", "result.h5", "report.json"))

    def _create(self, dtype, values, *, compression=None):
        with h5py.File(self.source, "w", libver="latest") as handle:
            selected = handle.create_dataset("science", shape=(64,), chunks=(1,),
                                             dtype=dtype, compression=compression)
            for coordinate, value in values.items():
                selected[coordinate] = selected.ref if value == "self" else value
            object_address = h5py.h5o.get_info(selected.id).addr
            element_width = _file_element_width(selected.id.get_type(), 8)
        with ModernH5File(self.source) as reader:
            index = reader.read_index(object_address, (64,), (1,), element_width,
                maxshape=(64,), filters=(h5py.h5z.FILTER_DEFLATE,) if compression else (),
                max_chunks=64)
        self.assertEqual(index.index_type, "fixed_array")
        self.assertEqual(len(index.chunks), len(values))
        return index

    def _damage(self, index):
        with self.source.open("r+b") as stream:
            stream.seek(index.data_block_pointer_offset)
            original = stream.read(1)
            stream.seek(index.data_block_pointer_offset)
            stream.write(bytes((original[0] ^ 0x67,)))
        return hashlib.sha256(self.source.read_bytes()).hexdigest()

    def test_variable_utf8_strings_survive_checked_compressed_link_repair(self):
        index = self._create(h5py.string_dtype("utf-8"),
                             {0: "alpha", 32: "café", 63: "gamma"}, compression="gzip")
        damaged_digest = self._damage(index)
        result = recover_relocated_index(self.source, "/science", self.output, self.report)
        self.assertEqual((result["mode"], result["accepted_elements"], result["unknown_elements"]),
                         ("checked_index_native_relocation", 3, 61))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), damaged_digest)
        self.assertEqual(len(result["index"]["repairs"]), 1)
        with h5py.File(self.output, "r") as handle:
            data = handle["science"]
            self.assertEqual((data[0], data[32], data[63]),
                             (b"alpha", "café".encode(), b"gamma"))
            status = handle["/_h5reclaim/element_status"][:]
            self.assertEqual([int(status[n]) for n in (0, 1, 32, 63)], [1, 2, 1, 1])
            self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()])["index"],
                             result["index"])

    def test_nested_compound_ragged_numeric_record_is_relocated(self):
        dtype = np.dtype([("number", "<i4"),
                          ("series", h5py.vlen_dtype(np.dtype("<i2")))])
        index = self._create(dtype, {0: (7, np.array([2, 3, 4], dtype="<i2")),
                                     63: (9, np.array([8, 13], dtype="<i2"))})
        self._damage(index)
        result = recover_relocated_index(self.source, "/science", self.output, self.report)
        self.assertEqual(result["accepted_elements"], 2)
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["science"][0]["series"], [2, 3, 4])
            np.testing.assert_array_equal(handle["science"][63]["series"], [8, 13])
            self.assertEqual(int(handle["science"][63]["number"]), 9)
            with h5py.File(self.source, "r") as original:
                self.assertEqual(handle["science"].id.get_type().encode(),
                                 original["science"].id.get_type().encode())

    def test_self_reference_is_remapped_into_recovered_file(self):
        index = self._create(h5py.ref_dtype, {0: "self", 63: "self"})
        self._damage(index)
        result = recover_relocated_index(self.source, "/science", self.output, self.report)
        self.assertEqual((result["accepted_elements"], result["unknown_elements"]), (2, 62))
        with h5py.File(self.output, "r") as handle:
            for coordinate in (0, 63):
                self.assertEqual(handle[handle["science"][coordinate]].name, "/science")
            self.assertEqual(int(handle["/_h5reclaim/element_status"][1]), 2)

    def test_foreign_reference_is_deferred_without_false_target(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            other = handle.create_dataset("other", data=np.arange(4, dtype="<u2"))
            selected = handle.create_dataset("science", shape=(64,), chunks=(1,),
                                             dtype=h5py.ref_dtype)
            selected[0] = other.ref
            address = h5py.h5o.get_info(selected.id).addr
        with ModernH5File(self.source) as reader:
            index = reader.read_index(address, (64,), (1,), 8, maxshape=(64,), max_chunks=64)
        self._damage(index)
        result = recover_relocated_index(self.source, "/science", self.output, self.report)
        self.assertEqual((result["accepted_elements"], result["deferred_reference_elements"]), (0, 1))
        with h5py.File(self.output, "r") as handle:
            self.assertEqual(int(handle["/_h5reclaim/element_status"][0]), 4)
            self.assertNotIn("other", handle)

    def test_self_region_reference_selection_is_remapped(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            selected = handle.create_dataset("science", shape=(64,), chunks=(1,),
                                             dtype=h5py.regionref_dtype)
            selected[0] = selected.regionref[0:2]
            address = h5py.h5o.get_info(selected.id).addr
        with ModernH5File(self.source) as reader:
            index = reader.read_index(address, (64,), (1,), 12, maxshape=(64,), max_chunks=64)
        self._damage(index)
        result = recover_relocated_index(self.source, "/science", self.output, self.report)
        self.assertEqual(result["accepted_elements"], 1)
        with h5py.File(self.output, "r") as handle:
            reference = handle["science"][0]
            self.assertEqual(handle[reference].name, "/science")
            self.assertEqual(len(handle["science"][reference]), 2)

    def test_intact_index_needs_no_speculative_relocation(self):
        self._create(h5py.string_dtype("utf-8"), {0: "alpha"})
        with self.assertRaisesRegex(UnsupportedCase, "requires a damaged"):
            recover_relocated_index(self.source, "/science", self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_automatic_route_recovers_broken_variable_string_index(self):
        from h5reclaim.rescue import auto_rescue
        index = self._create(h5py.string_dtype("utf-8"), {0: "A", 63: "Z"})
        self._damage(index)
        result = auto_rescue(self.source, "/science", self.output, self.report)
        self.assertEqual(result["mode"], "checked_index_native_relocation")
        self.assertEqual(result["automatic_routing"]["selected"], "relocated_native")
        self.assertEqual(result["accepted_elements"], 2)

    def test_whole_file_can_remap_reference_to_intact_sibling(self):
        from h5reclaim.whole_file import rescue_all
        with h5py.File(self.source, "w", libver="latest") as handle:
            other = handle.create_dataset("other", data=np.arange(4, dtype="<u2"))
            selected = handle.create_dataset("science", shape=(64,), chunks=(1,),
                                             dtype=h5py.ref_dtype)
            selected[0] = other.ref
            address = h5py.h5o.get_info(selected.id).addr
        with ModernH5File(self.source) as reader:
            index = reader.read_index(address, (64,), (1,), 8, maxshape=(64,), max_chunks=64)
        self._damage(index)
        result = rescue_all(self.source, self.output, self.report)
        self.assertEqual((result["datasets_exported"], result["datasets_failed"]), (2, 0))
        science = next(entry["report"] for entry in result["datasets"] if entry["path"] == "/science")
        self.assertEqual(science["accepted_elements"], 1)
        self.assertEqual(science["deferred_reference_elements"], 0)
        with h5py.File(self.output, "r") as handle:
            self.assertEqual(handle[handle["science"][0]].name, "/other")
            np.testing.assert_array_equal(handle["other"][:], np.arange(4, dtype="<u2"))


if __name__ == "__main__":
    unittest.main()
