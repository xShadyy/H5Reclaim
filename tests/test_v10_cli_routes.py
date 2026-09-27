"""Public opt-in large structural route keeps sparse uncertainty visible."""

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

from h5reclaim.metadata import read_dataset_spec
from h5reclaim.modern_indexes import ModernH5File


class V10CliRoutesTests(unittest.TestCase):
    def test_checked_fixed_array_link_public_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            damaged, output, report_path = (base / name for name in
                                            ("damaged.h5", "recovered.h5", "report.json"))
            with h5py.File(damaged, "w", libver="latest") as file:
                dataset = file.create_dataset("science", shape=(64,), chunks=(1,), dtype="u1")
                dataset[0], dataset[32], dataset[63] = 17, 91, 211
            spec = read_dataset_spec(damaged, "/science")
            with ModernH5File(damaged) as reader:
                index = reader.read_index(spec.object_address, spec.shape, spec.chunks, 1,
                                          maxshape=spec.maxshape, filters=spec.filters)
            self.assertEqual(index.index_type, "fixed_array")
            raw = bytearray(damaged.read_bytes())
            raw[index.data_block_pointer_offset] ^= 0x67
            damaged.write_bytes(raw)
            damaged_hash = hashlib.sha256(raw).hexdigest()
            completed = subprocess.run(
                [sys.executable, "-m", "h5reclaim", "rescue", str(damaged),
                 "--dataset", "/science", "--large-structural",
                 "--output", str(output), "--report", str(report_path)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["operation"], "large_checked_fixed_array_pointer_recovery")
            self.assertEqual(report["allocated_chunks_checked"], 3)
            self.assertEqual(report["accepted_elements"], 3)
            self.assertEqual(report["unknown_elements"], 61)
            self.assertEqual(hashlib.sha256(damaged.read_bytes()).hexdigest(), damaged_hash)
            with h5py.File(output, "r") as file:
                validity = file["/_h5reclaim/validity"][:]
                self.assertEqual(int(np.sum(validity)), 3)
                for coordinate, value in ((0, 17), (32, 91), (63, 211)):
                    self.assertEqual(int(validity[coordinate]), 1)
                    self.assertEqual(int(file["science"][coordinate]), value)
                self.assertEqual(int(validity[1]), 0)
                self.assertEqual(json.loads(file["/_h5reclaim/report_json"][()]), report)


if __name__ == "__main__":
    unittest.main()
