"""Independent checks of the controlled corruption experiment."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import H5File


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tools" / "make_healthy_fixture.py"
DAMAGE = ROOT / "tools" / "make_broken_link_fixture.py"


def command(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        env=environment,
    )


class BrokenLinkFixtureTest(unittest.TestCase):
    def test_verified_single_pointer_damage_and_standard_reader_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            clean = base / "healthy.h5"
            damaged = base / "damaged.h5"
            manifest = base / "truth" / "damage.json"
            generated = command(
                GENERATOR,
                "--output", str(clean),
                "--rows", "12", "--cols", "12",
                "--chunk-rows", "1", "--chunk-cols", "1",
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            original = clean.read_bytes()
            attempt = command(
                DAMAGE,
                "--input", str(clean),
                "--output", str(damaged),
                "--manifest", str(manifest),
            )
            self.assertEqual(attempt.returncode, 0, attempt.stderr)
            details = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(clean.read_bytes(), original)
            after = damaged.read_bytes()
            self.assertEqual(len(after), len(original))
            different = [i for i, (x, y) in enumerate(zip(original, after)) if x != y]
            self.assertEqual(different, details["modified_byte_offsets"])
            self.assertTrue(different)
            pointer = details["pointer_absolute_byte_offset"]
            self.assertTrue(all(pointer <= i < pointer + len(bytes.fromhex(details["pointer_after_hex"])) for i in different))
            self.assertEqual(after[pointer:pointer + 8], b"\xff" * 8)
            self.assertEqual(details["source_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(details["damaged_sha256"], hashlib.sha256(after).hexdigest())
            self.assertEqual(details["chunk_count"], 144)
            self.assertGreater(len(details["affected_chunk_offsets"]), 0)
            observation = details["standard_reader_observation"]
            self.assertGreater(
                observation["affected_wrong_values"] + observation["affected_read_errors"], 0
            )
            with h5py.File(clean, "r") as pristine, h5py.File(damaged, "r") as broken:
                expected = pristine["/measurements"]
                actual = broken["/measurements"]
                row, col = details["unaffected_check_offset"]
                np.testing.assert_array_equal(actual[row:row+1, col:col+1], expected[row:row+1, col:col+1])

            second = command(
                DAMAGE,
                "--input", str(clean),
                "--output", str(damaged),
                "--manifest", str(manifest),
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(clean.read_bytes(), original)
            self.assertEqual(damaged.read_bytes(), after)

    def test_filtered_dataset_is_refused_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            clean = base / "filtered.h5"
            damaged = base / "damaged.h5"
            manifest = base / "truth.json"
            with h5py.File(clean, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset(
                    "measurements",
                    data=np.arange(144, dtype="<u4").reshape(12, 12),
                    chunks=(1, 1),
                    compression="gzip",
                )
            before = clean.read_bytes()
            attempted = command(
                DAMAGE,
                "--input", str(clean),
                "--output", str(damaged),
                "--manifest", str(manifest),
            )
            self.assertNotEqual(attempted.returncode, 0)
            self.assertIn("unfiltered", attempted.stderr)
            self.assertEqual(clean.read_bytes(), before)
            self.assertFalse(damaged.exists())
            self.assertFalse(manifest.exists())

    def test_broken_reciprocal_sibling_does_not_get_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source_path = base / "bad_sibling.h5"
            generated = command(
                GENERATOR,
                "--output", str(source_path),
                "--rows", "12", "--cols", "12",
                "--chunk-rows", "1", "--chunk-cols", "1",
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            with h5py.File(source_path, "r") as file, H5File(source_path) as raw:
                object_address = h5py.h5o.get_info(file["/measurements"].id).addr
                root = raw.read_tree(raw.read_dataset_layout(object_address).root_address)
                self.assertEqual(len(root.entries), 3)
                center = raw.read_tree(root.entries[1].address)
                self.assertIsNotNone(center.left_sibling)
                sibling_byte = raw.absolute(center.address) + 8
                offset_size = raw.superblock.offset_size
            modified = bytearray(source_path.read_bytes())
            modified[sibling_byte : sibling_byte + offset_size] = b"\xff" * offset_size
            source_path.write_bytes(modified)
            source_hash = hashlib.sha256(modified).hexdigest()
            damaged = base / "damaged.h5"
            truth = base / "truth.json"
            attempted = command(
                DAMAGE,
                "--input", str(source_path),
                "--output", str(damaged),
                "--manifest", str(truth),
            )
            self.assertNotEqual(attempted.returncode, 0)
            self.assertIn("reciprocal", attempted.stderr)
            self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), source_hash)
            self.assertFalse(damaged.exists())
            self.assertFalse(truth.exists())


if __name__ == "__main__":
    unittest.main()
