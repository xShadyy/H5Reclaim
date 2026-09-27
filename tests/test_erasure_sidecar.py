"""Publicly staged multiple-loss parity on a real generated HDF5 index."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.baseline import capture_baseline
from h5reclaim.erasure_sidecar import capture_erasure_sidecar, restore_from_erasure
from h5reclaim.recovery import RecoveryError, sha256_file


class ErasureSidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.original = self.root / "original.h5"
        self.truth = np.arange(8 * 16, dtype="<u4") * 9 + 7
        with h5py.File(self.original, "w", libver="latest") as file:
            file.create_dataset("science", data=self.truth, chunks=(16,))
        self.baseline = self.root / "baseline.json"
        self.sidecar = self.root / "erasure.zip"
        capture_baseline(self.original, "/science", self.baseline)
        self.capture = capture_erasure_sidecar(
            self.original, "/science", self.baseline, self.sidecar,
            stripe_width=4, parity_shards=2,
        )
        self.damaged = self.root / "damaged.h5"
        shutil.copyfile(self.original, self.damaged)

    def _flip(self, chunk_indices: tuple[int, ...]) -> None:
        with h5py.File(self.original, "r") as file:
            selected = file["/science"]
            positions = [selected.id.get_chunk_info(index).byte_offset for index in chunk_indices]
        with self.damaged.open("r+b") as stream:
            for position in positions:
                stream.seek(position)
                previous = stream.read(1)
                stream.seek(position)
                stream.write(bytes([previous[0] ^ 0x20]))

    def _manifest(self, *, archive: Path | None = None) -> Path:
        archive = archive or self.sidecar
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({
            "schema_version": 1, "damaged_sha256": sha256_file(self.damaged),
            "baseline": {"path": str(self.baseline), "sha256": sha256_file(self.baseline)},
            "erasure": {"path": str(archive), "sha256": self.capture["archive_sha256"]},
        }), encoding="utf-8")
        return manifest

    def test_two_broken_chunks_in_one_stripe_reconstructed_exactly(self) -> None:
        self._flip((0, 2))
        before = sha256_file(self.damaged)
        report = restore_from_erasure(self.damaged, "/science", self._manifest(),
                                      self.root / "result.h5", self.root / "result.json")
        self.assertTrue(report["complete"])
        self.assertEqual(report["reconstructed_from_erasure"], 2)
        self.assertEqual(report["counts"]["recovered"], 8)
        with h5py.File(self.root / "result.h5", "r") as file:
            self.assertEqual(file["/science"][...].tobytes(), self.truth.tobytes())
            self.assertTrue(np.all(file["/_h5reclaim/chunk_status"][...] == 1))
            self.assertEqual(json.loads(file["/_h5reclaim/report_json"][()]), report)
        self.assertEqual(sha256_file(self.damaged), before)

    def test_three_losses_exceed_two_shards_and_become_unknown(self) -> None:
        self._flip((0, 1, 2))
        report = restore_from_erasure(self.damaged, "/science", self._manifest(),
                                      self.root / "partial.h5", self.root / "partial.json")
        self.assertFalse(report["complete"])
        self.assertEqual(report["reconstructed_from_erasure"], 0)
        self.assertEqual(report["counts"]["recovered"], 5)
        with h5py.File(self.root / "partial.h5", "r") as file:
            self.assertTrue(np.all(file["/_h5reclaim/chunk_status"][:3] != 1))
            self.assertTrue(np.all(file["/science"][:48] == 0))
            self.assertEqual(file["/science"][48:].tobytes(), self.truth[48:].tobytes())

    def test_tampered_sidecar_pin_refuses_without_publication(self) -> None:
        self._flip((0, 2))
        tampered = self.root / "tampered.zip"
        raw = bytearray(self.sidecar.read_bytes())
        raw[0] ^= 1
        tampered.write_bytes(raw)
        manifest = self._manifest(archive=tampered)
        with self.assertRaisesRegex(RecoveryError, "SHA-256"):
            restore_from_erasure(self.damaged, "/science", manifest,
                                 self.root / "bad.h5", self.root / "bad.json")
        self.assertFalse((self.root / "bad.h5").exists())
        self.assertFalse((self.root / "bad.json").exists())


if __name__ == "__main__":
    unittest.main()
