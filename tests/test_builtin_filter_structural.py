"""Checked broken-index recovery with HDF5's contextual built-in filters."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.large_structural import recover_large_fixed_array
from h5reclaim.metadata import read_dataset_spec
from h5reclaim.modern_indexes import ModernH5File
from h5reclaim.schema_codec import decode_chunk


def _codec_available(identifier: int) -> bool:
    return (h5py.h5z.filter_avail(identifier) and
            h5py.h5z.get_filter_info(identifier) &
            (h5py.h5z.FILTER_CONFIG_ENCODE_ENABLED | h5py.h5z.FILTER_CONFIG_DECODE_ENABLED)
            == h5py.h5z.FILTER_CONFIG_ENCODE_ENABLED | h5py.h5z.FILTER_CONFIG_DECODE_ENABLED)


class BuiltinStructuralFilterTests(unittest.TestCase):
    def test_nbit_scaleoffset_and_szip_from_real_damaged_indexes(self) -> None:
        for name, identifier in (("nbit", h5py.h5z.FILTER_NBIT),
                                 ("scaleoffset", h5py.h5z.FILTER_SCALEOFFSET),
                                 ("szip", h5py.h5z.FILTER_SZIP)):
            if not _codec_available(identifier):
                continue
            with self.subTest(filter=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, output, report_path = (root / path for path in
                    ("damaged.h5", "recovered.h5", "report.json"))
                values = np.arange(64, dtype="<u2")
                with h5py.File(source, "w", libver="latest") as handle:
                    if name == "nbit":
                        creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                        creation.set_chunk((8,))
                        creation.set_filter(identifier, 0, ())
                        dataset = h5py.h5d.create(handle.id, b"science",
                            h5py.h5t.py_create(values.dtype),
                            h5py.h5s.create_simple(values.shape), dcpl=creation)
                        dataset.write(h5py.h5s.ALL, h5py.h5s.ALL, values)
                    else:
                        options = ({"scaleoffset": 0} if name == "scaleoffset"
                                   else {"compression": "szip", "compression_opts": ("nn", 8)})
                        handle.create_dataset("science", data=values, chunks=(8,), **options)
                spec = read_dataset_spec(source, "/science")
                with h5py.File(source, "r") as handle:
                    mask, raw = handle["science"].id.read_direct_chunk((0,))
                np.testing.assert_array_equal(
                    np.frombuffer(decode_chunk(raw, spec, mask), dtype=values.dtype), values[:8])
                with ModernH5File(source, large_sparse_scan=True) as reader:
                    index = reader.read_index(spec.object_address, spec.shape, spec.chunks,
                        values.dtype.itemsize, maxshape=spec.maxshape, filters=spec.filters)
                self.assertEqual(index.index_type, "fixed_array")
                with source.open("r+b") as handle:
                    handle.seek(index.data_block_pointer_offset)
                    original = handle.read(1)
                    handle.seek(index.data_block_pointer_offset)
                    handle.write(bytes((original[0] ^ 0x67,)))
                damaged_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
                outcome = recover_large_fixed_array(source, "/science", output, report_path)
                self.assertEqual(outcome["counts"]["recovered"], 8)
                self.assertEqual(outcome["counts"]["decoder_unavailable"], 0)
                self.assertEqual(outcome["counts"]["decode_failed"], 0)
                self.assertEqual(outcome["dataset"]["filters_in_order"], [identifier])
                with h5py.File(output, "r") as handle:
                    np.testing.assert_array_equal(handle["science"][:], values)
                    np.testing.assert_array_equal(handle["/_h5reclaim/validity"][:],
                                                  np.ones((8,), dtype="u1"))
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), damaged_sha256)

    @unittest.skipUnless(_codec_available(h5py.h5z.FILTER_SCALEOFFSET),
                         "scale-offset encoder and decoder are required")
    def test_lossy_scaleoffset_decodes_only_currently_observable_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, report = (root / path for path in
                ("damaged.h5", "recovered.h5", "report.json"))
            values = np.linspace(-2.12345, 2.54321, 64, dtype="<f4")
            with h5py.File(source, "w", libver="latest") as handle:
                handle.create_dataset("science", data=values, chunks=(8,), scaleoffset=2)
            with h5py.File(source, "r") as handle:
                observable = handle["science"][:]
            self.assertFalse(np.array_equal(values, observable))
            spec = read_dataset_spec(source, "/science")
            with ModernH5File(source, large_sparse_scan=True) as reader:
                index = reader.read_index(spec.object_address, spec.shape, spec.chunks,
                    values.dtype.itemsize, maxshape=spec.maxshape, filters=spec.filters)
            with source.open("r+b") as handle:
                handle.seek(index.data_block_pointer_offset)
                byte = handle.read(1)
                handle.seek(index.data_block_pointer_offset)
                handle.write(bytes((byte[0] ^ 0x67,)))
            outcome = recover_large_fixed_array(source, "/science", output, report)
            self.assertEqual(outcome["counts"]["recovered"], 8)
            with h5py.File(output, "r") as handle:
                np.testing.assert_array_equal(handle["science"][:], observable)


if __name__ == "__main__":
    unittest.main()
