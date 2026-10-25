"""A failed file does not discard the rest of a batch or cached evidence."""

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.batch import batch_rescue


class BatchTests(unittest.TestCase):
    def test_batch_failure_and_hash_checked_resumption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, bad = root / "good.h5", root / "bad.h5"
            with h5py.File(source, "w") as handle:
                handle.create_dataset("data", data=np.arange(12), chunks=(4,))
            bad.write_bytes(b"no HDF5 metadata")
            summary, outputs = root / "summary.json", root / "outputs"
            result = batch_rescue([source, bad], outputs, summary, jobs=2)
            self.assertEqual(result["counts"], {"complete": 1, "partial": 0, "failed": 1})
            self.assertTrue(Path(result["results"][0]["output"]).is_file())
            self.assertTrue(Path(result["results"][0]["report"]).is_file())
            reused = batch_rescue([source], outputs, summary, resume=True)
            self.assertTrue(reused["results"][0]["reused"])
            Path(reused["results"][0]["report"]).write_text("changed")
            with self.assertRaises(ValueError):
                batch_rescue([source], outputs, summary, resume=True)


if __name__ == "__main__":
    unittest.main()
