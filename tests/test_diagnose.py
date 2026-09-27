"""Read-only triage stays accurate across supported and unreadable layouts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim import diagnose as diagnosis_module
from h5reclaim import survey as survey_module


class DiagnosisTests(unittest.TestCase):
    def test_mixed_layout_no_payload_reads_and_no_repair_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mixed.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset(
                    "supported", data=np.arange(144, dtype="<u4").reshape(12, 12),
                    chunks=(1, 1),
                )
                handle.create_dataset("other", data=np.arange(12, dtype="<f4"))
            before = source.read_bytes()
            with patch.object(h5py.Dataset, "__getitem__", side_effect=AssertionError("value read")):
                report = diagnosis_module.diagnose(source, "/supported")
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(report["outcome"], "triaged")
            self.assertEqual(report["next_action"], "inspect_anchored_index")
            self.assertEqual(report["selection"]["support"]["status"], "candidate")
            self.assertEqual(report["inventory"]["support_counts"]["unsupported"], 1)
            self.assertEqual(report["inventory"]["candidate_paths"], ["/supported"])
            self.assertFalse(report["recovery_attempted"])
            self.assertIsNone(report["recovered_values"])
            self.assertNotIn("dataset_path", [question["key"] for question in report["questions"]])

    def test_no_signature_does_not_attempt_hdf5_metadata_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "unknown.dat"
            source.write_bytes(b"not HDF5\0\x89HDF at a noncanonical location")
            with patch.object(diagnosis_module, "_survey_snapshot", side_effect=AssertionError("opened")):
                report = diagnosis_module.diagnose(source)
            self.assertEqual(report["condition"], "format_unrecognized")
            self.assertEqual(report["next_action"], "identify_source_format")
            self.assertFalse(report["format_signature"]["present"])
            self.assertIsNone(report["inventory"])

    def test_surviving_signature_unreadable_metadata_reports_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "damaged.h5"
            source.write_bytes(diagnosis_module.HDF5_SIGNATURE + b"\xff" + b"\0" * 100)
            with patch.object(diagnosis_module, "_survey_snapshot", side_effect=survey_module.SurveyError("cannot open metadata")):
                report = diagnosis_module.diagnose(source, "/readings")
            self.assertEqual(report["outcome"], "limited")
            self.assertEqual(report["condition"], "metadata_unreadable")
            self.assertEqual(report["format_signature"]["superblock_version_byte"], 255)
            self.assertEqual(report["error"]["code"], "hdf5_metadata_unreadable")
            self.assertIsNone(report["selection"])
            self.assertIn("external_or_virtual_dependencies", {q["key"] for q in report["questions"]})

    def test_exactly_eight_signature_bytes_report_truncated_hdf5(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "truncated.h5"
            source.write_bytes(diagnosis_module.HDF5_SIGNATURE)
            report = diagnosis_module.diagnose(source)
            self.assertEqual(report["condition"], "metadata_unreadable")
            self.assertTrue(report["format_signature"]["present"])
            self.assertIsNone(report["format_signature"]["superblock_version_byte"])

    def test_real_file_with_corrupt_superblock_reports_unreadable_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "damaged.h5"
            with h5py.File(source, "x") as handle:
                handle.create_dataset("readings", data=[1, 2, 3])
            image = bytearray(source.read_bytes())
            self.assertEqual(image[:8], diagnosis_module.HDF5_SIGNATURE)
            image[8] = 255  # Preserve the signature but invalidate its format version.
            source.write_bytes(image)
            report = diagnosis_module.diagnose(source, "/readings")
            self.assertEqual(report["condition"], "metadata_unreadable")
            self.assertFalse(report["recovery_attempted"])
            self.assertEqual(source.read_bytes(), bytes(image))

    def test_legal_userblock_offset_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "wrapped.h5"
            with h5py.File(source, "x", userblock_size=512, libver=("earliest", "v108")) as handle:
                handle.create_dataset(
                    "readings", data=np.arange(144, dtype="<u4").reshape(12, 12),
                    chunks=(1, 1),
                )
            report = diagnosis_module.diagnose(source)
            self.assertTrue(report["format_signature"]["present"])
            self.assertEqual(report["format_signature"]["byte_offset"], 512)
            self.assertEqual(report["next_action"], "select_dataset_for_inspection")

    def test_unknown_selected_path_does_not_hijack_an_existing_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "file.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset(
                    "measured", data=np.arange(144, dtype="<u4").reshape(12, 12),
                    chunks=(1, 1),
                )
            report = diagnosis_module.diagnose(source, "/claimed_by_operator")
            self.assertEqual(report["next_action"], "select_existing_local_dataset")
            self.assertIsNone(report["selection"])
            self.assertEqual(report["inventory"]["candidate_paths"], ["/measured"])

    def test_partial_inventory_does_not_offer_supported_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "partial.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset(
                    "measured", data=np.arange(144, dtype="<u4").reshape(12, 12),
                    chunks=(1, 1),
                )
                handle.create_dataset("other", data=[1])
            with patch.object(survey_module, "MAX_LINKS", 1):
                report = diagnosis_module.diagnose(source, "/measured")
            self.assertEqual(report["condition"], "inventory_partial")
            self.assertEqual(report["next_action"], "resolve_inventory_issues")
            self.assertEqual(report["inventory"]["support_counts"]["candidate"], 0)

    def test_paged_fixed_array_is_an_indexed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "modern.h5"
            with h5py.File(source, "x", libver="latest") as handle:
                handle.create_dataset(
                    "readings", data=np.arange(1089, dtype="<u4").reshape(33, 33),
                    chunks=(1, 1),
                )
            report = diagnosis_module.diagnose(source, "/readings")
            self.assertEqual(report["selection"]["support"]["status"], "candidate")
            self.assertEqual(report["selection"]["index"]["type"], "fixed_array")
            self.assertEqual(report["next_action"], "inspect_anchored_index")
            self.assertFalse(report["recovery_attempted"])

    def test_authentic_gwosc_4khz_intact_index_is_a_candidate(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "corpus/files/H-H1_GWOSC_4KHZ_R1-1126259447-32.hdf5"
        )
        report = diagnosis_module.diagnose(source, "/strain/Strain")
        self.assertEqual(report["next_action"], "inspect_anchored_index")
        self.assertEqual(report["selection"]["support"]["status"], "candidate")
        self.assertEqual(report["selection"]["index"]["root_level"], 0)
        self.assertFalse(report["recovery_attempted"])


if __name__ == "__main__":
    unittest.main()
