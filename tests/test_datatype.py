"""Direct byte export requires canonical full-width on-disk numeric types."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h5py

from h5reclaim.metadata import UnsupportedCase, read_dataset_spec


class DatatypeRepresentationTests(unittest.TestCase):
    def _source(self, path: Path, *, precision: int = 32, offset: int = 0,
                padding: tuple[int, int] | None = None) -> None:
        with h5py.File(path, "x", libver=("earliest", "v108")) as handle:
            datatype = h5py.h5t.STD_U32LE.copy()
            datatype.set_precision(precision)
            datatype.set_offset(offset)
            if padding is not None:
                datatype.set_pad(*padding)
            space = h5py.h5s.create_simple((12, 12))
            creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            creation.set_chunk((2, 2))
            dataset = h5py.h5d.create(handle.id, b"measurements", datatype, space, dcpl=creation)
            dataset.close()

    def test_canonical_uint32_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "canonical.h5"
            self._source(source)
            spec = read_dataset_spec(source, "/measurements")
            self.assertEqual(spec.shape, (12, 12))
            self.assertEqual(spec.chunks, (2, 2))

    def test_noncanonical_integer_bit_layouts_are_rejected(self) -> None:
        cases = {
            "reduced_precision": {"precision": 24},
            "shifted_bits": {"precision": 31, "offset": 1},
            "nonstandard_padding": {"padding": (h5py.h5t.PAD_ONE, h5py.h5t.PAD_ONE)},
        }
        for name, options in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / f"{name}.h5"
                self._source(source, **options)
                with self.assertRaisesRegex(UnsupportedCase, "noncanonical"):
                    read_dataset_spec(source, "/measurements")


if __name__ == "__main__":
    unittest.main()
