"""Tool bridges isolate input copies and never publish a modified source view."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.external_tools import export_with_tool
from h5reclaim.recovery import RecoveryError


class ExternalToolTests(unittest.TestCase):
    def test_modified_disposable_tool_input_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, report = (root / n for n in ("source.h5", "output.h5", "report.json"))
            with h5py.File(source, "w") as handle:
                handle.create_dataset("data", data=np.arange(10))
            before = source.read_bytes()
            def modified_worker(command, **kwargs):
                request, response = map(Path, command[-2:])
                doc = json.loads(request.read_text())
                image, staged = map(Path, doc["args"][-2:])
                shutil.copyfile(image, staged)
                with image.open("ab") as stream:
                    stream.write(b"unexpected tool write")
                response.write_text(json.dumps({"status": "completed", "output_metadata": {"datasets": 1}}))
                return subprocess.CompletedProcess(command, 0)
            with patch("h5reclaim.external_tools.run_worker", side_effect=modified_worker):
                with self.assertRaises(RecoveryError):
                    export_with_tool(source, output, report, executable="test-h5repack")
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(output.exists())

    @unittest.skipUnless(shutil.which("h5repack"), "official h5repack tool is not installed")
    def test_official_repack_retains_values_and_applies_gzip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, report = (root / n for n in ("source.h5", "output.h5", "report.json"))
            with h5py.File(source, "w", libver=("earliest", "v108")) as handle:
                handle.create_dataset("data", data=np.arange(1000))
            export_with_tool(source, output, report, compression="gzip")
            with h5py.File(output, "r") as handle:
                np.testing.assert_array_equal(handle["data"][:], np.arange(1000))
                self.assertEqual(handle["data"].compression, "gzip")


if __name__ == "__main__":
    unittest.main()
