"""Scientific attributes keep their values when named like output hints."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.baseline import capture_baseline
from h5reclaim.chunk_integrity import export_verified_chunks
from h5reclaim.erasure_sidecar import capture_erasure_sidecar, restore_from_erasure
from h5reclaim.metadata import read_dataset_spec
from h5reclaim.parity_sidecar import capture_parity_sidecar, restore_from_parity
from h5reclaim.recovery import recover, sha256_file
from h5reclaim.replica_recovery import restore_from_replicas


SELECTED = "/science/data"
ANNOTATION_NAMES = ["h5reclaim_chunk_status", "h5reclaim_complete", "h5reclaim_warning"]


class AnnotationCollisionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "experiment.h5"
        self.baseline = self.base / "capture.json"
        self.values = np.arange(64, dtype="<u4") + 7
        with h5py.File(self.source, "x", libver="latest") as file:
            selected = file.create_dataset(SELECTED, data=self.values, chunks=(16,))
            selected.attrs["h5reclaim_chunk_status"] = np.bytes_("instrument setting")
            selected.attrs["h5reclaim_complete"] = np.uint32(17)
            selected.attrs["h5reclaim_warning"] = np.bytes_("retain raw calibration")
            selected.attrs["units"] = np.bytes_("counts")
            selected.attrs["unsupported_array"] = np.array([1, 2], dtype="<u2")

    def assert_preserved(self, output: Path, report_path: Path, result: dict) -> None:
        self.assertEqual(result, json.loads(report_path.read_text()))
        self.assertEqual(result["selected_annotation_collisions"], ANNOTATION_NAMES)
        with h5py.File(self.source, "r") as old, h5py.File(output, "r") as new:
            original, selected = old[SELECTED], new[SELECTED]
            np.testing.assert_array_equal(selected[...], self.values)
            for name in (*ANNOTATION_NAMES, "units"):
                with self.subTest(name=name):
                    self.assertTrue(original.attrs.get_id(name).get_type().equal(
                        selected.attrs.get_id(name).get_type()))
                    self.assertEqual(original.attrs.get_id(name).get_space().get_simple_extent_type(),
                                     selected.attrs.get_id(name).get_space().get_simple_extent_type())
                    np.testing.assert_array_equal(np.asarray(original.attrs[name]),
                                                  np.asarray(selected.attrs[name]))
            self.assertNotIn("unsupported_array", selected.attrs)
            self.assertTrue(np.all(new["/_h5reclaim/chunk_status"][...] == 1))
            self.assertEqual(json.loads(new["/_h5reclaim/report_json"][()]), result)

    def test_structural_and_prospective_routes_preserve_colliding_scalars(self) -> None:
        original_sha = sha256_file(self.source)
        spec = read_dataset_spec(self.source, SELECTED)
        self.assertEqual([name for name, _value in spec.attributes],
                         [*ANNOTATION_NAMES, "units"])
        self.assertEqual(spec.omitted_attributes, ("unsupported_array",))

        output = self.base / "structural.h5"
        report_path = self.base / "structural.json"
        self.assert_preserved(output, report_path,
                              recover(self.source, SELECTED, output, report_path))

        capture_baseline(self.source, SELECTED, self.baseline)
        baseline_sha = sha256_file(self.baseline)
        output = self.base / "baseline.h5"
        report_path = self.base / "baseline.json"
        self.assert_preserved(output, report_path,
                              export_verified_chunks(self.source, SELECTED, self.baseline,
                                                     baseline_sha, output, report_path))

        parity = self.base / "parity.zip"
        capture_parity_sidecar(self.source, SELECTED, self.baseline, parity)
        parity_manifest = self.base / "parity_manifest.json"
        parity_manifest.write_text(json.dumps({
            "schema_version": 1, "damaged_sha256": original_sha,
            "baseline": {"path": str(self.baseline), "sha256": baseline_sha},
            "parity": {"path": str(parity), "sha256": sha256_file(parity)},
        }))
        output = self.base / "parity.h5"
        report_path = self.base / "parity.json"
        self.assert_preserved(output, report_path,
                              restore_from_parity(self.source, SELECTED, parity_manifest,
                                                  output, report_path))

        erasure = self.base / "erasure.zip"
        capture_erasure_sidecar(self.source, SELECTED, self.baseline, erasure,
                                stripe_width=4, parity_shards=2)
        erasure_manifest = self.base / "erasure_manifest.json"
        erasure_manifest.write_text(json.dumps({
            "schema_version": 1, "damaged_sha256": original_sha,
            "baseline": {"path": str(self.baseline), "sha256": baseline_sha},
            "erasure": {"path": str(erasure), "sha256": sha256_file(erasure)},
        }))
        output = self.base / "erasure.h5"
        report_path = self.base / "erasure.json"
        self.assert_preserved(output, report_path,
                              restore_from_erasure(self.source, SELECTED, erasure_manifest,
                                                   output, report_path))

        replica = self.base / "replica.h5"
        shutil.copyfile(self.source, replica)
        replica_manifest = self.base / "replica_manifest.json"
        replica_manifest.write_text(json.dumps({
            "schema_version": 1, "damaged_sha256": original_sha,
            "baseline": {"path": str(self.baseline), "sha256": baseline_sha},
            "replicas": [{"path": str(replica), "sha256": sha256_file(replica)}],
        }))
        output = self.base / "replica_out.h5"
        report_path = self.base / "replica_out.json"
        self.assert_preserved(output, report_path,
                              restore_from_replicas(self.source, SELECTED, replica_manifest,
                                                    output, report_path))
        self.assertEqual(sha256_file(self.source), original_sha)


if __name__ == "__main__":
    unittest.main()
