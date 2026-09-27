"""Evaluator self-checks; run with `python -m unittest benchmarks.test_heldout_trials`."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from .run_heldout_trials import PanelError, _score, digest, load_panel, run_panel


class HeldOutPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def _manifest(self, *, include_contiguous: bool = False) -> Path:
        entries = []
        for identifier in (("chunked", "contiguous") if include_contiguous else ("chunked",)):
            path = self.base / f"{identifier}.h5"
            with h5py.File(path, "w", libver="latest") as handle:
                options = ({"chunks": (8,), "fletcher32": True}
                           if identifier == "chunked" else {})
                handle.create_dataset("science", data=np.arange(32, dtype="<u4"), **options)
            entries.append({"id": identifier, "path": path.name,
                            "sha256": digest(path), "size_bytes": path.stat().st_size,
                            "dataset": "/science", "provenance": "synthetic evaluator self-check"})
        manifest = self.base / "manifest.json"
        manifest.write_text(json.dumps({"schema_version": 1, "cohort": "self-check",
                                        "entries": entries}), encoding="utf-8")
        return manifest

    def test_denominator_false_accept_and_oracle_tamper(self) -> None:
        manifest = self._manifest(include_contiguous=True)
        summary = run_panel(manifest, self.base / "runs",
                            faults=("intact", "object_header", "payload_bit"), seed=47)
        self.assertEqual((summary["planned_cases"], summary["eligible_cases"],
                          summary["excluded_cases"]), (6, 6, 0))
        self.assertEqual(summary["strata"]["intact"]["outcomes"]["all_exact"], 2)
        self.assertEqual(summary["strata"]["object_header"]["outcomes"]["safe_refusal"], 2)
        self.assertEqual(summary["strata"]["payload_bit"]["outcomes"]["false_accept"], 1)
        self.assertFalse(summary["no_false_accept_or_protocol_failure"])
        for stratum in summary["strata"].values():
            self.assertEqual(stratum["logical_elements_in_eligible_cases"],
                             sum(stratum[key] for key in (
                                 "exact_accepted_elements", "wrong_accepted_elements",
                                 "unknown_elements", "refused_elements", "unscored_elements")))
        case = next(item for item in summary["cases"] if item["case"] == "chunked_intact_000")
        folder = self.base / "runs" / "cases" / case["case"]
        with h5py.File(folder / "output.h5", "r+") as handle:
            handle["/science"][0] = np.uint32(999)
        # Score with the same entry as before, after a post-publication bit change.
        _hash, _cohort, entries = load_panel(manifest)
        scored = _score(entries[0], folder / "damaged.h5", folder / "output.h5",
                        folder / "report.json")
        self.assertEqual(scored["wrong_accepted_elements"], 1)
        self.assertEqual(scored["exact_elements"], 31)

    def test_manifest_hash_and_eligibility_exclusions(self) -> None:
        manifest = self._manifest()
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["entries"][0]["sha256"] = "0" * 64
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(PanelError, "declared size or SHA-256"):
            run_panel(manifest, self.base / "invalid", faults=("intact",))
        self.assertFalse((self.base / "invalid").exists())

    def test_compact_payload_exclusion_is_visible_in_planned_denominator(self) -> None:
        path = self.base / "compact.h5"
        access = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
        access.set_libver_bounds(h5py.h5f.LIBVER_LATEST, h5py.h5f.LIBVER_LATEST)
        file_id = h5py.h5f.create(bytes(path), h5py.h5f.ACC_TRUNC, fapl=access)
        create = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        create.set_layout(h5py.h5d.COMPACT)
        space = h5py.h5s.create_simple((4,))
        dtype = h5py.h5t.py_create(np.dtype("<u4"))
        dataset = h5py.h5d.create(file_id, b"science", dtype, space, dcpl=create)
        dataset.write(h5py.h5s.ALL, h5py.h5s.ALL, np.arange(4, dtype="<u4"))
        dataset.close()
        file_id.close()
        manifest = self.base / "compact-panel.json"
        manifest.write_text(json.dumps({"schema_version": 1, "cohort": "compact-self-check",
                                        "entries": [{"id": "compact", "path": path.name,
                                                     "sha256": digest(path), "size_bytes": path.stat().st_size,
                                                     "dataset": "/science", "provenance": "synthetic self-check"}]}),
                            encoding="utf-8")
        result = run_panel(manifest, self.base / "compact-runs",
                           faults=("intact", "payload_bit"))
        self.assertEqual((result["planned_cases"], result["eligible_cases"],
                          result["excluded_cases"]), (2, 1, 1))
        self.assertIn("compact", result["cases"][1]["exclusion_reason"])


if __name__ == "__main__":
    unittest.main()
