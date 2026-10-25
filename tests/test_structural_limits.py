"""Larger structural budgets remain explicit and bound decompression."""

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.metadata import UnsupportedCase, read_dataset_spec
from h5reclaim.schema_codec import ChunkDecodeError, decode_chunk


class StructuralLimitTests(unittest.TestCase):
    def test_large_filtered_chunk_requires_and_obeys_explicit_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.h5"
            values = np.arange(1024 * 1024, dtype="<u4").reshape(1024, 1024)
            with h5py.File(path, "w", libver="latest") as handle:
                data = handle.create_dataset("data", data=values, chunks=values.shape,
                                             compression="gzip", shuffle=True, fletcher32=True)
                handle.flush()
                mask, raw = data.id.read_direct_chunk((0, 0))
            with self.assertRaises(UnsupportedCase):
                read_dataset_spec(path, "/data")
            spec = read_dataset_spec(path, "/data", max_chunk_bytes=4 << 20)
            with self.assertRaises(ChunkDecodeError):
                decode_chunk(raw, spec, mask)
            decoded = decode_chunk(raw, spec, mask, max_decoded_bytes=4 << 20,
                                   max_stored_bytes=8 << 20)
            self.assertEqual(decoded, values.tobytes())
            with self.assertRaises(ChunkDecodeError):
                decode_chunk(raw, spec, mask, max_decoded_bytes=4 << 20, max_stored_bytes=16)

    def test_metadata_element_budget_can_cover_large_sparse_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.h5"
            with h5py.File(path, "w", libver="latest") as handle:
                handle.create_dataset("data", shape=(2048, 2048, 2), chunks=(32, 32, 2), dtype="i2")
            with self.assertRaises(UnsupportedCase):
                read_dataset_spec(path, "/data")
            spec = read_dataset_spec(path, "/data", max_elements=16 << 20)
            self.assertEqual(spec.shape, (2048, 2048, 2))


if __name__ == "__main__":
    unittest.main()
