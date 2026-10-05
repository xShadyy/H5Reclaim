"""Recover fixed schemas without converting record bytes or inventing tail data."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import UnsupportedFormat
from h5reclaim.native_io import read_fixed_block
from h5reclaim.nonchunked_recovery import export_nonchunked, read_nonchunked_spec
from h5reclaim.payload_integrity import capture_element_baseline, export_verified_nonchunked
from h5reclaim.rescue import auto_rescue
from h5reclaim.chunk_truncation import recover_truncated
from h5reclaim.large_streaming import LargeBudget
from h5reclaim.source_session import reused_image, share_image
from h5reclaim.whole_file import rescue_all


class NonchunkedFixedRecordTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "source.h5"
        self.output = self.directory / "recovered.h5"
        self.report = self.directory / "report.json"

    def _create(self, dtype, *, compact=False, latest=True, shape=(4,)):
        typ = h5py.h5t.py_create(np.dtype(dtype), logical=True)
        size = typ.get_size()
        count = int(np.prod(shape, dtype=object)) if shape else 1
        raw = bytes((index * 17 + 3) % 256 for index in range(size * count))
        records = np.frombuffer(raw, dtype=f"V{size}").reshape(shape)
        with h5py.File(self.source, "w", libver="latest" if latest else "earliest") as handle:
            creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            creation.set_layout(h5py.h5d.COMPACT if compact else h5py.h5d.CONTIGUOUS)
            space = (h5py.h5s.create_simple(shape) if shape else
                     h5py.h5s.create(h5py.h5s.SCALAR))
            dataset = h5py.Dataset(h5py.h5d.create(handle.id, b"records", typ, space, dcpl=creation))
            dataset.id.write(h5py.h5s.ALL, h5py.h5s.ALL, records, mtype=typ)
        with h5py.File(self.source) as handle:
            encoding = handle["records"].id.get_type().encode()
        return raw, encoding

    def _assert_recovered(self, raw, encoding, accepted, shape=(4,)):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = export_nonchunked(self.source, "/records", self.output, self.report)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        self.assertEqual(result["accepted_elements"], accepted)
        with h5py.File(self.output) as handle:
            dataset = handle["records"]
            self.assertEqual(dataset.id.get_type().encode(), encoding)
            observed = read_fixed_block(dataset, tuple(slice(0, axis) for axis in shape)).tobytes()
            self.assertEqual(observed[:len(raw)], raw)
            self.assertEqual(observed[len(raw):], bytes(len(observed) - len(raw)))
            status = handle[result["validity"]["dataset"]][...]
            self.assertEqual(np.count_nonzero(status), accepted)
        return result

    def test_exact_schemas_and_padding_survive_both_header_generations_and_layouts(self):
        schemas = {
            "fixed_string": np.dtype("S7"),
            "enum": h5py.enum_dtype({"offline": 0, "ready": 1}, basetype=">u2"),
            "opaque": h5py.opaque_dtype(np.dtype("V5")),
            "complex": np.dtype(">c16"),
            "array": np.dtype((">i2", (2, 3))),
            "compound_padding": np.dtype({
                "names": ["sensor", "reading", "label"],
                "formats": [">i2", ">f8", "S7"],
                "offsets": [0, 4, 14], "itemsize": 24,
            }),
        }
        for name, dtype in schemas.items():
            for latest in (False, True):
                for compact in (False, True):
                    with self.subTest(schema=name, latest=latest, compact=compact):
                        for file in (self.source, self.output, self.report):
                            file.unlink(missing_ok=True)
                        raw, encoding = self._create(dtype, compact=compact, latest=latest)
                        result = self._assert_recovered(raw, encoding, 4)
                        self.assertTrue(result["complete"])
                        self.assertEqual(result["dataset"]["exact_file_datatype_sha256"],
                                         hashlib.sha256(encoding).hexdigest())

    def test_truncated_compound_retains_complete_records_and_reports_cut_record(self):
        dtype = np.dtype({"names": ["id", "label"], "formats": [">u4", "S7"],
                          "offsets": [0, 8], "itemsize": 20})
        for latest in (False, True):
            with self.subTest(latest=latest):
                for file in (self.source, self.output, self.report):
                    file.unlink(missing_ok=True)
                raw, encoding = self._create(dtype, latest=latest)
                spec = read_nonchunked_spec(self.source, "/records")
                with self.source.open("r+b") as stream:
                    stream.truncate(spec.source_absolute_offset + 2 * 20 + 11)
                result = self._assert_recovered(raw[:40], encoding, 2)
                self.assertEqual(result["unknown_elements"], 2)
                self.assertEqual(result["unassigned_fragments"][0]["size_bytes"], 11)
                self.assertEqual(result["outcome"], "partial")

    def test_rank_32_contiguous_tail_recovery_uses_original_coordinates(self):
        shape = (1,) * 30 + (2, 2)
        raw, encoding = self._create(">i4", shape=shape)
        spec = read_nonchunked_spec(self.source, "/records")
        with self.source.open("r+b") as stream:
            stream.truncate(spec.source_absolute_offset + 10)
        result = self._assert_recovered(raw[:8], encoding, 2, shape)
        self.assertEqual(result["unknown_elements"], 2)
        self.assertEqual(result["dataset"]["shape"], list(shape))

    def test_baseline_checks_full_padded_records_and_preserves_schema(self):
        dtype = np.dtype({"names": ["sample"], "formats": [">i4"],
                          "offsets": [4], "itemsize": 16})
        raw, encoding = self._create(dtype)
        baseline = self.directory / "baseline.zip"
        capture = capture_element_baseline(self.source, "/records", baseline)
        spec = read_nonchunked_spec(self.source, "/records")
        # Changing padding also breaks the independently captured record hash.
        with self.source.open("r+b") as stream:
            stream.seek(spec.source_absolute_offset + 16)
            stream.write(bytes([raw[16] ^ 1]))
        result = export_verified_nonchunked(self.source, "/records", baseline,
                    capture["archive_sha256"], self.output, self.report)
        self.assertEqual((result["accepted_elements"], result["unknown_elements"]), (3, 1))
        with h5py.File(self.output) as handle:
            self.assertEqual(handle["records"].id.get_type().encode(), encoding)
            observed = read_fixed_block(handle["records"], (slice(0, 4),)).tobytes()
            self.assertEqual(observed, raw[:16] + bytes(16) + raw[32:])

    def test_heap_pointers_are_refused_by_raw_record_route(self):
        for dtype in (h5py.string_dtype(), h5py.ref_dtype,
                      np.dtype([("label", h5py.string_dtype())])):
            with self.subTest(dtype=dtype):
                self.source.unlink(missing_ok=True)
                with h5py.File(self.source, "w", libver="latest") as handle:
                    handle.create_dataset("records", shape=(4,), dtype=dtype)
                with self.assertRaises(UnsupportedFormat):
                    export_nonchunked(self.source, "/records", self.output, self.report)
                self.assertFalse(self.output.exists() or self.report.exists())

    def test_automatic_tail_route_recovers_fixed_strings(self):
        raw, encoding = self._create("S7")
        spec = read_nonchunked_spec(self.source, "/records")
        with self.source.open("r+b") as stream:
            stream.truncate(spec.source_absolute_offset + 17)
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = auto_rescue(self.source, "/records", self.output, self.report,
                             audit_science_context=False)
        self.assertEqual((result["accepted_elements"], result["unknown_elements"]), (2, 2))
        self.assertEqual(result["automatic_routing"]["selected"], "nonchunked")
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        with h5py.File(self.output) as handle:
            self.assertEqual(handle["records"].id.get_type().encode(), encoding)
            self.assertEqual(read_fixed_block(handle["records"], (slice(0, 4),)).tobytes(),
                             raw[:14] + bytes(14))

    def test_chunk_tail_view_keeps_shared_source_image_unchanged(self):
        dtype = np.dtype([("id", ">i4"), ("label", "S8")])
        rows = np.array([(1, b"alpha"), (2, b"beta"), (3, b"gamma"), (4, b"delta")], dtype=dtype)
        with h5py.File(self.source, "w", libver="latest") as handle:
            dataset = handle.create_dataset("records", data=rows, chunks=(2,), fletcher32=True)
            encoding = dataset.id.get_type().encode()
            last = dataset.id.get_chunk_info_by_coord((2,))
        with self.source.open("r+b") as stream:
            stream.truncate(last.byte_offset + last.size // 2)
        original = self.source.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        with share_image(self.source, digest, len(original), len(original), LargeBudget()):
            result = recover_truncated(self.source, "/records", self.output, self.report)
            self.assertIsNotNone(reused_image(self.source))
            self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(result["counts"]["recovered"], 1)
        self.assertEqual(result["counts"]["unavailable"], 1)
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle["records"][:2], rows[:2])
            self.assertEqual(handle["records"].id.get_type().encode(), encoding)

    def test_baseline_output_map_does_not_collide_with_selected_namespace(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("/_h5reclaim/data", data=np.arange(4, dtype="i4"))
        baseline = self.directory / "baseline.zip"
        capture = capture_element_baseline(self.source, "/_h5reclaim/data", baseline)
        result = export_verified_nonchunked(self.source, "/_h5reclaim/data", baseline,
                    capture["archive_sha256"], self.output, self.report)
        self.assertEqual(result["accepted_elements"], 4)
        with h5py.File(self.output) as handle:
            selected = handle["/_h5reclaim/data"]
            np.testing.assert_array_equal(selected[:], np.arange(4))
            self.assertEqual(selected.attrs["h5reclaim_element_status"],
                             result["validity"]["dataset"])
            self.assertEqual(handle[result["validity"]["dataset"]].shape, (4,))

    def test_multidimensional_regions_remap_across_dataspace_encoding_versions(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            target = handle.create_dataset("target", data=np.arange(12).reshape(3, 4))
            points = target.id.get_space()
            points.select_elements([(2, 1), (0, 2)])
            point_reference = h5py.h5r.create(handle.id, b"/target", h5py.h5r.DATASET_REGION, points)
            refs = handle.create_dataset("records", shape=(4,), dtype=h5py.regionref_dtype)
            refs[:] = [target.regionref[1:3, :2], target.regionref[0:1, :],
                       point_reference, h5py.RegionReference()]
        result = rescue_all(self.source, self.output, self.report)
        self.assertEqual(result["outcome"], "complete")
        with h5py.File(self.output) as handle:
            target, refs = handle["target"], handle["records"]
            np.testing.assert_array_equal(target[refs[0]], [[4, 5], [8, 9]])
            np.testing.assert_array_equal(target[refs[1]], [[0, 1, 2, 3]])
            np.testing.assert_array_equal(target[refs[2]], [9, 2])
            self.assertFalse(refs[3])


if __name__ == "__main__":
    unittest.main()
