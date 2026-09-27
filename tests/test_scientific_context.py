"""A source context audit remains bounded and never supplies missing measurements."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.scientific_context import audit_context


class ScientificContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "sample.h5"
        with h5py.File(self.source, "w") as handle:
            group = handle.create_group("lab")
            group.attrs["instrument"] = "spectrometer"
            selected = group.create_dataset("readings", data=list(range(8)), chunks=(4,))
            selected.attrs["units"] = "count"
            selected.attrs["channel_units"] = np.bytes_("millivolt")
            selected.attrs["calibration"] = 3.5
            handle["/alias"] = selected
            handle["/shortcut"] = h5py.SoftLink("/lab/readings")
            handle["/offsite"] = h5py.ExternalLink("unprovided.h5", "/hidden")
            axis = handle.create_dataset("axis", data=list(range(8)))
            axis.make_scale("sample")
            selected.dims[0].attach_scale(axis)
            selected.dims[0].label = "sample index"
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def test_rooted_metadata_and_omissions_are_reported_without_link_targets(self):
        result = audit_context(self.source, "/lab/readings", self.digest,
                               copied_attributes=("units",), omitted_attributes=("DIMENSION_LIST",))
        self.assertEqual(result["status"], "bounded_observation")
        self.assertEqual(result["source_sha256_bound"], self.digest)
        selected = result["source_metadata"]["selected_dataset"]
        self.assertEqual(selected["unit_attribute_names"], ["channel_units", "units"])
        self.assertEqual(selected["unit_values"]["channel_units"],
                         {"status": "observed", "value": "millivolt"})
        self.assertEqual(selected["unit_values"]["units"]["status"], "not_read")
        self.assertTrue(selected["dimension_scale_markers"]["dimension_list"])
        self.assertTrue(selected["dimension_scale_markers"]["dimension_labels"])
        self.assertEqual(selected["dimension_scale_targets"], "not dereferenced or copied")
        self.assertIn("DIMENSION_LIST", result["selected_attribute_names_not_copied"])
        self.assertNotIn("units", result["selected_attribute_names_not_copied"])
        root = result["source_metadata"]["ancestor_groups"][0]
        self.assertIn({"name": "offsite", "kind": "external"}, root["links"])
        self.assertIn({"name": "shortcut", "kind": "soft"}, root["links"])
        self.assertEqual(result["source_metadata"]["ancestor_groups"][1]["attributes"]["names"],
                         ["instrument"])
        self.assertIn("aliases outside the selected hard-link path were not inventoried",
                      result["omissions"])

    def test_changed_source_is_not_given_context_evidence(self):
        with self.source.open("ab") as stream:
            stream.write(b"different")
        result = audit_context(self.source, "/lab/readings", self.digest,
                               copied_attributes=("units",))
        self.assertEqual(result["status"], "uninspected")
        self.assertIsNone(result["source_metadata"])
        self.assertTrue(any("differs" in reason for reason in result["omissions"]))

    def test_wrong_copied_name_is_contradiction_not_silent_success(self):
        result = audit_context(self.source, "/lab/readings", self.digest,
                               copied_attributes=("invented_units",))
        self.assertEqual(result["status"], "contradiction")
        self.assertEqual(result["copied_name_contradictions"], ["invented_units"])

    def test_audit_does_not_follow_external_selected_link(self):
        result = audit_context(self.source, "/offsite", self.digest)
        self.assertEqual(result["status"], "uninspected")
        self.assertIsNone(result["source_metadata"])

    def test_large_attribute_inventory_stays_unenumerated(self):
        with h5py.File(self.source, "r+") as handle:
            selected = handle["/lab/readings"]
            for index in range(65):
                selected.attrs[f"extra_{index:02d}"] = index
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = audit_context(self.source, "/lab/readings", digest)
        self.assertEqual(result["status"], "bounded_observation")
        self.assertIsNone(result["source_metadata"]["selected_dataset"]["attribute_names"]["names"])
        self.assertTrue(any("selected attribute names exceed" in item for item in result["omissions"]))

    def test_native_child_crash_keeps_recovery_context_unknown(self):
        with patch("h5reclaim.scientific_context.run_worker",
                   return_value=subprocess.CompletedProcess([], -11)):
            result = audit_context(self.source, "/lab/readings", self.digest)
        self.assertEqual(result["status"], "uninspected")
        self.assertIsNone(result["source_metadata"])


if __name__ == "__main__":
    unittest.main()
