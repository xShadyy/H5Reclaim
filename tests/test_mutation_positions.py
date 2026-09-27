"""Vary the damaged parent slot while keeping benchmark truth separate."""

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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from h5reclaim.format import H5File  # noqa: E402
from tools.make_broken_link_fixture import make_damage  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MutationPositionTest(unittest.TestCase):
    def test_each_eligible_interior_root_child_recovers_exact_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            healthy = base / "truth" / "healthy.h5"
            healthy.parent.mkdir()
            values = np.random.default_rng(0x4835).integers(
                0, 2**32, size=(320, 256), dtype=np.uint32,
            )
            with h5py.File(healthy, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset(
                    "measurements", data=values, dtype="<u4", chunks=(16, 16),
                )
            healthy_hash = _sha256(healthy)

            with h5py.File(healthy, "r") as handle, H5File(healthy) as raw:
                dataset = handle["/measurements"]
                object_address = int(h5py.h5o.get_info(dataset.id).addr)
                root = raw.read_tree(raw.read_dataset_layout(object_address).root_address)
                self.assertEqual(root.level, 1)
                leaves = [raw.read_tree(entry.address) for entry in root.entries]
                eligible = [
                    index for index in range(1, len(leaves) - 1)
                    if leaves[index].entries
                    and leaves[index - 1].right_sibling == leaves[index].address
                    and leaves[index].left_sibling == leaves[index - 1].address
                    and leaves[index].right_sibling == leaves[index + 1].address
                    and leaves[index + 1].left_sibling == leaves[index].address
                ]
            self.assertGreaterEqual(len(eligible), 2)

            for index in eligible:
                with self.subTest(child_index=index):
                    case = base / f"slot-{index}"
                    case.mkdir()
                    damaged = case / "damaged.h5"
                    manifest_path = base / "truth" / f"slot-{index}.json"
                    manifest = make_damage(
                        healthy, damaged, manifest_path, "/measurements",
                        child_index=index,
                    )
                    self.assertEqual(manifest["parent_child_index"], index)
                    self.assertEqual(_sha256(healthy), healthy_hash)
                    self.assertEqual(_sha256(damaged), manifest["damaged_sha256"])
                    self.assertTrue(manifest_path.is_file())

                    output, report_path = case / "recovered.h5", case / "report.json"
                    environment = os.environ.copy()
                    environment["PYTHONPATH"] = (
                        str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
                    )
                    completed = subprocess.run(
                        [
                            sys.executable, "-m", "h5reclaim", "recover", str(damaged),
                            "--dataset", "/measurements", "--output", str(output),
                            "--report", str(report_path),
                        ],
                        cwd=case, env=environment, capture_output=True, text=True,
                        timeout=60, check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    self.assertEqual(_sha256(healthy), healthy_hash)
                    self.assertEqual(_sha256(damaged), manifest["damaged_sha256"])
                    self.assertTrue(report["complete"])
                    self.assertEqual(report["index"]["broken_links"], 1)
                    affected = {tuple(offset) for offset in manifest["affected_chunk_offsets"]}
                    self.assertEqual(report["reconstructed_chunks"], len(affected))
                    routes = {
                        tuple(mapping["coordinate"]): mapping["route"]
                        for mapping in report["mappings"]
                    }
                    self.assertEqual(len(routes), values.size // (16 * 16))
                    for coordinate, route in routes.items():
                        self.assertEqual(
                            route,
                            "reconstructed_link" if coordinate in affected else "intact_tree",
                        )
                    with h5py.File(output, "r") as result:
                        np.testing.assert_array_equal(result["/measurements"][...], values)
                        np.testing.assert_array_equal(
                            result["/_h5reclaim/chunk_status"][...],
                            np.ones((20, 16), dtype="u1"),
                        )

            invalid = base / "invalid"
            invalid.mkdir()
            with self.assertRaisesRegex(ValueError, "not an eligible interior leaf"):
                make_damage(
                    healthy, invalid / "damaged.h5", base / "truth" / "invalid.json",
                    "/measurements", child_index=0,
                )
            self.assertFalse((invalid / "damaged.h5").exists())
            self.assertFalse((base / "truth" / "invalid.json").exists())
            self.assertEqual(_sha256(healthy), healthy_hash)


if __name__ == "__main__":
    unittest.main()
