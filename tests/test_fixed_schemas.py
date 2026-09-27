"""Fixed-width scientific records survive structural damage without type conversion."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import H5File
from h5reclaim.metadata import UnsupportedCase, read_dataset_spec
from h5reclaim.metadata_fallback import _old_messages, read_dataset_spec_fallback
from h5reclaim.recovery import recover
from h5reclaim.baseline import capture_baseline


RECORD = np.dtype({
    "names": ["sequence", "potential", "flag", "label"],
    "formats": [">u4", "<f8", ">i2", "S6"],
    "offsets": [0, 8, 16, 18], "itemsize": 32,
})


class FixedSchemaRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "damaged.h5"
        self.output = self.root / "out.h5"
        self.report = self.root / "out.json"

    def _records(self, shape):
        values = np.zeros(shape, dtype=RECORD)
        values["sequence"] = np.arange(values.size, dtype=">u4").reshape(shape)
        values["potential"] = (np.arange(values.size) * 0.625).reshape(shape)
        values["flag"] = -3
        values["label"] = b"ADC"
        return values

    def _recover_and_check(self, expected, original_type, damaged_hash):
        result = recover(self.source, "/lab/readings", self.output, self.report)
        self.assertTrue(result["complete"])
        self.assertEqual(result["counts"]["allocation_unknown"], 0)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), damaged_hash)
        with h5py.File(self.output) as output:
            dataset = output["/lab/readings"]
            self.assertTrue(dataset.id.get_type().equal(original_type))
            np.testing.assert_array_equal(dataset[:], expected)
            self.assertEqual(dataset.dtype.itemsize, 32)
            self.assertEqual(dataset.dtype.fields["potential"][1], 8)
            self.assertEqual(dataset.dtype.fields["sequence"][0].byteorder, ">")

    def test_broken_v1_index_link_preserves_mixed_endian_padded_compound(self):
        expected = self._records((256, 256))
        with h5py.File(self.source, "w", libver="earliest") as handle:
            dataset = handle.create_dataset("/lab/readings", data=expected, chunks=(8, 8))
            original_type = dataset.id.get_type().copy()
            object_address = int(h5py.h5o.get_info(dataset.id).addr)
        with H5File(self.source) as reader:
            layout = reader.read_dataset_layout(object_address, rank=2)
            root = reader.read_tree(layout.root_address, rank=2, element_size=32)
            self.assertEqual(root.level, 1)
            self.assertGreaterEqual(len(root.entries), 3)
            pointer = root.entries[1].pointer_offset
            original_pointer = root.entries[1].address
            self.assertIsNotNone(original_pointer)
            self.assertNotEqual(root.entries[0].address, original_pointer)
        image = bytearray(self.source.read_bytes())
        image[pointer:pointer+8] = b"\xff" * 8
        self.source.write_bytes(image)
        damaged_hash = hashlib.sha256(image).hexdigest()
        self._recover_and_check(expected, original_type, damaged_hash)
        self.assertGreater(self.report.read_text().count('"reconstructed_link"'), 0)

    def test_older_damaged_optional_message_recovers_filtered_edge_records(self):
        expected = self._records((5, 7))
        with h5py.File(self.source, "w", libver="earliest") as handle:
            dataset = handle.create_dataset("/lab/readings", data=expected, chunks=(2, 3),
                                            shuffle=True, compression="gzip", fletcher32=True)
            original_type = dataset.id.get_type().copy()
            address = int(h5py.h5o.get_info(dataset.id).addr)
        with H5File(self.source) as reader:
            fill = next(message for message in _old_messages(reader, address) if message.kind == 5)
        image = bytearray(self.source.read_bytes())
        image[fill.absolute_offset] = 255
        self.source.write_bytes(image)
        with h5py.File(self.source) as handle:
            with self.assertRaises((KeyError, OSError)):
                _ = handle["/lab/readings"]
        fallback = read_dataset_spec_fallback(self.source, "/lab/readings")
        self.assertEqual(fallback.spec.dtype, RECORD)
        self.assertIsNotNone(fallback.spec.file_type_encoding)
        self._recover_and_check(expected, original_type, hashlib.sha256(image).hexdigest())

    def test_enum_array_string_opaque_types_keep_exact_h5t(self):
        types = {
            "enum": h5py.enum_dtype({"IDLE": 0, "ACQUIRE": 2}, basetype=">u2"),
            "array": np.dtype((np.dtype(">f4"), (2, 3))),
            "string": np.dtype("S9"),
            "opaque": h5py.opaque_dtype(np.dtype("V5")),
        }
        for name, file_dtype in types.items():
            with self.subTest(name=name):
                path = self.root / f"{name}.h5"
                result_path = self.root / f"{name}-out.h5"
                report_path = self.root / f"{name}-out.json"
                with h5py.File(path, "w", libver="earliest") as handle:
                    dataset = handle.create_dataset("data", shape=(8,), chunks=(4,),
                                                    dtype=file_dtype)
                    original_type = dataset.id.get_type().copy()
                    dataset[...] = np.zeros((8,), dtype=dataset.dtype)
                spec = read_dataset_spec(path, "/data")
                self.assertEqual(np.dtype(spec.dtype).itemsize, original_type.get_size())
                self.assertIsNotNone(spec.file_type_encoding)
                result = recover(path, "/data", result_path, report_path)
                self.assertTrue(result["complete"])
                with h5py.File(result_path) as output:
                    self.assertTrue(output["/data"].id.get_type().equal(original_type))

    def test_heap_and_references_refused_without_output(self):
        types = {
            "vlen": h5py.string_dtype(encoding="utf-8"),
            "reference": h5py.ref_dtype,
            "compound_reference": np.dtype([("id", "<u4"), ("linked", h5py.ref_dtype)]),
        }
        for name, file_dtype in types.items():
            with self.subTest(name=name):
                path = self.root / f"refuse-{name}.h5"
                with h5py.File(path, "w", libver="earliest") as handle:
                    handle.create_dataset("data", shape=(8,), chunks=(4,), dtype=file_dtype)
                with self.assertRaisesRegex(UnsupportedCase, "datatype|variable|reference|VLEN"):
                    read_dataset_spec(path, "/data")

    def test_numeric_only_legacy_baseline_refuses_complex_schema_cleanly(self):
        with h5py.File(self.source, "w", libver="earliest") as handle:
            handle.create_dataset("data", data=self._records((8,)), chunks=(4,))
        destination = self.root / "baseline.json"
        with self.assertRaisesRegex(UnsupportedCase, "exact-schema recovery capsule"):
            capture_baseline(self.source, "/data", destination)
        self.assertFalse(destination.exists())

    def test_modern_native_and_rooted_fallback_keep_fixed_compound(self):
        for damaged_fill in (False, True):
            with self.subTest(damaged_fill=damaged_fill):
                source = self.root / f"modern-{damaged_fill}.h5"
                out = self.root / f"modern-{damaged_fill}-out.h5"
                report = self.root / f"modern-{damaged_fill}-out.json"
                expected = self._records((5, 7))
                with h5py.File(source, "w", libver="latest") as handle:
                    dataset = handle.create_dataset("/lab/readings", data=expected, chunks=(2, 3),
                                                    shuffle=True, compression="gzip",
                                                    fletcher32=True)
                    original_type = dataset.id.get_type().copy()
                    address = int(h5py.h5o.get_info(dataset.id).addr)
                if damaged_fill:
                    from h5reclaim.metadata_fallback import _messages
                    from h5reclaim.modern_indexes import ModernH5File, lookup3
                    with ModernH5File(source) as reader:
                        fill = next(message for message in _messages(reader, address)
                                    if message.kind == 5)
                    image = bytearray(source.read_bytes())
                    width = 1 << (image[address + 5] & 3)
                    extra = (16 if image[address + 5] & 0x20 else 0) + (
                        4 if image[address + 5] & 0x10 else 0)
                    content = address + 6 + extra + width
                    count = int.from_bytes(image[content - width:content], "little")
                    checksum = content + count
                    image[fill.absolute_offset] = 255
                    image[checksum:checksum+4] = lookup3(image[address:checksum]).to_bytes(4, "little")
                    source.write_bytes(image)
                    fallback = read_dataset_spec_fallback(source, "/lab/readings")
                    self.assertEqual(fallback.spec.dtype, RECORD)
                result = recover(source, "/lab/readings", out, report)
                self.assertTrue(result["complete"])
                with h5py.File(out) as handle:
                    dataset = handle["/lab/readings"]
                    self.assertTrue(dataset.id.get_type().equal(original_type))
                    np.testing.assert_array_equal(dataset[:], expected)

    def test_committed_compound_datatype_resolves_through_rooted_shared_message(self):
        expected = self._records((8,))
        with h5py.File(self.source, "w", libver="latest") as handle:
            datatype = h5py.h5t.py_create(RECORD)
            datatype.commit(handle.id, b"adc_schema")
            space = h5py.h5s.create_simple((8,))
            creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            creation.set_chunk((4,))
            dataset = h5py.h5d.create(handle.id, b"data", datatype, space, dcpl=creation)
            dataset.write(h5py.h5s.ALL, h5py.h5s.ALL, expected)
            original_type = dataset.get_type().copy()
        fallback = read_dataset_spec_fallback(self.source, "/data")
        self.assertIn("with_shared_schema", fallback.route)
        self.assertIsNotNone(fallback.spec.file_type_encoding)
        result = recover(self.source, "/data", self.output, self.report)
        self.assertTrue(result["complete"])
        with h5py.File(self.output) as handle:
            recovered = handle["/data"]
            self.assertTrue(recovered.id.get_type().equal(original_type))
            np.testing.assert_array_equal(recovered[:], expected)


if __name__ == "__main__":
    unittest.main()
