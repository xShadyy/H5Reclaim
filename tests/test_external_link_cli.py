"""The guided public route resolves only a pinned external HDF5 link."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np


class ExternalLinkCliTests(unittest.TestCase):
    def test_rescue_linked_dataset_through_pinned_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.h5", root / "target.h5"
            with h5py.File(target, "w") as file:
                file.create_dataset("science", data=np.arange(6, dtype="<u4"), chunks=(3,))
            with h5py.File(source, "w") as file:
                file["external"] = h5py.ExternalLink("declared-but-not-resolved.h5", "/science")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            manifest = root / "related.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "files": [{"declared_name": "declared-but-not-resolved.h5",
                           "path": str(target),
                           "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}],
            }), encoding="utf-8")
            output, report = root / "out.h5", root / "out.json"
            result = subprocess.run([
                sys.executable, "-m", "h5reclaim", "rescue", str(source),
                "--dataset", "/external", "--related-files", str(manifest),
                "--output", str(output), "--report", str(report),
            ], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(report.read_text())
            self.assertEqual(evidence["mode"], "external_link_export")
            self.assertEqual(evidence["source"]["sha256_before"], digest)
            self.assertEqual(evidence["dataset"]["path"], "/external")
            with h5py.File(output, "r") as file:
                np.testing.assert_array_equal(file["external"][:], np.arange(6, dtype="<u4"))
                self.assertEqual(json.loads(file["/_h5reclaim/report_json"][()]), evidence)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
