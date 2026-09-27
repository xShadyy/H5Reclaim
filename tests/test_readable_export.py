"""Readable copying must not be mistaken for structural repair or source truth."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.hints import HintsError, parse_hints
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.readable_export import MAX_BLOCK_BYTES, export_readable
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
            self.assertEqual(result["dataset"]["allocated_chunks_checked"], 9)
            self.assertIn("array_omitted", result["dataset"]["attributes_omitted"])
            self.assertEqual(result["source"]["sha256_after"], original_digest)
            self.assertEqual(json.loads(report.read_text()), result)
            with h5py.File(output, "r") as handle:
                copied = handle["/experiment/strain"]
                self.assertEqual(copied[...].view("<u8").tobytes(), bits.tobytes())
                self.assertEqual(copied.attrs["units"], b"counts")
                self.assertEqual(copied.attrs["h5reclaim_mode"], "readable_export")
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

    def test_sparse_chunked_dataset_is_refused_even_if_native_reads_return_fill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = self._paths(Path(directory))
            with h5py.File(source, "x") as handle:
                selected = handle.create_dataset("values", shape=(20,), chunks=(5,),
                                                 dtype="<u4", fillvalue=0)
                selected[0:5] = np.arange(5, dtype="<u4")
                self.assertTrue(np.all(selected[5:] == 0))
            before = digest(source)
            with self.assertRaisesRegex(UnsupportedCase, "unallocated or unreachable"):
                export_readable(source, "/values", output, report)
            self.assertEqual(digest(source), before)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

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
                handle.create_dataset("references", shape=(2,), dtype=h5py.ref_dtype)
                handle.create_dataset("compound", data=np.array([(1, 2)], dtype=[("x", "i4"), ("y", "i4")]))
                handle.create_dataset("unwritten", shape=(8,), dtype="<u4")
                handle.create_dataset("large", shape=(MAX_BLOCK_BYTES // 8 + 1,),
                                      chunks=(MAX_BLOCK_BYTES // 8 + 1,), dtype="<f8")
            for selected in ("/strings", "/vlen", "/references", "/compound", "/unwritten", "/large"):
                with self.subTest(selected=selected):
                    with self.assertRaises(UnsupportedCase):
                        export_readable(source, selected, output, report)
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


if __name__ == "__main__":
    unittest.main()
