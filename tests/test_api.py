"""The public Python entry point shares recovery and evidence behavior."""

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.api import rescue
from h5reclaim.capabilities import capabilities


class ApiTests(unittest.TestCase):
    def test_selected_recovery_and_json_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.h5"
            with h5py.File(source, "w") as handle:
                handle.create_dataset("data", data=np.arange(12), chunks=(4,))
            result = rescue(source, dataset="/data")
            self.assertEqual(result["outcome"], "complete")
            self.assertTrue((root / "source.recovered.h5").is_file())
            self.assertTrue((root / "source.recovered.report.json").is_file())
            self.assertEqual(capabilities()["large_structural"]["maximum_index_repairs_per_parent"], 2)
            self.assertTrue(capabilities()["large_structural"]["multiple_index_repairs_per_dataset"])
            self.assertEqual(capabilities()["reporting"], ["JSON", "terminal"])


if __name__ == "__main__":
    unittest.main()
