"""End-to-end public command dispatch for the new prospective and scale routes."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class V09CliRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.healthy = self.root / "healthy.h5"
        self.truth = np.arange(128, dtype="<u4")
        with h5py.File(self.healthy, "w", libver="latest") as file:
            file.create_dataset("science", data=self.truth, chunks=(16,))

    def _command(self, *args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "h5reclaim", *map(str, args)],
                              capture_output=True, text=True, check=False, timeout=40)

    def test_capsule_restores_after_root_loss_and_requires_pin(self) -> None:
        capsule, broken = self.root / "capsule.zip", self.root / "broken.h5"
        result = self._command("capture-capsule", self.healthy,
                               "--dataset", "/science", "--output", capsule)
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = bytearray(self.healthy.read_bytes())
        raw[36] ^= 4
        broken.write_bytes(raw)
        refused = self._command("rescue", broken, "--dataset", "/science",
                                "--capsule", capsule, "--capsule-sha256", "0" * 64,
                                "--output", self.root / "refused.h5",
                                "--report", self.root / "refused.json")
        self.assertEqual(refused.returncode, 2)
        self.assertFalse((self.root / "refused.h5").exists())
        output, evidence = self.root / "restored.h5", self.root / "restored.json"
        restored = self._command("rescue", broken, "--dataset", "/science",
                                 "--capsule", capsule, "--capsule-sha256", _sha(capsule),
                                 "--output", output, "--report", evidence)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        report = json.loads(evidence.read_text(encoding="utf-8"))
        self.assertEqual(report["operation"], "prospective_recovery_capsule")
        self.assertEqual(report["source"]["sha256_before"], _sha(broken))
        with h5py.File(output, "r") as file:
            self.assertEqual(file["/science"][...].tobytes(), self.truth.tobytes())
            self.assertEqual(json.loads(file["/_h5reclaim/report_json"][()]), report)

    def test_two_erasure_shards_rebuild_two_damaged_chunks(self) -> None:
        baseline, parity = self.root / "baseline.json", self.root / "parity.zip"
        result = self._command("capture-baseline", self.healthy,
                               "--dataset", "/science", "--output", baseline)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self._command("capture-erasure", self.healthy, "--dataset", "/science",
                               "--baseline", baseline, "--parity-shards", 2,
                               "--stripe-width", 4, "--output", parity)
        self.assertEqual(result.returncode, 0, result.stderr)
        damaged = self.root / "damaged.h5"
        shutil.copyfile(self.healthy, damaged)
        with h5py.File(self.healthy, "r") as file:
            offsets = [file["/science"].id.get_chunk_info(index).byte_offset for index in (0, 2)]
        with damaged.open("r+b") as handle:
            for offset in offsets:
                handle.seek(offset)
                value = handle.read(1)
                handle.seek(offset)
                handle.write(bytes([value[0] ^ 0x20]))
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({
            "schema_version": 1, "damaged_sha256": _sha(damaged),
            "baseline": {"path": str(baseline), "sha256": _sha(baseline)},
            "erasure": {"path": str(parity), "sha256": _sha(parity)},
        }), encoding="utf-8")
        output, evidence = self.root / "recovered.h5", self.root / "recovery.json"
        result = self._command("rescue", damaged, "--dataset", "/science",
                               "--erasure", manifest, "--output", output, "--report", evidence)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(evidence.read_text())["reconstructed_from_erasure"], 2)
        with h5py.File(output, "r") as file:
            self.assertEqual(file["/science"][...].tobytes(), self.truth.tobytes())

    def test_large_readable_route_is_labeled_current_value_copy(self) -> None:
        output, report = self.root / "large-copy.h5", self.root / "large-copy.json"
        result = self._command("rescue", self.healthy, "--dataset", "/science",
                               "--large-readable", "--output", output, "--report", report)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Route: streamed native-readable data export.", result.stdout)
        self.assertEqual(json.loads(report.read_text())["operation"], "large_native_readable_export")
        with h5py.File(output, "r") as file:
            self.assertEqual(file["/science"][...].tobytes(), self.truth.tobytes())


if __name__ == "__main__":
    unittest.main()
