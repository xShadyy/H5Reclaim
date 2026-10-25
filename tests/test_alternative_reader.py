"""Alternative decoding never promotes unverified candidates to measurements."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.alternative_reader import export_alternative
from h5reclaim.preflight import inspect_metadata_local
from h5reclaim.masked_reader import read_masked


@unittest.skipIf(importlib.util.find_spec("pyfive") is None, "optional Pyfive reader not installed")
class AlternativeReaderTests(unittest.TestCase):
    def test_cross_reader_agreement_and_unverified_candidate_masks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, report = (root / n for n in ("source.h5", "output.h5", "report.json"))
            values = np.arange(120, dtype="i4").reshape(12, 10)
            with h5py.File(source, "w", libver=("earliest", "v108")) as handle:
                handle.create_dataset("data", data=values, chunks=(4, 5), compression="gzip")
            result = export_alternative(source, "/data", output, report)
            self.assertEqual(result["accepted_elements"], 120)
            np.testing.assert_array_equal(read_masked(output, report, "/data").compressed(), values.ravel())
            output.unlink()
            report.unlink()
            def incomplete(path, **kwargs):
                result = inspect_metadata_local(path, **kwargs)
                result["analysis"]["complete"] = False
                return result
            with patch("h5reclaim.alternative_reader.inspect_metadata_local", side_effect=incomplete):
                result = export_alternative(source, "/data", output, report)
            self.assertEqual(result["accepted_elements"], 0)
            self.assertEqual(result["candidate_elements"], 120)
            self.assertTrue(read_masked(output, report, "/data").mask.all())

    def test_optional_reader_runs_through_the_isolated_cli_route(self):
        from h5reclaim.route_worker import run_route
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, report = (root / n for n in ("source.h5", "output.h5", "report.json"))
            with h5py.File(source, "w", libver=("earliest", "v108")) as handle:
                handle.create_dataset("data", data=np.arange(12), chunks=(4,))
            result = run_route("alternative_reader", output, report, source=str(source), dataset="/data")
            self.assertEqual(result["accepted_elements"], 12)


if __name__ == "__main__":
    unittest.main()
