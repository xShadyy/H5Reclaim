"""A public partial-recovery report can export only its unresolved raw bytes."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import h5py

from h5reclaim.recovery import recover


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "corpus/files/H-H1_GWOSC_4KHZ_R1-1126259447-32.hdf5"


class FragmentCliTests(unittest.TestCase):
    def test_partial_report_exports_decoding_failure_without_claiming_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            damaged = folder / "damaged.h5"
            shutil.copyfile(SOURCE, damaged)
            with h5py.File(damaged, "r") as handle:
                offset = handle["/strain/Strain"].id.get_chunk_info(0).byte_offset
            with damaged.open("r+b") as handle:
                handle.seek(offset)
                handle.write(b"\x00")
            before = hashlib.sha256(damaged.read_bytes()).hexdigest()
            output, report_path, archive = (folder / name for name in
                                            ("partial.h5", "report.json", "fragments.zip"))
            result = recover(damaged, "/strain/Strain", output, report_path)
            self.assertEqual(result["outcome"], "partial")
            self.assertEqual(result["counts"]["decode_failed"], 1)
            command = [sys.executable, "-m", "h5reclaim", "export-fragments",
                       str(report_path), str(damaged), "--output", str(archive)]
            completed = subprocess.run(command, cwd=ROOT, capture_output=True,
                                       text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("unresolved raw fragment", completed.stdout)
            with zipfile.ZipFile(archive) as bundle:
                manifest = json.loads(bundle.read("manifest.json"))
                self.assertEqual(len(manifest["fragments"]), 1)
                fragment = manifest["fragments"][0]
                self.assertEqual(fragment["decision"]["status"], "unassigned")
                self.assertTrue(fragment["proposed_coordinate_unverified"] is not None)
                self.assertEqual(hashlib.sha256(bundle.read(fragment["filename"])).hexdigest(),
                                 fragment["sha256"])
            self.assertEqual(hashlib.sha256(damaged.read_bytes()).hexdigest(), before)
            altered = folder / "wrong.h5"
            shutil.copyfile(damaged, altered)
            with altered.open("r+b") as handle:
                handle.seek(offset + 10)
                byte = handle.read(1)
                handle.seek(offset + 10)
                handle.write(bytes((byte[0] ^ 1,)))
            rejected = subprocess.run([*command[:5], str(altered), "--output",
                               str(folder / "rejected.zip")], cwd=ROOT,
                               capture_output=True, text=True, check=False)
            self.assertEqual(rejected.returncode, 2)
            self.assertFalse((folder / "rejected.zip").exists())


if __name__ == "__main__":
    unittest.main()
