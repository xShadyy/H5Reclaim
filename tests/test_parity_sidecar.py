"""Prospective parity must require a trusted prior hash for each accepted byte."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.baseline import capture_baseline
from h5reclaim.parity_sidecar import capture_parity_sidecar, restore_from_parity
from h5reclaim.recovery import RecoveryError


SELECTED = "/science/signal"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mutate(path: Path, origin: tuple[int, int]) -> None:
    with h5py.File(path, "r") as file:
        offset = file[SELECTED].id.get_chunk_info_by_coord(origin).byte_offset
    with path.open("r+b") as stream:
        stream.seek(offset)
        value = stream.read(1)
        stream.seek(offset)
        stream.write(bytes([value[0] ^ 0x55]))


class ParitySidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.original = root / "original.h5"
        self.damaged = root / "damaged.h5"
        self.baseline = root / "baseline.json"
        self.parity = root / "parity.zip"
        self.manifest = root / "recovery.json"
        self.output = root / "output.h5"
        self.report = root / "report.json"

    def capture(self, *, filtered: bool = False, edge: bool = False) -> dict:
        with h5py.File(self.original, "x", libver="latest") as file:
            shape = (5, 7) if edge else (6, 8)
            data = file.create_dataset(
                SELECTED, shape=shape, chunks=(3, 2), dtype="<u4",
                compression="gzip" if filtered else None,
                fletcher32=filtered,
            )
            data[...] = np.arange(np.prod(shape), dtype="<u4").reshape(shape) * 37 + 3
        capture_baseline(self.original, SELECTED, self.baseline)
        sidecar = capture_parity_sidecar(self.original, SELECTED, self.baseline, self.parity)
        shutil.copyfile(self.original, self.damaged)
        return sidecar

    def pin(self) -> None:
        document = {
            "schema_version": 1,
            "damaged_sha256": digest(self.damaged),
            "baseline": {"path": str(self.baseline), "sha256": digest(self.baseline)},
            "parity": {"path": str(self.parity), "sha256": digest(self.parity)},
        }
        self.manifest.write_text(json.dumps(document), encoding="utf-8")

    def restore(self) -> dict:
        return restore_from_parity(
            self.damaged, SELECTED, self.manifest, self.output, self.report,
        )

    def test_two_single_losses_in_different_stripes_restore_exact_chunks(self) -> None:
        captured = self.capture()
        self.assertEqual([len(s["members"]) for s in captured["stripes"]], [4, 4])
        mutate(self.damaged, (0, 2))
        mutate(self.damaged, (3, 4))
        self.pin()
        before = digest(self.damaged)
        result = self.restore()
        self.assertEqual(digest(self.damaged), before)
        self.assertEqual(result, json.loads(self.report.read_text()))
        self.assertTrue(result["complete"])
        self.assertEqual(result["reconstructed_from_parity"], 2)
        parity_rows = [row for row in result["mappings"] if row["source_id"] == "parity"]
        self.assertEqual([row["coordinate"] for row in parity_rows], [[0, 2], [3, 4]])
        self.assertTrue(all(len(row["parity"]["companion_evidence"]) == 3 for row in parity_rows))
        with h5py.File(self.original, "r") as truth, h5py.File(self.output, "r") as output:
            np.testing.assert_array_equal(output[SELECTED][...], truth[SELECTED][...])
            self.assertTrue(np.all(output["/_h5reclaim/chunk_status"][...] == 1))

    def test_filtered_failed_decode_and_edge_chunk_restore(self) -> None:
        self.capture(filtered=True, edge=True)
        mutate(self.damaged, (3, 6))
        self.pin()
        result = self.restore()
        self.assertTrue(result["complete"])
        self.assertEqual(result["reconstructed_from_parity"], 1)
        with h5py.File(self.original, "r") as truth, h5py.File(self.output, "r") as output:
            np.testing.assert_array_equal(output[SELECTED][...], truth[SELECTED][...])

    def test_missing_allocation_has_no_accepted_fill_and_restores_from_parity(self) -> None:
        self.capture()
        self.damaged.unlink()
        with h5py.File(self.original, "r") as truth, h5py.File(self.damaged, "x", libver="latest") as file:
            source = truth[SELECTED]
            destination = file.create_dataset(SELECTED, shape=source.shape,
                                              maxshape=source.maxshape, chunks=source.chunks,
                                              dtype=source.dtype)
            for row in range(0, 6, 3):
                for column in range(0, 8, 2):
                    if (row, column) != (0, 2):
                        destination[row:row + 3, column:column + 2] = source[
                            row:row + 3, column:column + 2]
        self.pin()
        result = self.restore()
        self.assertTrue(result["complete"])
        self.assertEqual(result["reconstructed_from_parity"], 1)
        with h5py.File(self.original, "r") as truth, h5py.File(self.output, "r") as output:
            np.testing.assert_array_equal(output[SELECTED][...], truth[SELECTED][...])

    def test_two_losses_same_stripe_remain_unknown(self) -> None:
        self.capture()
        mutate(self.damaged, (0, 0))
        mutate(self.damaged, (0, 2))
        self.pin()
        result = self.restore()
        self.assertFalse(result["complete"])
        self.assertEqual(result["reconstructed_from_parity"], 0)
        self.assertEqual(len(result["unresolved_chunks"]), 2)
        self.assertTrue(all("multiple unverified" in row["reason"]
                            for row in result["unresolved_chunks"]))
        with h5py.File(self.output, "r") as output:
            self.assertEqual(int(output["/_h5reclaim/chunk_status"][0, 0]), 4)
            self.assertEqual(int(output["/_h5reclaim/chunk_status"][0, 1]), 4)

    def test_missing_prior_hash_refuses_capture(self) -> None:
        self.capture()
        self.parity.unlink()
        baseline = json.loads(self.baseline.read_text())
        baseline["chunk_hashes"].pop()
        self.baseline.write_text(json.dumps(baseline), encoding="utf-8")
        with self.assertRaisesRegex(RecoveryError, "one prior hash"):
            capture_parity_sidecar(self.original, SELECTED, self.baseline, self.parity)
        self.assertFalse(self.parity.exists())

    def test_altered_source_or_baseline_at_capture_refused(self) -> None:
        self.capture()
        self.parity.unlink()
        mutate(self.original, (0, 0))
        with self.assertRaisesRegex(RecoveryError, "source hash differs"):
            capture_parity_sidecar(self.original, SELECTED, self.baseline, self.parity)
        self.assertFalse(self.parity.exists())

    def test_sidecar_tampering_without_pin_change_refused(self) -> None:
        self.capture()
        mutate(self.damaged, (0, 0))
        self.pin()
        with self.parity.open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaisesRegex(RecoveryError, "parity differs"):
            self.restore()
        self.assertFalse(self.output.exists())

    def test_wrong_parity_cannot_be_promoted_even_when_operator_repins(self) -> None:
        self.capture()
        mutate(self.damaged, (0, 0))
        # Alter the stripe and its internal checksum. The independently kept
        # baseline still rejects the reconstructed bytes after repinning.
        with zipfile.ZipFile(self.parity) as archive:
            payloads = {name: archive.read(name) for name in archive.namelist()}
        sidecar = json.loads(payloads["manifest.json"])
        stripe = sidecar["stripes"][0]
        altered = bytes([payloads[stripe["entry"]][0] ^ 1]) + payloads[stripe["entry"]][1:]
        payloads[stripe["entry"]] = altered
        stripe["sha256"] = hashlib.sha256(altered).hexdigest()
        payloads["manifest.json"] = json.dumps(sidecar).encode()
        self.parity.unlink()
        with zipfile.ZipFile(self.parity, "x", compression=zipfile.ZIP_STORED) as archive:
            for name, payload in payloads.items():
                archive.writestr(name, payload)
        self.pin()
        result = self.restore()
        self.assertFalse(result["complete"])
        self.assertEqual(result["reconstructed_from_parity"], 0)
        self.assertEqual(result["unresolved_chunks"][0]["reason"],
                         "parity reconstruction contradicts prior chunk hash")

    def test_alias_and_existing_destination_refused(self) -> None:
        self.capture()
        with self.assertRaises(RecoveryError):
            capture_parity_sidecar(self.original, SELECTED, self.baseline, self.parity)
        self.pin()
        with self.assertRaises(RecoveryError):
            restore_from_parity(self.damaged, SELECTED, self.manifest, self.damaged, self.report)
        self.assertFalse(self.report.exists())


if __name__ == "__main__":
    unittest.main()
