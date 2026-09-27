"""Exact-value and hostile-chunk checks for the supported GWOSC strain case."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zlib
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.metadata import DatasetSpec, UnsupportedCase, read_dataset_spec
from h5reclaim.recovery import (
    ChunkDecodeError, STATUS_CODES, _decode_chunk, _fletcher32, analyze, recover,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "corpus/files/H-H1_GWOSC_16KHZ_R1-1126259447-32.hdf5"


class FilterChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = DatasetSpec("/strain/Strain", 100, (16,), (16,), "<f8", (3, 1))
        self.payload = np.arange(16, dtype="<f8").tobytes()
        self.checked = self.payload + _fletcher32(self.payload).to_bytes(4, "little")

    def test_fletcher_then_deflate_and_optional_deflate_skip(self) -> None:
        self.assertEqual(_decode_chunk(zlib.compress(self.checked), self.spec, 0), self.payload)
        self.assertEqual(_decode_chunk(self.checked, self.spec, 2), self.payload)

    def test_wrong_checksum_and_unknown_filter_masks_refused(self) -> None:
        damaged = bytearray(self.checked)
        damaged[2] ^= 0x80
        with self.assertRaisesRegex(ChunkDecodeError, "Fletcher32 checksum mismatch"):
            _decode_chunk(zlib.compress(damaged), self.spec, 0)
        for mask in (1, 3, 4, 0x80000000):
            with self.subTest(mask=mask), self.assertRaises(ChunkDecodeError):
                _decode_chunk(self.checked, self.spec, mask)

    def test_extra_stream_or_output_past_fixed_bound_refused(self) -> None:
        for raw in (
            zlib.compress(self.checked) + b"trailing",
            zlib.compress(self.checked + b"x"),
            zlib.compress(self.checked + b"x" * (1 << 20)),
            b"not a zlib stream",
        ):
            with self.subTest(length=len(raw)), self.assertRaises(ChunkDecodeError):
                _decode_chunk(raw, self.spec, 0)

    def test_integer_case_remains_unfiltered_only(self) -> None:
        unfiltered = DatasetSpec("/measurements", 100, (2, 2), (2, 2))
        payload = np.array([[0, 1], [2, 3]], dtype="<u4").tobytes()
        self.assertEqual(_decode_chunk(payload, unfiltered, 0), payload)
        with self.assertRaises(ChunkDecodeError):
            _decode_chunk(payload + b"x", unfiltered, 0)

    def test_other_rank_one_filters_and_datatypes_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "unsupported.h5"
            with h5py.File(source, "x") as handle:
                # h5py writes gzip first and Fletcher32 second, which is a
                # different pipeline from the official GWOSC source.
                handle.create_dataset(
                    "other_order", data=np.arange(64, dtype="<f8"),
                    chunks=(16,), compression="gzip", fletcher32=True,
                )
                handle.create_dataset(
                    "wrong_type", data=np.arange(64, dtype="<u4"), chunks=(16,),
                )
            with self.assertRaisesRegex(UnsupportedCase, "Fletcher32 followed by deflate"):
                read_dataset_spec(source, "/other_order")
            with self.assertRaisesRegex(UnsupportedCase, "IEEE binary64"):
                read_dataset_spec(source, "/wrong_type")


class RealSourceTests(unittest.TestCase):
    def test_original_source_exact_bits_and_scientific_scalar_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "recovered.h5"
            report_path = Path(directory) / "report.json"
            source_before = SOURCE.read_bytes()
            report = recover(SOURCE, "/strain/Strain", output, report_path)
            self.assertEqual(SOURCE.read_bytes(), source_before)
            self.assertTrue(report["complete"])
            self.assertEqual(report["counts"]["recovered"], 128)
            self.assertEqual(report["dataset"]["filters"], [3, 1])
            self.assertEqual(set(report["dataset"]["attributes_omitted"]),
                             {"Xlabel", "Xunits", "Ylabel", "Yunits"})
            self.assertEqual(json.loads(report_path.read_text()), report)
            with h5py.File(SOURCE, "r") as truth, h5py.File(output, "r") as result:
                original = truth["/strain/Strain"]
                exported = result["/strain/Strain"]
                self.assertEqual(exported.dtype, np.dtype("<f8"))
                self.assertEqual(exported.shape, original.shape)
                self.assertEqual(exported[...].view("<u8").tobytes(), original[...].view("<u8").tobytes())
                for index in (0, 32, 127):
                    _filter_mask, expected_raw = original.id.read_direct_chunk((index * 4096,))
                    _out_mask, actual_raw = exported.id.read_direct_chunk((index * 4096,))
                    self.assertEqual(actual_raw, _decode_chunk(expected_raw, read_dataset_spec(SOURCE, "/strain/Strain"), 0))
                for name in report["dataset"]["attributes_copied"]:
                    self.assertEqual(exported.attrs[name], original.attrs[name])
                np.testing.assert_array_equal(
                    result["/_h5reclaim/chunk_status"][...], np.ones(128, dtype="u1")
                )

    def test_corrupt_compressed_chunk_is_marked_decode_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            damaged = root / "damaged.h5"
            shutil.copyfile(SOURCE, damaged)
            with h5py.File(damaged, "r") as handle:
                info = handle["/strain/Strain"].id.get_chunk_info(0)
            # A change to the chunk's first zlib byte invalidates its stream;
            # it does not touch the HDF5 index, source metadata, or pristine file.
            with damaged.open("r+b") as handle:
                handle.seek(info.byte_offset)
                handle.write(b"\x00")
            analysis = analyze(damaged, "/strain/Strain")
            self.assertEqual(analysis.report["counts"]["decode_failed"], 1)
            self.assertEqual(analysis.report["counts"]["recovered"], 127)
            self.assertEqual(analysis.report["failed_chunks"][0]["chunk_index"], [0])
            output = root / "out.h5"
            report_path = root / "report.json"
            recover(damaged, "/strain/Strain", output, report_path)
            with h5py.File(output, "r") as handle:
                self.assertEqual(int(handle["/_h5reclaim/chunk_status"][0]), STATUS_CODES["decode_failed"])
                self.assertEqual(int(handle["/_h5reclaim/chunk_status"][1]), STATUS_CODES["recovered"])


if __name__ == "__main__":
    unittest.main()
