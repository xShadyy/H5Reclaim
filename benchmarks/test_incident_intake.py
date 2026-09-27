"""Protocol self-checks with constructed faults, never claimed as natural incidents."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from .run_incident_intake import (IncidentError, capture_truth_hashes, digest,
                                  load_incidents, run_incidents, score_incidents)


class IncidentIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.reference = self.folder / "private" / "original.h5"
        self.reference.parent.mkdir()
        with h5py.File(self.reference, "w", libver="latest") as file:
            file.create_dataset("measurements", data=np.arange(32, dtype="<u4"))
        self.inputs = self.folder / "inputs"
        self.inputs.mkdir()
        self.damaged = self.inputs / "damage.h5"
        shutil.copyfile(self.reference, self.damaged)
        with h5py.File(self.damaged, "r") as file:
            offset = file["measurements"].id.get_offset()
        with self.damaged.open("r+b") as stream:
            stream.seek(offset + 4 * 6)
            old = stream.read(1)
            stream.seek(offset + 4 * 6)
            stream.write(bytes([old[0] ^ 1]))
        self.manifest = self.folder / "intake.json"
        self._write_intake()

    def _write_intake(self, cases=None):
        if cases is None:
            cases = [{"id": "field-01", "incident_id": "event-one",
                      "path": "inputs/damage.h5", "sha256": digest(self.damaged),
                      "size_bytes": self.damaged.stat().st_size,
                      "dataset": "/measurements", "declared_cause": "payload",
                      "provenance": "constructed evaluator self-check, not a natural case",
                      "consent": {"local_processing": True, "authorized_by": "test owner",
                                  "recorded_on": "2026-09-27"}}]
        self.manifest.write_text(json.dumps({"schema_version": 1, "cohort": "self-check",
                                             "cases": cases}), encoding="utf-8")

    def _truth_file(self):
        manifest = self.folder / "private" / "truth.json"
        manifest.write_text(json.dumps({"schema_version": 1, "cases": [{
            "id": "field-01", "kind": "healthy_file", "path": "original.h5",
            "sha256": digest(self.reference), "size_bytes": self.reference.stat().st_size,
            "dataset": "/measurements", "provenance": "evaluator-only original in synthetic test",
        }]}), encoding="utf-8")
        return manifest

    def test_wrong_historical_value_detected_only_after_separate_truth(self) -> None:
        workspace = self.folder / "work"
        result = run_incidents(self.manifest, workspace)
        self.assertEqual(result["truth_status"],
                         "not supplied to recovery or run stage; no historical accuracy scored")
        self.assertEqual(result["run_outcomes"]["output"], 1)
        self.assertNotIn(str(self.reference), (workspace / "run.json").read_text())
        truth = self._truth_file()
        score = score_incidents(workspace / "run.json", digest(workspace / "run.json"),
                                truth, digest(truth))
        self.assertEqual((score["exact_accepted_elements"],
                          score["wrong_accepted_elements"], score["unknown_elements"]),
                         (31, 1, 0))
        self.assertEqual(score["scorable_output_cases"], 1)
        self.assertEqual(score["verified_elements_in_outputs"], 32)
        with self.assertRaisesRegex(IncidentError, "run receipt differs"):
            score_incidents(workspace / "run.json", "0" * 64, truth, digest(truth))

    def test_missing_and_partial_truth_are_not_counted_as_success(self) -> None:
        workspace = self.folder / "work"
        run_incidents(self.manifest, workspace)
        empty = self.folder / "private" / "no_truth.json"
        empty.write_text('{"schema_version":1,"cases":[]}', encoding="utf-8")
        result = score_incidents(workspace / "run.json", digest(workspace / "run.json"),
                                 empty, digest(empty))
        self.assertEqual((result["unscorable_cases"], result["scorable_output_cases"]), (1, 0))
        prior = self.folder / "private" / "prior.json"
        captured = capture_truth_hashes(self.reference, "/measurements", "field-01",
                                        "self-check hashes retained before controlled edit", prior)
        self.assertEqual(captured["element_count"], 32)
        partial = json.loads(prior.read_text())
        partial["cases"][0]["element_sha256"] = {
            key: partial["cases"][0]["element_sha256"][key] for key in ("0", "6")}
        prior.write_text(json.dumps(partial), encoding="utf-8")
        result = score_incidents(workspace / "run.json", digest(workspace / "run.json"),
                                 prior, digest(prior))
        self.assertEqual(result["cases"][0]["scoring"], "partial")
        self.assertEqual((result["exact_accepted_elements"], result["wrong_accepted_elements"],
                          result["unverified_accepted_elements"]), (1, 1, 30))
        with (workspace / "cases" / "field-01" / "output.h5").open("ab") as stream:
            stream.write(b"post-run tampering")
        without_truth = score_incidents(workspace / "run.json", digest(workspace / "run.json"),
                                        empty, digest(empty))
        self.assertEqual(without_truth["invalid_cases"], 1)
        self.assertEqual(without_truth["unscorable_cases"], 0)

    def test_refusal_denominator_and_immutable_receipt(self) -> None:
        refused = self.inputs / "no_signature.h5"
        shutil.copyfile(self.reference, refused)
        with refused.open("r+b") as stream:
            stream.write(b"X")
        raw = json.loads(self.manifest.read_text())["cases"][0]
        refusal = dict(raw, id="field-02", incident_id="event-one",
                       path="inputs/no_signature.h5", sha256=digest(refused),
                       size_bytes=refused.stat().st_size, declared_cause="metadata")
        self._write_intake([raw, refusal])
        workspace = self.folder / "work"
        run = run_incidents(self.manifest, workspace)
        self.assertEqual(run["distinct_incident_ids"], 1)
        self.assertEqual(run["run_outcomes"], {"output": 1, "safe_refusal": 1,
                                              "timeout": 0, "protocol_failure": 0})
        truth = self._truth_file()
        doc = json.loads(truth.read_text())
        doc["cases"].append(dict(doc["cases"][0], id="field-02"))
        truth.write_text(json.dumps(doc), encoding="utf-8")
        score = score_incidents(workspace / "run.json", digest(workspace / "run.json"),
                                truth, digest(truth))
        self.assertEqual(score["safe_refusal_cases_with_truth"], 1)
        self.assertEqual(score["refused_elements"], 32)
        with (workspace / "cases" / "field-01" / "output.h5").open("ab") as stream:
            stream.write(b"tamper")
        amended = score_incidents(workspace / "run.json", digest(workspace / "run.json"),
                                   truth, digest(truth))
        self.assertEqual(amended["invalid_cases"], 1)

    def test_consent_path_and_truth_pin_validation(self) -> None:
        row = json.loads(self.manifest.read_text())["cases"][0]
        row["consent"]["local_processing"] = False
        self._write_intake([row])
        with self.assertRaisesRegex(IncidentError, "permission"):
            load_incidents(self.manifest)
        row["consent"]["local_processing"] = True
        row["path"] = "../private/original.h5"
        self._write_intake([row])
        with self.assertRaisesRegex(IncidentError, "safe relative"):
            load_incidents(self.manifest)
        row["path"] = "inputs/damage.h5"
        self._write_intake([row])
        workspace = self.folder / "work"
        run_incidents(self.manifest, workspace)
        truth = self._truth_file()
        with self.assertRaisesRegex(IncidentError, "truth manifest differs"):
            score_incidents(workspace / "run.json", digest(workspace / "run.json"),
                            truth, "0" * 64)


if __name__ == "__main__":
    unittest.main()
