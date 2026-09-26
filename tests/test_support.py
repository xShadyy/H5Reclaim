"""Unsupported datasets fail before output creation or coordinate inference."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import UnsupportedFormat
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.recovery import recover


class SupportEnvelopeTests(unittest.TestCase):
    def test_rejects_filtered_edge_chunk_wrong_dtype_and_multiple_datasets(self) -> None:
        for case in ("filtered", "edge", "float", "big_endian", "multiple"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.h5"
                shape = (11, 12) if case == "edge" else (12, 12)
                values = np.arange(shape[0] * shape[1], dtype="<u4").reshape(shape)
                if case == "float":
                    values = values.astype("<f4")
                elif case == "big_endian":
                    values = values.astype(">u4")
                with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                    handle.create_dataset(
                        "measurements",
                        data=values,
                        chunks=(2, 2) if case == "edge" else (1, 1),
                        compression="gzip" if case == "filtered" else None,
                    )
                    if case == "multiple":
                        handle.create_dataset("distractor", data=np.arange(4, dtype="<u4"))
                before = source.read_bytes()
                output, report = root / "recovered.h5", root / "report.json"
                with self.assertRaises(UnsupportedCase):
                    recover(source, "/measurements", output, report)
                self.assertEqual(source.read_bytes(), before)
                self.assertFalse(output.exists())
                self.assertFalse(report.exists())

    def test_latest_layout_is_explicitly_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "latest.h5"
            with h5py.File(source, "x", libver="latest") as handle:
                handle.create_dataset(
                    "measurements",
                    data=np.arange(144, dtype="<u4").reshape(12, 12),
                    chunks=(1, 1),
                )
            before = source.read_bytes()
            output, report = root / "out.h5", root / "out.json"
            with self.assertRaises((UnsupportedCase, UnsupportedFormat)):
                recover(source, "/measurements", output, report)
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
