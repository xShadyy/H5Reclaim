"""Damage cases for prospective per-element evidence in nonchunked datasets."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError, UnsupportedFormat
from h5reclaim.nonchunked_recovery import read_nonchunked_spec
from h5reclaim.payload_integrity import capture_element_baseline, export_verified_nonchunked
from h5reclaim.recovery import RecoveryError


class ProspectiveElementIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "experiment.h5"
        self.baseline = self.directory / "prior-element-hashes.zip"
        self.output = self.directory / "derived.h5"
        self.report = self.directory / "evidence.json"

    def _source(self, *, latest: bool = True, dtype: str = "<i4") -> tuple[np.ndarray, int]:
        values = np.arange(40, dtype=dtype).reshape((5, 8)) * 13 - 50
        with h5py.File(self.source, "w", libver="latest" if latest else "earliest") as file:
            group = file.create_group("instrument")
            group.create_dataset("measurements", data=values)
            group.create_dataset("other", data=np.full((5, 8), 909, dtype=dtype))
        spec = read_nonchunked_spec(self.source, "/instrument/measurements")
        assert spec.source_absolute_offset is not None
        return values, spec.source_absolute_offset

    def _capture(self) -> str:
        baseline = capture_element_baseline(self.source, "/instrument/measurements", self.baseline)
        self.assertEqual(baseline["archive_sha256"], hashlib.sha256(self.baseline.read_bytes()).hexdigest())
        return baseline["archive_sha256"]

    def _export(self, digest: str) -> dict:
        return export_verified_nonchunked(
            self.source, "/instrument/measurements", self.baseline, digest,
            self.output, self.report,
        )

    def test_two_silent_bit_flips_are_unknown_neighbors_remain_exact(self) -> None:
        for latest in (False, True):
            with self.subTest(latest=latest):
                for path in (self.source, self.baseline, self.output, self.report):
                    path.unlink(missing_ok=True)
                expected, offset = self._source(latest=latest, dtype=">i4")
                prior_hash = self._capture()
                raw = bytearray(self.source.read_bytes())
                for index in (3, 35):
                    raw[offset + index * 4 + 2] ^= 0x40
                self.source.write_bytes(raw)
                damaged_digest = hashlib.sha256(raw).hexdigest()
                # HDF5's native read succeeds and returns altered values silently.
                with h5py.File(self.source) as file:
                    self.assertNotEqual(file["/instrument/measurements"][...].tobytes(),
                                        expected.tobytes())
                result = self._export(prior_hash)
                self.assertEqual(result["counts"], {"recovered": 38, "unknown": 2})
                self.assertEqual(result["baseline"]["captured_source_sha256"],
                                 json.loads(zipfile.ZipFile(self.baseline).read("manifest.json"))
                                 ["source_sha256"])
                self.assertEqual(result, json.loads(self.report.read_text()))
                self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), damaged_digest)
                with h5py.File(self.output) as file:
                    observed = file["/instrument/measurements"][...]
                    status = file["/_h5reclaim/element_status"][...].reshape(-1)
                    self.assertEqual(np.flatnonzero(status == 0).tolist(), [3, 35])
                    self.assertTrue(np.array_equal(observed.reshape(-1)[status == 1],
                                                   expected.reshape(-1)[status == 1]))
                    self.assertTrue(np.all(observed.reshape(-1)[status == 0] == 0))

    def test_tail_truncation_and_mismatch_combine_without_assigning_lost_values(self) -> None:
        expected, offset = self._source(dtype="<u2")
        prior_hash = self._capture()
        raw = bytearray(self.source.read_bytes()[:offset + 18 * 2 + 1])
        raw[offset + 4 * 2] ^= 0x01
        self.source.write_bytes(raw)
        result = self._export(prior_hash)
        self.assertEqual(result["counts"], {"recovered": 17, "unknown": 23})
        self.assertEqual(result["unassigned_fragments"][0]["size_bytes"], 1)
        with h5py.File(self.output) as file:
            values = file["/instrument/measurements"][...].reshape(-1)
            status = file["/_h5reclaim/element_status"][...].reshape(-1)
            self.assertEqual(np.flatnonzero(status == 0).tolist(), [4, *range(18, 40)])
            np.testing.assert_array_equal(values[status == 1], expected.reshape(-1)[status == 1])

    def test_unaltered_dataset_is_complete_and_scalar_bit_patterns_survive(self) -> None:
        expected, _ = self._source(dtype=">f8")
        prior_hash = self._capture()
        result = self._export(prior_hash)
        self.assertTrue(result["complete"])
        with h5py.File(self.output) as file:
            observed = file["/instrument/measurements"].astype(expected.dtype)[...]
            self.assertEqual(observed.tobytes(), expected.tobytes())

    def test_nan_payload_and_signed_zero_survive_while_neighbor_is_rejected(self) -> None:
        bits = np.array([0x7ff8000000007777, 0x8000000000000000,
                         0x3ff0000000000000, 0x7ff8000000004444], dtype="<u8")
        with h5py.File(self.source, "w", libver="latest") as file:
            file.create_dataset("x", data=bits.view("<f8"))
        offset = read_nonchunked_spec(self.source, "/x").source_absolute_offset
        assert offset is not None
        previous = capture_element_baseline(self.source, "/x", self.baseline)
        raw = bytearray(self.source.read_bytes())
        raw[offset + 2 * 8] ^= 0x1
        self.source.write_bytes(raw)
        result = export_verified_nonchunked(
            self.source, "/x", self.baseline, previous["archive_sha256"],
            self.output, self.report,
        )
        self.assertEqual(result["counts"], {"recovered": 3, "unknown": 1})
        with h5py.File(self.output) as file:
            observed = file["x"].astype("<f8")[...].view("<u8")
            np.testing.assert_array_equal(observed[[0, 1, 3]], bits[[0, 1, 3]])
            self.assertEqual(observed[2], 0)

    def test_wrong_prior_hash_or_schema_refuses_without_publishing(self) -> None:
        self._source()
        prior_hash = self._capture()
        with self.assertRaisesRegex(RecoveryError, "differs from the retained SHA-256"):
            self._export("0" * 64)
        self.assertFalse(self.output.exists())
        # A valid old baseline from a different layout cannot be silently
        # borrowed for a newly shaped current dataset with the same path.
        self.source.unlink()
        with h5py.File(self.source, "w", libver="latest") as file:
            file.create_group("instrument").create_dataset(
                "measurements", data=np.arange(35, dtype="<i4").reshape(5, 7)
            )
        with self.assertRaisesRegex(RecoveryError, "schema contradicts"):
            self._export(prior_hash)
        self.assertFalse(self.output.exists())

    def test_baseline_capture_refuses_partial_source_and_existing_path(self) -> None:
        _values, offset = self._source()
        self._capture()
        with self.assertRaisesRegex(RecoveryError, "must be new"):
            capture_element_baseline(self.source, "/instrument/measurements", self.baseline)
        self.baseline.unlink()
        self.source.write_bytes(self.source.read_bytes()[:offset + 11])
        with self.assertRaisesRegex(UnsupportedFormat, "complete selected allocation"):
            capture_element_baseline(self.source, "/instrument/measurements", self.baseline)
        self.assertFalse(self.baseline.exists())

    def test_checked_compact_payload_corruption_cannot_be_bypassed_by_baseline(self) -> None:
        with h5py.File(self.source, "w", libver="latest") as file:
            space = h5py.h5s.create_simple((8,))
            dtype = h5py.h5t.py_create(np.dtype("<u2"))
            creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            creation.set_layout(h5py.h5d.COMPACT)
            obj = h5py.h5d.create(file.id, b"measurements", dtype, space, dcpl=creation)
            obj.write(h5py.h5s.ALL, h5py.h5s.ALL, np.arange(8, dtype="<u2"))
            obj.close()
        spec = read_nonchunked_spec(self.source, "/measurements")
        assert spec.source_absolute_offset is not None
        prior_hash = capture_element_baseline(self.source, "/measurements", self.baseline)
        raw = bytearray(self.source.read_bytes())
        raw[spec.source_absolute_offset + 2] ^= 1
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "object-header checksum mismatch"):
            export_verified_nonchunked(self.source, "/measurements", self.baseline,
                                       prior_hash["archive_sha256"], self.output, self.report)
        self.assertFalse(self.output.exists())

    def test_aliased_baseline_is_rejected(self) -> None:
        self._source()
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RecoveryError, "distinct regular files"):
            export_verified_nonchunked(self.source, "/instrument/measurements",
                                       self.source, digest, self.output, self.report)
