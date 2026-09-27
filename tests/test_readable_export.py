"""Readable copying must not be mistaken for structural repair or source truth."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.hints import HintsError, parse_hints
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.readable_export import MAX_BLOCK_BYTES, MAX_DATA_BYTES, export_readable
from h5reclaim.recovery import RecoveryError


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReadableExportTests(unittest.TestCase):
    def _paths(self, folder: Path) -> tuple[Path, Path, Path]:
        return folder / "source.h5", folder / "output.h5", folder / "report.json"

    def test_compressed_partial_edge_chunks_and_nested_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            # NaN payload and signed negative zero make numeric equality too weak.
            bits = (np.arange(35, dtype="<u8") * 13).reshape((5, 7))
            bits[0, 0] = 0x7ff800000000a51f
            bits[0, 1] = 0x8000000000000000
            with h5py.File(source, "x") as handle:
                selected = handle.create_dataset(
                    "/experiment/strain", data=bits.view("<f8"), chunks=(2, 3),
                    compression="gzip", shuffle=True, fletcher32=True,
                )
                selected.attrs["Npoints"] = 35
                selected.attrs["units"] = np.bytes_("counts")
                selected.attrs["array_omitted"] = np.array([1, 2])
                handle.create_dataset("/distractor", data=np.arange(5))
            original_digest = digest(source)
            result = export_readable(source, "/experiment/strain", output, report)
            self.assertEqual(result["mode"], "readable_export")
            self.assertTrue(result["native_worker"]["dynamic_plugins_disabled"])
            self.assertEqual(result["native_worker"]["wall_time_seconds"], 900)
            self.assertEqual(result["dataset"]["allocated_chunks_checked"], 9)
            self.assertIn("array_omitted", result["dataset"]["attributes_copied"])
            self.assertEqual(result["source"]["sha256_after"], original_digest)
            self.assertEqual(json.loads(report.read_text()), result)
            with h5py.File(output, "r") as handle:
                copied = handle["/experiment/strain"]
                self.assertEqual(copied[...].view("<u8").tobytes(), bits.tobytes())
                self.assertEqual(copied.attrs["units"], b"counts")
                np.testing.assert_array_equal(copied.attrs["array_omitted"], [1, 2])
                self.assertEqual(copied.chunks, (2, 3))
                self.assertEqual(copied.compression, "gzip")
                self.assertTrue(copied.fletcher32)
                self.assertEqual(handle["/_h5reclaim/validity"][...].tolist(),
                                 [[1, 1, 1], [1, 1, 1], [1, 1, 1]])
                self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()]), result)
                self.assertNotIn("distractor", handle)
            self.assertEqual(digest(source), original_digest)

    def test_ordinary_contiguous_and_compact_numeric_layouts(self) -> None:
        for layout in ("contiguous", "compact"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as directory:
                source, output, report = self._paths(Path(directory))
                values = np.arange(42, dtype=">i2").reshape(6, 7)
                with h5py.File(source, "x") as handle:
                    if layout == "compact":
                        creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                        creation.set_layout(h5py.h5d.COMPACT)
                        dataspace = h5py.h5s.create_simple(values.shape)
                        low = h5py.h5d.create(handle.id, b"values", h5py.h5t.STD_I16BE,
                                              dataspace, dcpl=creation)
                        low.write(h5py.h5s.ALL, h5py.h5s.ALL, values)
                        low.close()
                    else:
                        handle.create_dataset("values", data=values)
                result = export_readable(source, "/values", output, report)
                self.assertEqual(result["dataset"]["layout"], layout)
                if layout == "contiguous":
                    self.assertEqual(result["dataset"]["source_contiguous_raw_sha256"],
                                     hashlib.sha256(values.tobytes()).hexdigest())
                    self.assertEqual(result["dataset"]["source_contiguous_byte_range"]["end_exclusive"]
                                     - result["dataset"]["source_contiguous_byte_range"]["start"],
                                     values.nbytes)
                else:
                    self.assertIsNone(result["dataset"]["source_contiguous_byte_range"])
                with h5py.File(output, "r") as handle:
                    self.assertEqual(handle["/values"].dtype, np.dtype(">i2"))
                    self.assertEqual(handle["/values"][...].tobytes(), values.tobytes())

    def test_byte_sized_integer_types_and_blocked_reads(self) -> None:
        for datatype in ("i1", "u1"):
            with self.subTest(datatype=datatype), tempfile.TemporaryDirectory() as directory:
                source, output, report = self._paths(Path(directory))
                values = np.arange(MAX_BLOCK_BYTES + 37, dtype=datatype)
                with h5py.File(source, "x") as handle:
                    handle.create_dataset("values", data=values)
                result = export_readable(source, "/values", output, report)
                self.assertGreater(result["blocks_verified"], 1)
                with h5py.File(output, "r") as handle:
                    self.assertEqual(handle["/values"][...].tobytes(), values.tobytes())

    def test_fixed_size_real_world_representations_keep_type_and_native_bits(self) -> None:
        values = {
            "boolean": np.array([True, False, True], dtype=bool),
            "complex": np.array([1 + 2j, -2 + 3j], dtype="<c8"),
            "enum": np.array([1, 0, 1], dtype=h5py.enum_dtype({"off": 0, "on": 1}, basetype="u1")),
            "fixed_string": np.array([b"A\x00B", b"sample"], dtype="S8"),
            "opaque": np.array([b"ABCD", b"EFGH"], dtype="V4"),
            "compound": np.array(
                [(123, b"strain", [7, 8]), (456, b"angle", [9, 10])],
                dtype=[("channel", ">u2"), ("name", "S8"), ("pair", "<u2", (2,))],
            ),
        }
        for kind, payload in values.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                source, output, report = self._paths(Path(directory))
                with h5py.File(source, "x") as handle:
                    selected = handle.create_dataset("/session/values", data=payload, chunks=(2,),
                                                     maxshape=(None,))
                    selected.attrs["channel_ids"] = np.array([2, 4, 6], dtype="<u2")
                source_hash = digest(source)
                result = export_readable(source, "/session/values", output, report)
                self.assertEqual(result["outcome"], "complete")
                self.assertEqual(result["dataset"]["maxshape"], [None])
                self.assertIn("channel_ids", result["dataset"]["attributes_copied"])
                with h5py.File(source) as original, h5py.File(output) as exported:
                    before, after = original["/session/values"], exported["/session/values"]
                    self.assertEqual(after.dtype, before.dtype)
                    self.assertEqual(after.maxshape, before.maxshape)
                    self.assertEqual(after.chunks, before.chunks)
                    self.assertEqual(after[...].tobytes(), before[...].tobytes())
                    np.testing.assert_array_equal(after.attrs["channel_ids"], [2, 4, 6])
                self.assertEqual(source_hash, digest(source))

    def test_sparse_growing_filtered_dataset_keeps_fill_distinct_from_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                selected = handle.create_dataset("values", shape=(11,), maxshape=(None,), chunks=(3,),
                                                 dtype=">i2", fillvalue=-9, compression="gzip",
                                                 shuffle=True, fletcher32=True)
                selected[:3] = [1, 2, 3]
                selected[9:11] = [99, 100]
            result = export_readable(source, "/values", output, report)
            self.assertEqual(result["outcome"], "partial")
            self.assertEqual((result["accepted_elements"], result["unknown_elements"]), (5, 6))
            self.assertEqual(result["dataset"]["filters_in_order"], [2, 1, 3])
            self.assertTrue(all(len(record["raw_sha256"]) == 64
                                for record in result["dataset"]["source_chunk_records"]))
            with h5py.File(output) as handle:
                copied = handle["/values"]
                self.assertEqual(copied.maxshape, (None,))
                self.assertEqual(copied.chunks, (3,))
                self.assertEqual(copied.fillvalue, -9)
                self.assertEqual(copied.dtype, np.dtype(">i2"))
                self.assertEqual(copied[...].tolist(), [1, 2, 3, -9, -9, -9, -9, -9, -9, 99, 100])
                self.assertEqual(handle["/_h5reclaim/validity"][...].tolist(), [1, 0, 0, 1])

    def test_optional_filter_mask_is_reported_per_source_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            values = np.arange(4, dtype="<u4")
            with h5py.File(source, "x") as handle:
                selected = handle.create_dataset("values", shape=(4,), chunks=(4,), dtype="<u4",
                                                 compression="gzip")
                selected.id.write_direct_chunk((0,), values.tobytes(), filter_mask=1)
            result = export_readable(source, "/values", output, report)
            self.assertEqual(result["dataset"]["source_chunk_records"][0]["filter_mask"], 1)
            with h5py.File(output) as handle:
                np.testing.assert_array_equal(handle["/values"][...], values)

    def test_sparse_chunked_dataset_has_unknown_validity_not_accepted_fill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                selected = handle.create_dataset("values", shape=(20,), chunks=(5,),
                                                 dtype="<u4", fillvalue=0)
                selected[0:5] = np.arange(5, dtype="<u4")
                self.assertTrue(np.all(selected[5:] == 0))
            before = digest(source)
            result = export_readable(source, "/values", output, report)
            self.assertEqual(digest(source), before)
            self.assertEqual(result["outcome"], "partial")
            self.assertEqual((result["accepted_elements"], result["unknown_elements"]), (5, 15))
            self.assertEqual(result["dataset"]["unknown_chunk_origins"], [[5], [10], [15]])
            with h5py.File(output, "r") as handle:
                self.assertEqual(handle["/values"][...].tolist(), list(range(5)) + [0] * 15)
                self.assertEqual(handle["/_h5reclaim/validity"][...].tolist(), [1, 0, 0, 0])
            self.assertEqual(json.loads(report.read_text()), result)

    def test_compressed_chunk_with_invalid_checksum_has_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                selected = handle.create_dataset("values", data=np.arange(40, dtype="<f8"),
                                                 chunks=(10,), fletcher32=True)
                location = selected.id.get_chunk_info_by_coord((0,)).byte_offset
            image = bytearray(source.read_bytes())
            image[location + 3] ^= 0x10
            source.write_bytes(image)
            before = digest(source)
            with self.assertRaises(RecoveryError):
                export_readable(source, "/values", output, report)
            self.assertEqual(digest(source), before)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_soft_external_and_virtual_links_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output, report = self._paths(base)
            external = base / "external.h5"
            with h5py.File(external, "x") as handle:
                handle.create_dataset("values", data=np.arange(4))
            with h5py.File(source, "x") as handle:
                handle.create_dataset("plain", data=np.arange(4))
                handle["soft"] = h5py.SoftLink("/plain")
                handle["external"] = h5py.ExternalLink(str(external), "/values")
                layout = h5py.VirtualLayout(shape=(4,), dtype="<i8")
                layout[:] = h5py.VirtualSource(str(external), "values", shape=(4,))
                handle.create_virtual_dataset("virtual", layout)
            for selected in ("/soft", "/external", "/virtual"):
                with self.subTest(selected=selected):
                    with self.assertRaises(UnsupportedCase):
                        export_readable(source, selected, output, report)
                    self.assertFalse(output.exists())
                    self.assertFalse(report.exists())

    def test_unsupported_types_and_incomplete_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                handle.create_dataset("strings", data=np.array(["hello"], dtype="S5"))
                handle.create_dataset("vlen", shape=(2,), dtype=h5py.vlen_dtype(np.dtype("<i4")))
                handle.create_dataset("vlen_strings", data=["hello"], dtype=h5py.string_dtype("utf-8"))
                handle.create_dataset("references", shape=(2,), dtype=h5py.ref_dtype)
                handle.create_dataset("nested_references", shape=(2,),
                                      dtype=np.dtype([("measurement", "<f4"), ("pointer", h5py.ref_dtype)]))
                handle.create_dataset("compound", data=np.array([(1, 2)], dtype=[("x", "i4"), ("y", "i4")]))
                handle.create_dataset("unwritten", shape=(8,), dtype="<u4")
                handle.create_dataset("large", shape=(MAX_BLOCK_BYTES // 8 + 1,),
                                      chunks=(MAX_BLOCK_BYTES // 8 + 1,), dtype="<f8")
            for selected in ("/vlen", "/vlen_strings", "/nested_references",
                             "/unwritten", "/large"):
                with self.subTest(selected=selected):
                    with self.assertRaises(UnsupportedCase):
                        export_readable(source, selected, output, report)
                    self.assertFalse(output.exists())
                    self.assertFalse(report.exists())
            for selected in ("/strings", "/compound"):
                with self.subTest(selected=selected):
                    result = export_readable(source, selected, output, report)
                    self.assertEqual(result["outcome"], "complete")
                    with h5py.File(source) as old, h5py.File(output) as new:
                        self.assertEqual(old[selected][...].tobytes(), new[selected][...].tobytes())
                    output.unlink()
                    report.unlink()

    def test_reduced_precision_integer_is_copied_but_unbounded_size_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                lowered = h5py.h5t.STD_U32LE.copy()
                lowered.set_precision(24)
                created = h5py.h5d.create(handle.id, b"short_precision", lowered,
                                          h5py.h5s.create_simple((2,)))
                created.write(h5py.h5s.ALL, h5py.h5s.ALL,
                              np.array([0x123456, 0xabcdef], dtype="<u4"))
                created.close()
                handle.create_dataset("huge_sparse", shape=(MAX_DATA_BYTES // 8 + 1,),
                                      chunks=(1024,), dtype="<f8")
            export_readable(source, "/short_precision", output, report)
            with h5py.File(output, "r") as handle:
                self.assertEqual(handle["short_precision"][...].tolist(), [0x123456, 0xabcdef])
                self.assertEqual(handle["short_precision"].id.get_type().get_precision(), 24)
            output.unlink()
            report.unlink()
            with self.assertRaisesRegex(UnsupportedCase, "512 MiB"):
                export_readable(source, "/huge_sparse", output, report)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_builtin_nbit_and_scaleoffset_keep_observed_values_and_pipeline(self) -> None:
        for kind in ("nbit", "scaleoffset"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                source, output, report = self._paths(Path(directory))
                with h5py.File(source, "x") as handle:
                    if kind == "nbit":
                        properties = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                        properties.set_chunk((5,))
                        properties.set_filter(h5py.h5z.FILTER_NBIT,
                                              h5py.h5z.FLAG_MANDATORY, ())
                        datatype = h5py.h5t.STD_U16LE.copy()
                        datatype.set_precision(12)
                        created = h5py.h5d.create(handle.id, b"values", datatype,
                                                  h5py.h5s.create_simple((20,)),
                                                  dcpl=properties)
                        created.write(h5py.h5s.ALL, h5py.h5s.ALL,
                                      np.arange(20, dtype="<u2"))
                        created.close()
                    else:
                        handle.create_dataset(
                            "values", data=np.array([0.101, 0.205, 1.517, 2.993], dtype="<f8"),
                            chunks=(2,), scaleoffset=2,
                        )
                    expected = handle["/values"][...].tobytes()
                    original_filters = [handle["/values"].id.get_create_plist().get_filter(i)
                                        for i in range(handle["/values"].id.get_create_plist().get_nfilters())]
                before = digest(source)
                result = export_readable(source, "/values", output, report)
                self.assertEqual(digest(source), before)
                self.assertEqual(result["outcome"], "complete")
                with h5py.File(output, "r") as handle:
                    copied = handle["/values"]
                    self.assertEqual(copied[...].tobytes(), expected)
                    self.assertEqual([copied.id.get_create_plist().get_filter(i)
                                      for i in range(copied.id.get_create_plist().get_nfilters())],
                                     original_filters)
                if kind == "scaleoffset":
                    self.assertIn("lossy", result["dataset"]["filter_semantics"])
                else:
                    self.assertIsNone(result["dataset"]["filter_semantics"])

    def test_bitfield_preserves_native_values_and_hdf5_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                original = h5py.h5d.create(handle.id, b"bits", h5py.h5t.STD_B16LE,
                                           h5py.h5s.create_simple((3,)))
                original.write(h5py.h5s.ALL, h5py.h5s.ALL,
                               np.array([0x0012, 0x8000, 0xffff], dtype="<u2"))
                original.close()
            result = export_readable(source, "/bits", output, report)
            self.assertEqual(result["outcome"], "complete")
            with h5py.File(source, "r") as original, h5py.File(output, "r") as exported:
                self.assertEqual(exported["/bits"].id.get_type().get_class(), h5py.h5t.BITFIELD)
                self.assertEqual(exported["/bits"][...].tobytes(),
                                 original["/bits"][...].tobytes())

    def test_szip_uses_only_compiled_in_encoder_and_decoder(self) -> None:
        if (not h5py.h5z.filter_avail(h5py.h5z.FILTER_SZIP)
                or h5py.h5z.get_filter_info(h5py.h5z.FILTER_SZIP) & 3 != 3):
            self.skipTest("SZIP encoder and decoder are not built into this HDF5")
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                handle.create_dataset("values", data=np.arange(64, dtype="<u2"),
                                      chunks=(32,), compression="szip",
                                      compression_opts=("ec", 8))
            result = export_readable(source, "/values", output, report)
            self.assertEqual(result["dataset"]["filters_in_order"], [h5py.h5z.FILTER_SZIP])
            with h5py.File(output, "r") as handle:
                np.testing.assert_array_equal(handle["/values"][...], np.arange(64))

    def test_self_object_references_are_remapped_with_sparse_validity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                selected = handle.create_dataset("refs", shape=(5,), dtype=h5py.ref_dtype,
                                                 chunks=(2,))
                selected[0:2] = [selected.ref, h5py.Reference()]
                selected[4] = selected.ref
            before = digest(source)
            result = export_readable(source, "/refs", output, report)
            self.assertEqual(digest(source), before)
            self.assertEqual(result["outcome"], "partial")
            self.assertEqual((result["accepted_elements"], result["unknown_elements"]), (3, 2))
            self.assertEqual(result["dataset"]["reference_mapping"]["selected_dataset_count"], 2)
            self.assertEqual(result["dataset"]["reference_mapping"]["null_count"], 1)
            with h5py.File(output, "r") as handle:
                selected = handle["/refs"]
                self.assertTrue(bool(selected[0]))
                self.assertEqual(handle[selected[0]].name, "/refs")
                self.assertFalse(bool(selected[1]))
                self.assertEqual(handle[selected[4]].name, "/refs")
                self.assertEqual(handle["/_h5reclaim/validity"][...].tolist(), [1, 0, 1])

    def test_selected_dataset_region_references_keep_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                selected = handle.create_dataset("regions", shape=(3,), dtype=h5py.regionref_dtype)
                selected[0] = selected.regionref[0:2]
                selected[1] = h5py.RegionReference()
                selected[2] = selected.regionref[2:3]
            before = digest(source)
            result = export_readable(source, "/regions", output, report)
            self.assertEqual(digest(source), before)
            self.assertEqual(result["dataset"]["reference_mapping"]["kind"], "top_level_region")
            self.assertEqual(result["dataset"]["reference_mapping"]["selected_dataset_count"], 2)
            with h5py.File(source, "r") as old, h5py.File(output, "r") as new:
                for index in (0, 2):
                    old_ref, new_ref = old["/regions"][index], new["/regions"][index]
                    self.assertEqual(new[new_ref].name, "/regions")
                    self.assertEqual(h5py.h5r.get_region(old_ref, old.id).encode(),
                                     h5py.h5r.get_region(new_ref, new.id).encode())
                self.assertFalse(bool(new["/regions"][1]))

    def test_reference_to_other_object_and_region_reference_are_refused(self) -> None:
        for kind in ("outside", "dangling", "region"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                source, output, report = self._paths(Path(directory))
                with h5py.File(source, "x") as handle:
                    if kind == "region":
                        other = handle.create_dataset("other", data=[1])
                        selected = handle.create_dataset("refs", shape=(1,),
                                                         dtype=h5py.regionref_dtype)
                        selected[0] = other.regionref[0:1]
                    else:
                        other = handle.create_dataset("other", data=[1])
                        selected = handle.create_dataset("refs", shape=(1,), dtype=h5py.ref_dtype)
                        selected[0] = other.ref
                        if kind == "dangling":
                            del handle["other"]
                before = digest(source)
                with self.assertRaises(UnsupportedCase):
                    export_readable(source, "/refs", output, report)
                self.assertEqual(digest(source), before)
                self.assertFalse(output.exists())
                self.assertFalse(report.exists())

    def test_unknown_filter_is_refused_before_any_payload_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                creation.set_chunk((3,))
                creation.set_filter(32001, h5py.h5z.FLAG_OPTIONAL, ())
                dataset = h5py.h5d.create(
                    handle.id, b"values", h5py.h5t.STD_U32LE,
                    h5py.h5s.create_simple((9,)), dcpl=creation,
                )
                dataset.close()
            with self.assertRaisesRegex(UnsupportedCase, "plugin"):
                export_readable(source, "/values", output, report)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_large_variable_length_attribute_is_not_dereferenced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                values = handle.create_dataset("values", data=np.arange(5, dtype="<u4"))
                values.attrs["heap_backed"] = "a" * (1024 * 1024)
                values.attrs["units"] = np.bytes_("counts")
            from h5py._hl.attrs import AttributeManager
            actual_read = AttributeManager.__getitem__

            def observe_read(manager: AttributeManager, name: str):
                if name == "heap_backed":
                    raise AssertionError("heap-backed value was dereferenced")
                return actual_read(manager, name)

            with patch.object(AttributeManager, "__getitem__", observe_read):
                result = export_readable(source, "/values", output, report)
            self.assertIn("heap_backed", result["dataset"]["attributes_omitted"])
            self.assertIn("units", result["dataset"]["attributes_copied"])

    def test_changed_source_is_refused_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                handle.create_dataset("values", data=np.arange(10))
            from h5reclaim import readable_export as module
            actual_verify = module._verify_source
            def mutate_and_verify(path: Path, identity: tuple[int, ...], original_hash: str) -> None:
                with source.open("ab") as handle:
                    handle.write(b"changed")
                actual_verify(path, identity, original_hash)
            with patch("h5reclaim.readable_export._verify_source", side_effect=mutate_and_verify):
                with self.assertRaises(RecoveryError):
                    export_readable(source, "/values", output, report)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())
            self.assertFalse(list(Path(directory).glob(".h5reclaim-*")))

    def test_hint_matches_are_recorded_as_assertions_and_conflicts_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                handle.create_dataset("values", data=np.arange(12, dtype="<u4"), chunks=(4,))
            correct = parse_hints(json.dumps({
                "schema_version": 1,
                "dataset": {"path": "/values", "shape": [12], "chunks": [4],
                            "dtype": "<u4", "filters": []},
                "source_sha256": digest(source), "note": "From an acquisition log",
            }).encode())
            result = export_readable(source, "/values", output, report, hints=correct)
            self.assertEqual(result["operator_hints"]["trust_level"], "unverified_operator_assertion")
            self.assertTrue(all(item["status"] == "matches" for item in
                                result["operator_hints"]["comparisons"]))
            output.unlink()
            report.unlink()
            wrong = parse_hints(b'{"schema_version":1,"dataset":{"path":"/values","shape":[13]}}')
            with self.assertRaisesRegex(HintsError, "shape"):
                export_readable(source, "/values", output, report, hints=wrong)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_source_or_destination_alias_refused_and_publication_failure_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output, report = self._paths(base)
            with h5py.File(source, "x") as handle:
                handle.create_dataset("values", data=np.arange(4))
            original = digest(source)
            alias = base / "alias.h5"
            os.link(source, alias)
            with self.assertRaises(RecoveryError):
                export_readable(source, "/values", alias, report)
            with self.assertRaises(RecoveryError):
                export_readable(source, "/values", output, alias)
            self.assertEqual(digest(source), original)

            real_link = os.link
            calls = 0
            def publish_then_fail(temp: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected report publication failure")
                real_link(temp, target)
            with patch("h5reclaim.readable_export.os.link", side_effect=publish_then_fail):
                with self.assertRaises(RecoveryError):
                    export_readable(source, "/values", output, report)
            self.assertEqual(digest(source), original)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())
            self.assertFalse(list(base.glob(".h5reclaim-*")))

    def test_native_worker_timeout_does_not_publish_or_leave_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output, report = self._paths(base)
            with h5py.File(source, "x") as handle:
                handle.create_dataset("values", data=np.arange(4))
            original = digest(source)
            real_run = subprocess.run

            def hung_child(*args, **kwargs):
                self.assertEqual(kwargs["env"]["HDF5_PLUGIN_PRELOAD"], "::")
                self.assertNotIn("HDF5_PLUGIN_PATH", kwargs["env"])
                return real_run([sys.executable, "-c", "import time; time.sleep(3)"], **kwargs)

            with patch.dict(os.environ, {"HDF5_PLUGIN_PATH": str(base),
                                         "HDF5_PLUGIN_PRELOAD": "unsafe"}):
                with patch("h5reclaim.readable_export.subprocess.run", side_effect=hung_child):
                    with self.assertRaisesRegex(RecoveryError, "time limit"):
                        export_readable(source, "/values", output, report, timeout_seconds=0.05)
            self.assertEqual(digest(source), original)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())
            self.assertFalse(list(base.glob(".h5reclaim-*")))

    @unittest.skipIf(os.name == "nt", "POSIX signal test")
    def test_native_worker_crash_does_not_publish_or_leave_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output, report = self._paths(base)
            with h5py.File(source, "x") as handle:
                handle.create_dataset("values", data=np.arange(4))
            original = digest(source)
            real_run = subprocess.run

            def crashed_child(*args, **kwargs):
                return real_run([sys.executable, "-c", "import os, signal; "
                                 "os.kill(os.getpid(), signal.SIGKILL)"], **kwargs)

            with patch("h5reclaim.readable_export.subprocess.run", side_effect=crashed_child):
                with self.assertRaisesRegex(RecoveryError, "without a result"):
                    export_readable(source, "/values", output, report)
            self.assertEqual(digest(source), original)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())
            self.assertFalse(list(base.glob(".h5reclaim-*")))

    @unittest.skipIf(os.name == "nt", "POSIX address-space limit test")
    def test_worker_sets_address_space_budget_before_h5py_import(self) -> None:
        code = ("from h5reclaim.native_worker import _apply_memory_limit; "
                "import resource; _apply_memory_limit(536870912); "
                "print(resource.getrlimit(resource.RLIMIT_AS)[0])")
        result = subprocess.run([sys.executable, "-c", code], text=True,
                                capture_output=True, timeout=10, check=True)
        self.assertLessEqual(int(result.stdout.strip()), 536870912)


if __name__ == "__main__":
    unittest.main()
