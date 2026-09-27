"""Independent exact-value and refusal cases for rooted nonchunked storage."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError, UnsupportedFormat
from h5reclaim.hints import DatasetHints, HintsError
from h5reclaim.metadata_fallback import _messages, _old_messages
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.nonchunked_recovery import (
    _TruncatedOldReader, export_nonchunked, read_nonchunked_spec,
)


def _compact(handle: h5py.File, name: str, values: np.ndarray) -> None:
    sid = h5py.h5s.create_simple(values.shape) if values.shape else h5py.h5s.create(h5py.h5s.SCALAR)
    type_id = h5py.h5t.py_create(values.dtype)
    creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
    creation.set_layout(h5py.h5d.COMPACT)
    selected = h5py.h5d.create(handle.id, name.encode(), type_id, sid, dcpl=creation)
    selected.write(h5py.h5s.ALL, h5py.h5s.ALL, values)
    selected.close()


def _patch_checked_object(path: Path, object_address: int, offset: int, replacement: bytes) -> None:
    raw = bytearray(path.read_bytes())
    flags = raw[object_address + 5]
    width = 1 << (flags & 3)
    content = object_address + 6 + (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0) + width
    count = int.from_bytes(raw[content - width:content], "little")
    checksum = content + count
    assert object_address <= offset and offset + len(replacement) <= checksum
    raw[offset:offset + len(replacement)] = replacement
    raw[checksum:checksum + 4] = lookup3(raw[object_address:checksum]).to_bytes(4, "little")
    path.write_bytes(raw)


class NonchunkedRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "damaged.h5"
        self.output = self.base / "output.h5"
        self.report_path = self.base / "report.json"

    def _create(self, *, latest: bool, compact: bool, dtype: str = "<i2") -> np.ndarray:
        values = np.arange(12, dtype=dtype).reshape((3, 4))
        with h5py.File(self.source, "w", libver="latest" if latest else "earliest") as handle:
            group = handle.create_group("instrument")
            if compact:
                _compact(handle, "instrument/values", values)
            else:
                group.create_dataset("values", data=values)
            group.create_dataset("distractor", data=np.full((3, 4), 33, dtype=dtype))
        return values

    def _assert_output(self, expected: np.ndarray, valid: int) -> dict:
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = export_nonchunked(self.source, "/instrument/values", self.output,
                                   self.report_path)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        self.assertEqual(result, json.loads(self.report_path.read_text()))
        self.assertEqual(result["counts"], {"recovered": valid,
                                             "unknown": expected.size - valid})
        with h5py.File(self.output) as handle:
            data = handle["/instrument/values"].astype(expected.dtype)[:]
            original = expected.tobytes(order="C")[:valid * expected.dtype.itemsize]
            self.assertEqual(data.tobytes(order="C")[:len(original)], original)
            validity = handle["/_h5reclaim/element_status"][:]
            self.assertEqual(validity.shape, expected.shape)
            np.testing.assert_array_equal(validity.flat[:valid], np.ones(valid, dtype="u1"))
            np.testing.assert_array_equal(validity.flat[valid:], np.zeros(expected.size - valid,
                                                                          dtype="u1"))
        return result

    def test_old_and_modern_compact_and_contiguous_exact_selected_values(self) -> None:
        for latest in (False, True):
            for compact in (False, True):
                with self.subTest(latest=latest, compact=compact):
                    self.source.unlink(missing_ok=True)
                    self.output.unlink(missing_ok=True)
                    self.report_path.unlink(missing_ok=True)
                    expected = self._create(latest=latest, compact=compact, dtype=">i2")
                    result = self._assert_output(expected, expected.size)
                    self.assertEqual(result["dataset"]["layout"],
                                     "compact" if compact else "contiguous")
                    with h5py.File(self.output) as handle:
                        self.assertEqual(handle["/instrument/values"].id.get_create_plist().get_layout(),
                                         h5py.h5d.COMPACT if compact else h5py.h5d.CONTIGUOUS)
                    self.assertEqual([s["name"] for s in result["metadata_resolution"]
                                      ["selected_hard_link_chain"]], ["instrument", "values"])
                    self.assertEqual(result["mappings"][0]["integrity"],
                                     "not_independently_verified")

    def test_truncated_modern_contiguous_keeps_complete_elements_only(self) -> None:
        values = self._create(latest=True, compact=False, dtype="<i4")
        spec = read_nonchunked_spec(self.source, "/instrument/values")
        assert spec.source_absolute_offset is not None
        self.source.write_bytes(self.source.read_bytes()[:spec.source_absolute_offset + 5*4 + 2])
        result = self._assert_output(values, 5)
        self.assertTrue(result["allocation"]["declared_eof_past_physical_file"])
        self.assertEqual(result["allocation"]["physically_available_size_bytes"], 22)
        fragment = result["unassigned_fragments"][0]
        self.assertEqual(fragment["source_absolute_offset"], spec.source_absolute_offset + 20)
        self.assertEqual(fragment["size_bytes"], 2)
        self.assertFalse(result["complete"])

    def test_truncated_old_contiguous_keeps_complete_elements_only(self) -> None:
        values = self._create(latest=False, compact=False, dtype="<u2")
        spec = read_nonchunked_spec(self.source, "/instrument/values")
        assert spec.source_absolute_offset is not None
        self.source.write_bytes(self.source.read_bytes()[:spec.source_absolute_offset + 7*2])
        result = self._assert_output(values, 7)
        self.assertEqual(result["counts"]["unknown"], 5)

    def test_unallocated_contiguous_is_unknown_not_fill_measurements(self) -> None:
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("values", (3, 4), dtype="<u4")
        result = export_nonchunked(self.source, "/values", self.output, self.report_path)
        self.assertEqual(result["counts"], {"recovered": 0, "unknown": 12})
        self.assertEqual(result["mappings"], [])
        with h5py.File(self.output) as handle:
            self.assertEqual(int(np.count_nonzero(handle["/_h5reclaim/element_status"][:])), 0)

    def test_scalar_and_float_bit_patterns_are_preserved(self) -> None:
        for scalar in (False, True):
            for order in ("<", ">"):
                with self.subTest(scalar=scalar, order=order):
                    self.source.unlink(missing_ok=True)
                    self.output.unlink(missing_ok=True)
                    self.report_path.unlink(missing_ok=True)
                    bits = np.array([0x7ff8000000000001, 0x8000000000000000,
                                     0xfff8000000004444], dtype=order + "u8")
                    expected = bits[:1].view(order + "f8").reshape(()) if scalar else bits.view(order + "f8")
                    with h5py.File(self.source, "w", libver="latest") as handle:
                        handle.create_dataset("x", data=expected)
                    result = export_nonchunked(self.source, "/x", self.output,
                                               self.report_path)
                    self.assertTrue(result["complete"])
                    with h5py.File(self.output) as handle:
                        observed = handle["x"].astype(expected.dtype)[...]
                        self.assertEqual(observed.tobytes(), expected.tobytes())
                        self.assertEqual(handle["/_h5reclaim/element_status"].shape,
                                         expected.shape)

    def test_checked_compact_payload_damage_fails_header_checksum(self) -> None:
        self._create(latest=True, compact=True)
        spec = read_nonchunked_spec(self.source, "/instrument/values")
        assert spec.source_absolute_offset is not None
        raw = bytearray(self.source.read_bytes())
        raw[spec.source_absolute_offset + 2] ^= 0x80
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "object-header checksum mismatch"):
            export_nonchunked(self.source, "/instrument/values", self.output,
                              self.report_path)
        self.assertFalse(self.output.exists())

    def test_modified_optional_metadata_can_break_native_open_without_payload_loss(self) -> None:
        expected = self._create(latest=True, compact=False)
        with h5py.File(self.source) as handle:
            address = h5py.h5o.get_info(handle["/instrument/values"].id).addr
        with ModernH5File(self.source) as reader:
            fill = next(m for m in _messages(reader, address) if m.kind == 5)
        _patch_checked_object(self.source, address, fill.absolute_offset, b"\xff")
        with h5py.File(self.source) as handle:
            with self.assertRaises((KeyError, OSError)):
                _ = handle["/instrument/values"]
        result = self._assert_output(expected, expected.size)
        self.assertEqual(result["operation"], "structural_nonchunked_export")

    def test_contradictory_layout_address_overlapping_metadata_refused(self) -> None:
        self._create(latest=True, compact=False)
        with h5py.File(self.source) as handle:
            address = h5py.h5o.get_info(handle["/instrument/values"].id).addr
        with ModernH5File(self.source) as reader:
            layout = next(m for m in _messages(reader, address) if m.kind == 8)
        _patch_checked_object(self.source, address, layout.absolute_offset + 2,
                              address.to_bytes(8, "little"))
        with self.assertRaisesRegex(FormatError, "overlaps rooted metadata"):
            export_nonchunked(self.source, "/instrument/values", self.output, self.report_path)
        self.assertFalse(self.output.exists())

    def test_wrong_declared_size_and_corrupt_checksum_refused(self) -> None:
        self._create(latest=True, compact=False)
        with h5py.File(self.source) as handle:
            address = h5py.h5o.get_info(handle["/instrument/values"].id).addr
        with ModernH5File(self.source) as reader:
            layout = next(m for m in _messages(reader, address) if m.kind == 8)
        size_offset = layout.absolute_offset + 10
        _patch_checked_object(self.source, address, size_offset, (23).to_bytes(8, "little"))
        with self.assertRaisesRegex(FormatError, "allocation length contradicts"):
            export_nonchunked(self.source, "/instrument/values", self.output, self.report_path)
        raw = bytearray(self.source.read_bytes())
        raw[address + 25] ^= 1
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "object-header checksum mismatch"):
            export_nonchunked(self.source, "/instrument/values", self.output, self.report_path)

    def test_missing_local_link_never_falls_back_to_compatible_distractor(self) -> None:
        self._create(latest=True, compact=False)
        with self.assertRaisesRegex(UnsupportedFormat, "no rooted hard link"):
            export_nonchunked(self.source, "/instrument/absent", self.output, self.report_path)
        self.assertFalse(self.output.exists())

    def test_operator_hints_can_reject_conflicts_but_do_not_assign_bytes(self) -> None:
        self._create(latest=True, compact=False)
        with self.assertRaisesRegex(HintsError, "assert chunks"):
            export_nonchunked(
                self.source, "/instrument/values", self.output, self.report_path,
                hints=DatasetHints("/instrument/values", chunks=(3, 4)),
            )
        with self.assertRaisesRegex(HintsError, "conflict"):
            export_nonchunked(
                self.source, "/instrument/values", self.output, self.report_path,
                hints=DatasetHints("/instrument/values", dtype=">u4"),
            )
        self.assertFalse(self.output.exists())
        report = export_nonchunked(
            self.source, "/instrument/values", self.output, self.report_path,
            hints=DatasetHints("/instrument/values", shape=(3, 4), dtype="<i2"),
        )
        self.assertEqual([comparison["status"] for comparison in
                          report["operator_hints"]["comparisons"]], ["matches"] * 3)

    def test_authentic_qubit_mixed_headers_native_failure_and_sibling_conflict(self) -> None:
        corpus = Path(__file__).resolve().parents[1] / "corpus"
        original = corpus / "files" / "fast_feedback_raw_data.h5"
        manifest = json.loads((corpus / "manifest.json").read_text())
        pinned = next(entry["sha256"] for entry in manifest["entries"]
                      if entry["id"] == "zenodo_qubit_feedback")
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), pinned)
        path = "/circuit_0/result/hard_measurements/36"
        sibling = "/circuit_0/result/hard_measurements/38"
        with h5py.File(original) as handle:
            expected = handle[path][:]
            object_address = h5py.h5o.get_info(handle[path].id).addr
            source_offset = handle[path].id.get_offset()
            sibling_offset = handle[sibling].id.get_offset()
            newer_group_address = h5py.h5o.get_info(
                handle["/reference_data/delays/delay_9"].id).addr
        with _TruncatedOldReader(original) as reader:
            messages = _old_messages(reader, object_address)
            fill = next(message for message in messages if message.kind == 5)
            layout = next(message for message in messages if message.kind == 8)
        shutil.copyfile(original, self.source)
        with self.source.open("r+b") as stream:
            stream.seek(fill.absolute_offset)
            stream.write(b"\xff")
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with h5py.File(self.source) as handle:
            with self.assertRaises((KeyError, OSError)):
                _ = handle[path]
        report = export_nonchunked(self.source, path, self.output, self.report_path)
        self.assertEqual(report["counts"], {"recovered": 2000, "unknown": 0})
        self.assertEqual(report["mappings"][0]["source_absolute_offset"], source_offset)
        self.assertTrue(report["metadata_resolution"]["allocation_conflict_check"]["complete"])
        self.assertGreater(report["metadata_resolution"]["object_header_generations_seen"]["v1"], 0)
        self.assertGreater(report["metadata_resolution"]["object_header_generations_seen"]["v2"], 0)
        with h5py.File(self.output) as recovered:
            self.assertEqual(recovered[path][:].tobytes(), expected.tobytes())
            self.assertTrue(np.all(recovered["/_h5reclaim/element_status"][:] == 1))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)

        public_output, public_report = self.base / "rescue.h5", self.base / "rescue.json"
        project = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(project / "src") + os.pathsep + environment.get("PYTHONPATH", "")
        process = subprocess.run(
            [sys.executable, "-m", "h5reclaim", "rescue", str(self.source),
             "--dataset", path, "--output", str(public_output),
             "--report", str(public_report)],
            cwd=project, env=environment, capture_output=True, text=True,
            check=False, timeout=60,
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout[-500:])
        self.assertIn("2000 accepted elements", process.stdout)
        public_evidence = json.loads(public_report.read_text())
        self.assertEqual(public_evidence["operation"], "structural_nonchunked_export")
        with h5py.File(public_output) as recovered:
            self.assertEqual(recovered[path][:].tobytes(), expected.tobytes())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)

        conflict = self.base / "conflict.h5"
        shutil.copyfile(self.source, conflict)
        with conflict.open("r+b") as stream:
            stream.seek(layout.absolute_offset + 2)
            stream.write(int(sibling_offset).to_bytes(8, "little"))
        with self.assertRaisesRegex(FormatError, "overlaps another rooted dataset allocation"):
            export_nonchunked(conflict, path, self.base / "refused.h5",
                              self.base / "refused.json")
        self.assertFalse((self.base / "refused.h5").exists())

        bad_header = self.base / "bad_newer_header.h5"
        shutil.copyfile(self.source, bad_header)
        with bad_header.open("r+b") as stream:
            stream.seek(newer_group_address + 20)
            original_byte = stream.read(1)
            stream.seek(newer_group_address + 20)
            stream.write(bytes([original_byte[0] ^ 1]))
        with self.assertRaisesRegex(FormatError, "object-header checksum mismatch"):
            export_nonchunked(bad_header, path, self.base / "refused2.h5",
                              self.base / "refused2.json")
        self.assertFalse((self.base / "refused2.h5").exists())


if __name__ == "__main__":
    unittest.main()
