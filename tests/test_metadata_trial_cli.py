"""Public metadata trial uses damaged input alone and publishes source evidence."""

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


class MetadataTrialCliTests(unittest.TestCase):
    def test_one_broken_chunk_dimension_byte_public_rescue(self) -> None:
        from h5reclaim.header_dimension_trial import _selected_dimension_field
        from h5reclaim.modern_indexes import ModernH5File

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            damaged, output, report_path = (root / name for name in ("broken.h5", "output.h5", "report.json"))
            truth = np.arange(32, dtype="<u4")
            with h5py.File(damaged, "w", libver="latest") as file:
                dataset = file.create_dataset("science", data=truth, chunks=(8,))
                object_address = int(h5py.h5o.get_info(dataset.id).addr)
            with ModernH5File(damaged) as reader:
                start, _, field, _, _ = _selected_dimension_field(reader, object_address,
                                                                  require_mismatch=False)
            raw = bytearray(damaged.read_bytes())
            raw[start + field.start] ^= 4
            damaged.write_bytes(raw)
            original_digest = hashlib.sha256(raw).hexdigest()
            completed = subprocess.run(
                [sys.executable, "-m", "h5reclaim", "rescue", str(damaged),
                 "--dataset", "/science", "--metadata-trial", "dimension",
                 "--output", str(output), "--report", str(report_path)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["metadata_correction"]["dimension_after"], 8)
            self.assertEqual(report["source"]["sha256_before"], original_digest)
            self.assertEqual(hashlib.sha256(damaged.read_bytes()).hexdigest(), original_digest)
            with h5py.File(output, "r") as file:
                self.assertEqual(file["/science"][...].tobytes(), truth.tobytes())
                self.assertEqual(json.loads(file["/_h5reclaim/report_json"][()]), report)

    def test_one_broken_root_byte_public_rescue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            damaged, output, report_path = (root / name for name in ("broken.h5", "output.h5", "report.json"))
            truth = np.arange(32, dtype="<u4")
            with h5py.File(damaged, "w", libver="latest") as file:
                file.create_dataset("science", data=truth, chunks=(8,))
            raw = bytearray(damaged.read_bytes())
            raw[36] ^= 4
            damaged.write_bytes(raw)
            original_digest = hashlib.sha256(raw).hexdigest()
            completed = subprocess.run(
                [sys.executable, "-m", "h5reclaim", "rescue", str(damaged),
                 "--dataset", "/science", "--metadata-trial", "root",
                 "--output", str(output), "--report", str(report_path)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["mode"], "metadata_trial_readable_export")
            self.assertEqual(report["metadata_correction"]["changed_bytes"], 1)
            self.assertEqual(report["source"]["sha256_before"], original_digest)
            self.assertEqual(hashlib.sha256(damaged.read_bytes()).hexdigest(), original_digest)
            with h5py.File(output, "r") as file:
                self.assertEqual(file["/science"][...].tobytes(), truth.tobytes())
                self.assertEqual(json.loads(file["/_h5reclaim/report_json"][()]), report)


if __name__ == "__main__":
    unittest.main()
