"""External raw export checks actual element positions and safe refusals."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.dependency_routes import DependencyError
from h5reclaim.external_raw_export import export_external_raw
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.recovery import RecoveryError


def _manifest(*entries: tuple[str, Path]) -> dict:
    return {"schema_version": 1, "files": [
        {"declared_name": name, "path": str(path),
         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for name, path in entries
    ]}


class ExternalRawExportTests(unittest.TestCase):
    def test_single_external_file_is_pinned_and_never_ambiently_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main, supplied = root / "main.h5", root / "other-name.bin"
            values = np.array([8, -12, 44, 999], dtype="<i4")
            supplied.write_bytes(values.tobytes())
            with h5py.File(main, "x") as handle:
                ds = handle.create_dataset("experiment/readings", shape=(4,), dtype="<i4",
                                           external=[("unavailable-declared.bin", 0, 16)])
                ds.attrs["unit"] = np.bytes_("m/s")
            before_main, before_raw = main.read_bytes(), supplied.read_bytes()
            output, report_path = root / "out.h5", root / "report.json"
            report = export_external_raw(
                main, "/experiment/readings",
                _manifest(("unavailable-declared.bin", supplied)), output, report_path,
                published_output=root / "eventual-public.h5",
            )
            self.assertEqual(report["mode"], "external_raw_export")
            self.assertEqual(report["outcome"], "complete")
            self.assertEqual(report["accepted_elements"], 4)
            self.assertFalse(report["historical_values_verified"])
            self.assertEqual(report["output_path"], str(root / "eventual-public.h5"))
            self.assertEqual(report["external_segments"][0]["physical_byte_range"], [0, 16])
            self.assertEqual(report["external_segments"][0]["present_raw_sha256"],
                             hashlib.sha256(before_raw).hexdigest())
            self.assertEqual(json.loads(report_path.read_text()), report)
            with h5py.File(output, "r") as handle:
                self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()])["output_path"],
                                 str(root / "eventual-public.h5"))
                np.testing.assert_array_equal(handle["/experiment/readings"][:], values)
                np.testing.assert_array_equal(handle["/_h5reclaim/validity"][:], [1, 1, 1, 1])
                self.assertEqual(handle["experiment/readings"].attrs["unit"], b"m/s")
                self.assertEqual(handle["/experiment/readings"].id.get_create_plist().get_external_count(), 0)
            self.assertEqual(main.read_bytes(), before_main)
            self.assertEqual(supplied.read_bytes(), before_raw)
            self.assertFalse((root / "unavailable-declared.bin").exists())

    def test_multifile_byte_stream_can_split_an_element(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main, left, right = root / "main.h5", root / "left.bin", root / "right.bin"
            values = np.array([0x12345678, 0x75432101, -321, 82], dtype=">i4")
            raw = values.tobytes()
            left.write_bytes(b"HEAD!" + raw[:5] + b"unused")
            right.write_bytes(b"xx" + raw[5:] + b"tail")
            with h5py.File(main, "x") as handle:
                handle.create_dataset("measurement", shape=(2, 2), dtype=">i4",
                                      external=[("part-one", 5, 5), ("part-two", 2, 11)])
            output, report_path = root / "out.h5", root / "report.json"
            report = export_external_raw(main, "/measurement",
                                         _manifest(("part-one", left), ("part-two", right)),
                                         output, report_path)
            self.assertEqual(report["accepted_elements"], 4)
            self.assertEqual(report["external_segments"][1]["logical_byte_range"], [5, 16])
            with h5py.File(output, "r") as handle:
                self.assertTrue(handle["measurement"].id.get_type().equal(
                    h5py.h5t.py_create(np.dtype(">i4"))))
                np.testing.assert_array_equal(handle["measurement"][:], values.reshape(2, 2))
                np.testing.assert_array_equal(handle["_h5reclaim/validity"][:], np.ones((2, 2), dtype="u1"))

    def test_short_source_marks_partial_and_cross_segment_elements_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main, left, right = root / "main.h5", root / "left.bin", root / "right.bin"
            values = np.array([11, 22, 33, 44], dtype="<i4")
            raw = values.tobytes()
            left.write_bytes(raw[:7])  # Segment claims 9 bytes; two are missing.
            right.write_bytes(raw[9:])
            with h5py.File(main, "x") as handle:
                handle.create_dataset("measurement", shape=(4,), dtype="<i4",
                                      external=[("first", 0, 9), ("second", 0, 7)])
            output, report_path = root / "out.h5", root / "report.json"
            report = export_external_raw(main, "/measurement",
                                         _manifest(("first", left), ("second", right)),
                                         output, report_path)
            self.assertEqual((report["outcome"], report["accepted_elements"], report["unknown_elements"]),
                             ("partial", 2, 2))
            self.assertEqual(report["external_segments"][0]["present_logical_byte_range"], [0, 7])
            self.assertEqual(report["dependency_validation"]["references"][0]["status"],
                             "declared_raw_range_missing")
            with h5py.File(output) as handle:
                np.testing.assert_array_equal(handle["_h5reclaim/validity"][:], [1, 0, 0, 1])
                np.testing.assert_array_equal(handle["measurement"][:][[0, 3]], values[[0, 3]])

    def test_missing_related_file_allows_only_present_segment(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main, part = root / "main.h5", root / "supplied.bin"
            part.write_bytes(np.array([10, 20], dtype="<i4").tobytes())
            with h5py.File(main, "x") as handle:
                handle.create_dataset("measurement", shape=(4,), dtype="<i4",
                                      external=[("first", 0, 8), ("missing", 0, 8)])
            output, report_path = root / "out.h5", root / "report.json"
            report = export_external_raw(main, "/measurement", _manifest(("first", part)),
                                         output, report_path)
            self.assertEqual(report["outcome"], "partial")
            self.assertEqual(report["accepted_elements"], 2)
            self.assertEqual(report["external_segments"][1]["status"], "unavailable")
            with h5py.File(output) as handle:
                np.testing.assert_array_equal(handle["_h5reclaim/validity"][:], [1, 1, 0, 0])
                np.testing.assert_array_equal(handle["measurement"][:2], [10, 20])

    def test_unlimited_final_segment_preserves_maxshape_with_local_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main, part = root / "main.h5", root / "part.bin"
            values = np.arange(12, dtype="<u2").reshape((3, 4))
            part.write_bytes(values.tobytes())
            with h5py.File(main, "x") as handle:
                creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                creation.set_external(b"source", 0, h5py.h5f.UNLIMITED)
                space = h5py.h5s.create_simple((3, 4), (h5py.h5s.UNLIMITED, 4))
                dataset = h5py.h5d.create(handle.id, b"measurement", h5py.h5t.STD_U16LE, space, dcpl=creation)
                dataset.close()
            output, report_path = root / "out.h5", root / "report.json"
            report = export_external_raw(main, "/measurement", _manifest(("source", part)),
                                         output, report_path)
            self.assertEqual(report["accepted_elements"], 12)
            with h5py.File(output) as handle:
                self.assertEqual(handle["measurement"].maxshape, (None, 4))
                self.assertIsNotNone(handle["measurement"].chunks)
                np.testing.assert_array_equal(handle["measurement"][:], values)

    def test_fixed_record_schema_and_exact_bits_survive(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main, part = root / "main.h5", root / "records.bin"
            dtype = np.dtype([("time", ">f8"), ("sample", "<i2"), ("label", "S5")])
            values = np.array([(1.25, -12, b"alpha"), (-0.0, 7, b"beta")], dtype=dtype)
            part.write_bytes(values.tobytes())
            with h5py.File(main, "x") as handle:
                handle.create_dataset("records", shape=(2,), dtype=dtype,
                                      external=[("external-records", 0, len(values.tobytes()))])
            output, report_path = root / "out.h5", root / "report.json"
            result = export_external_raw(main, "/records", _manifest(("external-records", part)),
                                         output, report_path)
            self.assertEqual(result["accepted_elements"], 2)
            with h5py.File(main) as source, h5py.File(output) as derived:
                self.assertTrue(derived["records"].id.get_type().equal(source["records"].id.get_type()))
                self.assertEqual(derived["records"][:].tobytes(), values.tobytes())

    def test_tall_matrix_uses_bounded_contiguous_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main, part = root / "main.h5", root / "matrix.bin"
            values = np.arange(3000, dtype="<i4").reshape(3000, 1)
            part.write_bytes(values.tobytes())
            with h5py.File(main, "x") as handle:
                handle.create_dataset("matrix", shape=values.shape, dtype=values.dtype,
                                      external=[("matrix-raw", 0, values.nbytes)])
            output, report_path = root / "out.h5", root / "report.json"
            from h5reclaim import external_raw_export as module
            with patch.object(module, "_block_bytes", wraps=module._block_bytes) as blocks:
                result = export_external_raw(main, "/matrix", _manifest(("matrix-raw", part)),
                                             output, report_path)
            self.assertEqual(result["accepted_elements"], 3000)
            self.assertLessEqual(blocks.call_count, 2)  # one write pass, one readback pass
            with h5py.File(output) as handle:
                np.testing.assert_array_equal(handle["matrix"][:], values)

    def test_mismatched_manifest_hash_or_overlapping_ranges_never_publish(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main, part = root / "main.h5", root / "part.bin"
            part.write_bytes(np.arange(4, dtype="<i4").tobytes())
            with h5py.File(main, "x") as handle:
                handle.create_dataset("measurement", shape=(4,), dtype="<i4",
                                      external=[("first", 0, 8), ("second", 4, 8)])
            output, report_path = root / "out.h5", root / "report.json"
            manifest = _manifest(("first", part), ("second", part))
            with self.assertRaisesRegex(UnsupportedCase, "same physical source bytes"):
                export_external_raw(main, "/measurement", manifest, output, report_path)
            self.assertFalse(output.exists() or report_path.exists())
            manifest["files"][0]["sha256"] = "0" * 64
            with self.assertRaisesRegex(DependencyError, "hash_mismatch"):
                export_external_raw(main, "/measurement", manifest, output, report_path)
            self.assertFalse(output.exists() or report_path.exists())

    def test_source_changed_after_snapshot_prevents_publication(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main, part = root / "main.h5", root / "part.bin"
            part.write_bytes(np.arange(4, dtype="<i4").tobytes())
            with h5py.File(main, "x") as handle:
                handle.create_dataset("measurement", shape=(4,), dtype="<i4",
                                      external=[("source", 0, 16)])
            output, report_path = root / "out.h5", root / "report.json"
            from h5reclaim import external_raw_export as module
            original = module._block_bytes
            changed = False

            def change_original(*args, **kwargs):
                nonlocal changed
                if not changed:
                    changed = True
                    part.write_bytes(b"new!" + part.read_bytes()[4:])
                return original(*args, **kwargs)

            with patch.object(module, "_block_bytes", side_effect=change_original):
                with self.assertRaisesRegex(RecoveryError, "changed"):
                    export_external_raw(main, "/measurement", _manifest(("source", part)),
                                        output, report_path)
            self.assertFalse(output.exists() or report_path.exists())

    def test_no_pinned_complete_element_is_safe_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main, part = root / "main.h5", root / "part.bin"
            part.write_bytes(b"abc")
            with h5py.File(main, "x") as handle:
                handle.create_dataset("measurement", shape=(2,), dtype="<i4",
                                      external=[("source", 0, 8)])
            output, report_path = root / "out.h5", root / "report.json"
            with self.assertRaisesRegex(UnsupportedCase, "no complete"):
                export_external_raw(main, "/measurement", _manifest(("source", part)),
                                    output, report_path)
            self.assertFalse(output.exists() or report_path.exists())


if __name__ == "__main__":
    unittest.main()
