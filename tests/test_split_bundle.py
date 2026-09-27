"""Split driver tests use the real HDF5 writer and exact coordinate checks."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.format import FormatError, UnsupportedFormat
from h5reclaim.recovery import RecoveryError
from h5reclaim.split_bundle import export_split, inspect_split_map


class SplitBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stem = self.root / "instrument"
        self.metadata = self.root / "instrument-m.h5"
        self.raw = self.root / "instrument-r.h5"
        self.output = self.root / "output.h5"
        self.report = self.root / "report.json"

    def _create(self, *, libver: str = "earliest", layout: str = "chunked", sparse: bool = False) -> None:
        with h5py.File(self.stem, "w", driver="split", meta_ext=b"-m.h5", raw_ext=b"-r.h5",
                       libver=libver) as handle:
            if layout == "chunked":
                data = handle.create_dataset("/exp/strain", shape=(25,), dtype="<f8",
                                             chunks=(8,), fletcher32=True, compression="gzip")
                data[:8] = np.arange(8, dtype="<f8") + 0.25
                data[8:16] = np.arange(8, 16, dtype="<f8") + 0.25
                if not sparse:
                    data[16:] = np.arange(16, 25, dtype="<f8") + 0.25
            elif layout == "contiguous":
                handle.create_dataset("/exp/strain", data=np.arange(25, dtype="<f8") + 0.25)
            else:
                handle.create_dataset("/exp/strain", data=np.arange(4, dtype="<f8"))

    def _manifest(self) -> dict:
        return {"schema_version": 1, "driver": "split", "members": [
            {"role": role, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for role, path in (("metadata", self.metadata), ("raw", self.raw))
        ]}

    def test_old_and_new_superblocks_restore_exact_current_chunk_coordinates(self) -> None:
        for version in ("earliest", "latest"):
            with self.subTest(version=version):
                self._create(libver=version)
                previous = (self.metadata.read_bytes(), self.raw.read_bytes())
                result = export_split(self._manifest(), "/exp/strain", self.output, self.report)
                self.assertEqual(result["outcome"], "complete")
                self.assertEqual(result["accepted_elements"], 25)
                self.assertEqual([record["coordinate"] for record in result["accepted_ranges"]],
                                 [[0], [8], [16], [24]])
                self.assertTrue(all(record["member_role"] == "raw" for record in result["accepted_ranges"]))
                self.assertEqual(result["address_map"]["driver_anchor_checksum_validated"], version == "latest")
                for record in result["accepted_ranges"]:
                    start, stop = record["physical_byte_range"]
                    self.assertEqual(hashlib.sha256(previous[1][start:stop]).hexdigest(), record["raw_sha256"])
                    self.assertEqual(result["address_map"]["raw_start"] + start, record["logical_address"])
                with h5py.File(self.output, "r") as output:
                    np.testing.assert_array_equal(output["/exp/strain"][:],
                                                  np.arange(25, dtype="<f8") + 0.25)
                    np.testing.assert_array_equal(output["/_h5reclaim/validity"][:], np.ones(4, dtype="u1"))
                    self.assertTrue(output["/exp/strain"].id.get_type().equal(
                        h5py.h5t.py_create(np.dtype("<f8"))))
                self.assertEqual(self.metadata.read_bytes(), previous[0])
                self.assertEqual(self.raw.read_bytes(), previous[1])
                self.assertEqual(json.loads(self.report.read_text()), result)
                self.output.unlink()
                self.report.unlink()
                self.metadata.unlink()
                self.raw.unlink()

    def test_contiguous_virtual_address_is_mapped_to_pinned_raw_bytes(self) -> None:
        self._create(layout="contiguous")
        result = export_split(self._manifest(), "/exp/strain", self.output, self.report)
        self.assertEqual(result["accepted_elements"], 25)
        self.assertEqual(result["accepted_ranges"][0]["physical_byte_range"], [0, 200])
        self.assertIsNone(result["validity_map"])
        with h5py.File(self.output, "r") as output:
            np.testing.assert_array_equal(output["/exp/strain"][:], np.arange(25) + 0.25)

    def test_sparse_chunk_is_unknown_and_never_copied_as_fill_measurement(self) -> None:
        self._create(sparse=True)
        result = export_split(self._manifest(), "/exp/strain", self.output, self.report)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["unknown_chunk_origins"], [[16], [24]])
        self.assertEqual(result["accepted_elements"], 16)
        with h5py.File(self.output, "r") as output:
            np.testing.assert_array_equal(output["/_h5reclaim/validity"][:], [1, 1, 0, 0])

    def test_missing_or_bad_raw_member_never_publishes(self) -> None:
        self._create()
        manifest = self._manifest()
        self.raw.unlink()
        with self.assertRaises(OSError):
            export_split(manifest, "/exp/strain", self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())
        self._create()
        manifest = self._manifest()
        manifest["members"][1]["sha256"] = "0" * 64
        with self.assertRaisesRegex(RecoveryError, "pinned hash"):
            export_split(manifest, "/exp/strain", self.output, self.report)
        self.assertFalse(self.output.exists())

    def test_declared_eoa_cannot_silently_cover_truncated_raw_bytes(self) -> None:
        self._create()
        self.raw.write_bytes(self.raw.read_bytes()[:-1])
        with self.assertRaises(FormatError):
            export_split(self._manifest(), "/exp/strain", self.output, self.report)
        self.assertFalse(self.output.exists())

    def test_checksum_failing_raw_chunk_cannot_be_published_as_valid(self) -> None:
        self._create()
        damaged = bytearray(self.raw.read_bytes())
        damaged[10] ^= 0x20
        self.raw.write_bytes(damaged)
        with self.assertRaises((OSError, RuntimeError, RecoveryError)):
            export_split(self._manifest(), "/exp/strain", self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_relocated_safe_names_are_read_only_from_private_members(self) -> None:
        with h5py.File(self.stem, "w", driver="split", meta_ext=b".metadataA", raw_ext=b".recordsB") as file:
            file.create_dataset("/exp/strain", data=np.arange(16, dtype="<u4"))
        alternate_meta = self.root / "instrument.metadataA"
        alternate_raw = self.root / "instrument.recordsB"
        self.metadata.write_bytes(alternate_meta.read_bytes())
        self.raw.write_bytes(alternate_raw.read_bytes())
        alternate_meta.unlink()
        alternate_raw.unlink()
        result = export_split(self._manifest(), "/exp/strain", self.output, self.report)
        self.assertEqual(result["address_map"]["stored_member_names"],
                         ["%s.metadataA", "%s.recordsB"])
        with h5py.File(self.output, "r") as file:
            np.testing.assert_array_equal(file["/exp/strain"][:], np.arange(16, dtype="<u4"))

    def test_wrong_driver_mapping_is_not_treated_as_split(self) -> None:
        self._create()
        data = bytearray(self.metadata.read_bytes())
        location = data.find(b"NCSAmult")
        self.assertGreater(location, 0)
        data[location + 8 + 3] = 6  # raw-memory class routed to a different member
        self.metadata.write_bytes(data)
        with self.assertRaisesRegex(UnsupportedFormat, "generic Multi mapping"):
            export_split(self._manifest(), "/exp/strain", self.output, self.report)
        self.assertFalse(self.output.exists())

    def test_modern_driver_info_tampering_fails_checksum(self) -> None:
        self._create(libver="latest")
        data = bytearray(self.metadata.read_bytes())
        location = data.find(b"NCSAmult")
        self.assertGreater(location, 0)
        data[location + 12] ^= 1
        self.metadata.write_bytes(data)
        with self.assertRaisesRegex(FormatError, "checksum"):
            inspect_split_map(self.metadata, self.raw.stat().st_size)

    def test_non_file_paths_and_output_alias_refuse(self) -> None:
        self._create()
        manifest = self._manifest()
        with self.assertRaisesRegex(RecoveryError, "distinct new paths"):
            export_split(manifest, "/exp/strain", self.raw, self.report)
        manifest["members"][0]["path"] = "instrument-m.h5"
        with self.assertRaisesRegex(RecoveryError, "absolute paths"):
            export_split(manifest, "/exp/strain", self.output, self.report)

    def test_rechecked_sources_must_remain_pinned_before_publish(self) -> None:
        self._create()
        from h5reclaim import split_bundle
        with patch.object(split_bundle, "_verify_source", side_effect=RecoveryError("changed")):
            with self.assertRaisesRegex(RecoveryError, "changed"):
                export_split(self._manifest(), "/exp/strain", self.output, self.report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())


if __name__ == "__main__":
    unittest.main()
