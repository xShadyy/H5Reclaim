"""Public recovery across codecs, multidimensional records and whole files."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.large_streaming import LargeBudget, export_large_readable
from h5reclaim.metadata import read_dataset_spec
from h5reclaim.native_io import read_fixed_block, write_fixed_block
from h5reclaim.readable_export import export_readable
from h5reclaim.recovery import RecoveryError, recover
from h5reclaim.rescue import auto_rescue
from h5reclaim.schema_codec import ChunkDecodeError, _unlzf, decode_chunk
from h5reclaim.whole_file import rescue_all
from h5reclaim.variable_readable import export_variable


class BroaderRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "source.h5"
        self.output = self.base / "output.h5"
        self.report = self.base / "report.json"

    def test_lzf_actual_legacy_and_modern_chunks_with_edge_and_checksum(self) -> None:
        values = np.tile(np.arange(9, dtype=">i2"), (11, 1))
        for index, version in enumerate((("earliest", "v108"), "latest")):
            with self.subTest(version=version):
                source = self.base / f"lzf-{index}.h5"
                output, report = self.base / f"out-{index}.h5", self.base / f"rep-{index}.json"
                with h5py.File(source, "w", libver=version) as handle:
                    handle.create_dataset("data", data=values, chunks=(4, 5), compression="lzf",
                                          shuffle=True, fletcher32=True)
                spec = read_dataset_spec(source, "/data")
                with h5py.File(source) as handle:
                    mask, stored = handle["data"].id.read_direct_chunk((0, 0))
                    decoded = np.frombuffer(decode_chunk(stored, spec, mask), dtype=values.dtype).reshape(4, 5)
                    np.testing.assert_array_equal(decoded, values[:4, :5])
                result = recover(source, "/data", output, report)
                self.assertTrue(result["complete"])
                with h5py.File(output) as handle:
                    np.testing.assert_array_equal(handle["data"][:], values)

    def test_lzf_skipped_incompressible_chunks_use_their_mask(self) -> None:
        values = np.random.default_rng(312).integers(0, 256, 1024, dtype="u1")
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("data", data=values, chunks=(32,), compression="lzf")
        spec = read_dataset_spec(self.source, "/data")
        masks = []
        with h5py.File(self.source) as handle:
            for origin in range(0, len(values), 32):
                mask, raw = handle["data"].id.read_direct_chunk((origin,))
                masks.append(mask)
                self.assertEqual(decode_chunk(raw, spec, mask), values[origin:origin + 32].tobytes())
        self.assertIn(1, masks)

    def test_lzf_overlap_and_malformed_stream_bounds(self) -> None:
        self.assertEqual(_unlzf(b"\x00A\x20\x00", 4), b"AAAA")
        for stream, cap in ((b"\x02ab", 10), (b"\x20\x00", 10), (b"\xe0", 10),
                            (b"\xe0\x01", 10), (b"\x00A\xe0\xff\x00", 16)):
            with self.subTest(stream=stream):
                with self.assertRaises(ChunkDecodeError):
                    _unlzf(stream, cap)

    def test_native_lzf_export(self) -> None:
        values = np.arange(40, dtype="<f4").reshape(5, 8)
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("data", data=values, chunks=(3, 4), compression="lzf")
        result = export_readable(self.source, "/data", self.output, self.report)
        self.assertEqual(result["accepted_elements"], values.size)
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle["data"][:], values)

    def test_native_scalar_fixed_string_preserves_declared_width(self) -> None:
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("data", data=np.asarray(b"sensor", dtype="S40"))
        result = export_readable(self.source, "/data", self.output, self.report)
        self.assertEqual(result["accepted_elements"], 1)
        with h5py.File(self.source) as original, h5py.File(self.output) as handle:
            self.assertEqual(handle["data"].shape, ())
            self.assertEqual(handle["data"].dtype, np.dtype("S40"))
            self.assertEqual(read_fixed_block(handle["data"], ()).tobytes(),
                             read_fixed_block(original["data"], ()).tobytes())

    def test_nd_sparse_streaming_counts_edge_elements_and_coordinates(self) -> None:
        with h5py.File(self.source, "w") as handle:
            data = handle.create_dataset("data", shape=(5, 7, 3), chunks=(3, 4, 2),
                                         dtype=">i2", compression="lzf", fillvalue=-1)
            data[3:5, 4:7, 2:3] = np.arange(6, dtype=">i2").reshape(2, 3, 1)
        result = export_large_readable(self.source, "/data", self.output, self.report)
        self.assertEqual(result["accepted_elements"], 6)
        self.assertEqual(result["unknown_elements"], 99)
        with h5py.File(self.output) as handle:
            self.assertEqual(handle["/_h5reclaim/validity"].shape, (2, 2, 2))
            self.assertEqual(int(np.count_nonzero(handle["/_h5reclaim/validity"][:])), 1)
            self.assertEqual(handle["/_h5reclaim/physical_evidence"][0]["origin"].tolist(), [3, 4, 2])
            np.testing.assert_array_equal(handle["data"][3:5, 4:7, 2:3], np.arange(6).reshape(2, 3, 1))

    def test_nd_contiguous_evidence_range_matches_actual_source(self) -> None:
        values = np.arange(120, dtype=">f8").reshape(3, 4, 10)
        with h5py.File(self.source, "w") as handle:
            data = handle.create_dataset("data", data=values)
            offset = data.id.get_offset()
        result = export_large_readable(self.source, "/data", self.output, self.report)
        self.assertEqual(result["dataset"]["source_contiguous_byte_range"],
                         {"start": offset, "end_exclusive": offset + values.nbytes})
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle["data"][:], values)

    def test_streaming_preserves_padded_compound_records_byte_for_byte(self) -> None:
        dtype = np.dtype({"names": ["id", "value"], "formats": ["u1", ">i2"],
                          "offsets": [0, 6], "itemsize": 12})
        expected = np.arange(144, dtype="u1").tobytes()
        with h5py.File(self.source, "w") as handle:
            data = handle.create_dataset("data", shape=(3, 4), dtype=dtype, chunks=(3, 4))
            data.id.write_direct_chunk((0, 0), expected)
        export_large_readable(self.source, "/data", self.output, self.report)
        with h5py.File(self.output) as handle:
            self.assertEqual(handle["data"].dtype, dtype)
            self.assertEqual(read_fixed_block(handle["data"], (slice(0, 3), slice(0, 4))).tobytes(), expected)

    def test_streaming_top_level_array_records(self) -> None:
        dtype = np.dtype((">i2", (2, 3)))
        expected = np.arange(24, dtype=">i2").tobytes()
        with h5py.File(self.source, "w") as handle:
            data = handle.create_dataset("data", shape=(4,), dtype=dtype, chunks=(2,))
            write_fixed_block(data, (slice(0, 4),), np.frombuffer(expected, dtype="V12").copy())
        export_large_readable(self.source, "/data", self.output, self.report)
        with h5py.File(self.output) as handle:
            self.assertEqual(read_fixed_block(handle["data"], (slice(0, 4),)).tobytes(), expected)

    def test_automatic_rank_five_streaming_with_history_map(self) -> None:
        values = np.arange(32, dtype="<i4").reshape((2,) * 5)
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("data", data=values, chunks=(1,) * 5)
        result = auto_rescue(self.source, "/data", self.output, self.report, audit_science_context=False)
        self.assertEqual(result["mode"], "native_stream_export")
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle["data"][:], values)
            self.assertEqual(handle["/_h5reclaim/historical_status"].shape, (2,) * 5)
            self.assertFalse(np.any(handle["/_h5reclaim/historical_status"][:]))

    def test_variable_strings_contiguous_chunked_and_scalar(self) -> None:
        for number, (shape, chunks) in enumerate((((3, 4), None), ((3, 4), (2, 2)), ((), None))):
            with self.subTest(shape=shape, chunks=chunks):
                source = self.base / f"strings-{number}.h5"
                output, report = self.base / f"strings-out-{number}.h5", self.base / f"strings-{number}.json"
                values = np.asarray(["", "μV", "三", "example"] * 3, dtype=object).reshape(3, 4)
                if not shape:
                    values = "μV"
                with h5py.File(source, "w") as handle:
                    handle.create_dataset("data", data=values, chunks=chunks, dtype=h5py.string_dtype())
                result = export_variable(source, "/data", output, report)
                self.assertEqual(result["outcome"], "complete")
                self.assertEqual(result["dataset"]["file_descriptor_bytes"], 16)
                with h5py.File(output) as handle:
                    np.testing.assert_array_equal(handle["data"].asstr()[()], values)
                    self.assertTrue(np.all(handle["/_h5reclaim/element_status"][()] == 1))

    def test_ragged_numeric_nd_edges_empty_sequences_and_sparse_chunks(self) -> None:
        with h5py.File(self.source, "w") as handle:
            data = handle.create_dataset("data", shape=(5, 4), chunks=(2, 3),
                                         dtype=h5py.vlen_dtype(np.dtype("<i4")))
            for index in ((0, 0), (0, 1), (1, 0), (1, 1), (1, 2), (0, 2), (4, 3)):
                data[index] = np.arange(sum(index), dtype="<i4")
        result = export_variable(self.source, "/data", self.output, self.report)
        self.assertEqual(result["accepted_elements"], 7)
        self.assertEqual(result["unknown_elements"], 13)
        with h5py.File(self.source) as original, h5py.File(self.output) as handle:
            status = handle["/_h5reclaim/element_status"][:]
            for index in np.ndindex(status.shape):
                if status[index] == 1:
                    np.testing.assert_array_equal(handle["data"][index], original["data"][index])

    def test_ragged_numeric_byte_order_preserves_current_interpretation(self) -> None:
        for number, dtype in enumerate((">i2", "<i4", ">f8")):
            with self.subTest(dtype=dtype):
                source = self.base / f"ragged-{number}.h5"
                output, report = self.base / f"ragged-out-{number}.h5", self.base / f"ragged-{number}.json"
                with h5py.File(source, "w") as handle:
                    data = handle.create_dataset("data", shape=(2,), dtype=h5py.vlen_dtype(np.dtype(dtype)))
                    data[0], data[1] = np.arange(4, dtype=dtype), np.asarray([], dtype=dtype)
                export_variable(source, "/data", output, report)
                with h5py.File(source) as original, h5py.File(output) as handle:
                    self.assertTrue(handle["data"].id.get_type().equal(original["data"].id.get_type()))
                    for index in range(2):
                        np.testing.assert_array_equal(handle["data"][index], original["data"][index])

    def test_variable_corrupt_heap_element_does_not_discard_other_values(self) -> None:
        with h5py.File(self.source, "w") as handle:
            data = handle.create_dataset("data", data=["bad", "good", "also good"],
                                         chunks=(3,), dtype=h5py.string_dtype())
            mask, raw = data.id.read_direct_chunk((0,))
            damaged = bytearray(raw)
            damaged[4:12] = b"\xff" * 8
            data.id.write_direct_chunk((0,), bytes(damaged), filter_mask=mask)
        result = auto_rescue(self.source, "/data", self.output, self.report, audit_science_context=False)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["accepted_elements"], 2)
        self.assertEqual(result["failed_elements"], 1)
        with h5py.File(self.output) as handle:
            self.assertEqual(handle["data"].asstr()[1:].tolist(), ["good", "also good"])
            self.assertEqual(handle["/_h5reclaim/element_status"][:].tolist(), [6, 1, 1])

    def test_automatic_variable_route_with_history_map(self) -> None:
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("data", data=["one", "two"], dtype=h5py.string_dtype())
        result = auto_rescue(self.source, "/data", self.output, self.report, audit_science_context=False)
        self.assertEqual(result["mode"], "native_stream_export")
        with h5py.File(self.output) as handle:
            self.assertEqual(handle["data"].asstr()[:].tolist(), ["one", "two"])
            self.assertFalse(np.any(handle["/_h5reclaim/historical_status"][:]))

    def test_streaming_budget_rejects_noninteger_counts_and_nonfinite_duration(self) -> None:
        for budget in ({"max_chunks": True}, {"max_grid": 1.5}, {"max_seconds": float("inf")},
                       {"max_seconds": float("nan")}, {"max_seconds": "10"}):
            with self.subTest(budget=budget):
                with self.assertRaises(ValueError):
                    LargeBudget(**budget)

    def _whole_source(self) -> None:
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.attrs["instrument"] = "Example instrument"
            group = handle.create_group("experiment")
            group.attrs["run"] = np.int32(7)
            time = group.create_dataset("time", data=np.arange(5, dtype="f8"))
            time.make_scale("Time")
            data = group.create_dataset("readings", data=np.arange(10, dtype="i4").reshape(5, 2),
                                        chunks=(2, 2), compression="lzf")
            data.attrs["units"] = "volts"
            data.dims[0].label = "time"
            data.dims[0].attach_scale(time)
            group["alias"] = data

    def test_whole_file_values_attributes_aliases_scales_and_rebased_reports(self) -> None:
        self._whole_source()
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = rescue_all(self.source, self.output, self.report)
        self.assertEqual(result["datasets_exported"], 2)
        self.assertEqual(result["datasets_failed"], 0)
        with h5py.File(self.output) as handle:
            self.assertEqual(handle.attrs["instrument"], "Example instrument")
            self.assertEqual(handle["experiment"].attrs["run"], 7)
            self.assertEqual(handle["experiment/readings"].attrs["units"], "volts")
            self.assertEqual(handle["experiment/readings"].id, handle["experiment/alias"].id)
            self.assertEqual(list(handle["experiment/readings"].dims[0].keys()), ["Time"])
            self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()]), result)
            for entry in result["datasets"]:
                report = entry["report"]
                prefix = report["whole_file_metadata_group"]
                self.assertEqual(json.loads(handle[prefix + "/report_json"][()]), report)
                self.assertEqual(report["source"]["path"], str(self.source))
                annotation = handle[entry["path"]].attrs.get("h5reclaim_chunk_status")
                if annotation:
                    self.assertIn(annotation, handle)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)

    def test_whole_file_continues_after_one_unexportable_dataset(self) -> None:
        self._whole_source()
        with h5py.File(self.source, "r+") as handle:
            handle.create_dataset("external", shape=(1,), dtype="i4", external=[("missing.raw", 0, 4)])
        result = rescue_all(self.source, self.output, self.report)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["datasets_exported"], 2)
        self.assertEqual(result["datasets_failed"], 1)
        self.assertEqual(result["failures"][0]["path"], "/external")

    def test_whole_file_variable_strings_and_numeric_sequences(self) -> None:
        self._whole_source()
        with h5py.File(self.source, "r+") as handle:
            handle.create_dataset("names", data=["α", "β"], dtype=h5py.string_dtype())
            data = handle.create_dataset("sequences", shape=(2,), dtype=h5py.vlen_dtype(np.dtype("f8")))
            data[0], data[1] = np.arange(3, dtype="f8"), np.asarray([], dtype="f8")
        result = rescue_all(self.source, self.output, self.report)
        self.assertEqual(result["datasets_exported"], 4)
        self.assertEqual(result["datasets_failed"], 0)
        with h5py.File(self.output) as handle:
            self.assertEqual(handle["names"].asstr()[:].tolist(), ["α", "β"])
            np.testing.assert_array_equal(handle["sequences"][0], np.arange(3))
            self.assertEqual(len(handle["sequences"][1]), 0)

    def test_whole_file_group_alias_and_cycle(self) -> None:
        self._whole_source()
        with h5py.File(self.source, "r+") as handle:
            handle["same"] = handle["experiment"]
            handle["experiment"]["root"] = handle["/"]
        result = rescue_all(self.source, self.output, self.report)
        with h5py.File(self.output) as handle:
            self.assertEqual(handle["same"].id, handle["experiment"].id)
            self.assertEqual(handle["experiment/root"].id, handle["/"].id)
        self.assertEqual(result["datasets_exported"], 2)

    def test_whole_file_no_overwrite_and_publication_rollback(self) -> None:
        self._whole_source()
        self.output.write_bytes(b"existing")
        with self.assertRaises(RecoveryError):
            rescue_all(self.source, self.output, self.report)
        self.assertEqual(self.output.read_bytes(), b"existing")
        self.output.unlink()
        from h5reclaim import whole_file
        original = whole_file.os.link

        def fail_output(source, destination):
            if Path(destination) == self.output:
                raise OSError("publication fault")
            return original(source, destination)

        with patch("h5reclaim.whole_file.os.link", side_effect=fail_output):
            with self.assertRaisesRegex(OSError, "publication fault"):
                rescue_all(self.source, self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_public_cli_defaults_to_whole_file(self) -> None:
        self._whole_source()
        completed = subprocess.run([sys.executable, "-m", "h5reclaim", "rescue", str(self.source),
                                    "--output", str(self.output), "--report", str(self.report)],
                                   capture_output=True, text=True, timeout=40)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("2/2 datasets exported", completed.stdout)
        self.assertEqual(json.loads(self.report.read_text())["mode"], "whole_file_recovery")


if __name__ == "__main__":
    unittest.main()
