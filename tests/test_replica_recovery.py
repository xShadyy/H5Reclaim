"""Independent copies require historical hashes, rooted indexes, and no conflicts."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.replica_recovery import RecoveryError, restore_from_replicas
from h5reclaim.baseline import capture_baseline


SELECTED = "/science/signal"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def fixture(path: Path, *, dtype: str = "<u4", compression: bool = False) -> None:
    with h5py.File(path, "x", libver="latest") as file:
        source = file.create_dataset(
            SELECTED, shape=(6, 8), chunks=(3, 2), dtype=dtype,
            compression="gzip" if compression else None,
            fletcher32=compression,
        )
        values = np.arange(48, dtype=dtype).reshape(6, 8)
        source[...] = values * 37 + 3


def mutate_chunk(path: Path, coordinate: tuple[int, int]) -> None:
    with h5py.File(path, "r") as source:
        storage = source[SELECTED].id.get_chunk_info_by_coord(coordinate)
        offset = storage.byte_offset
    with path.open("r+b") as stream:
        stream.seek(offset)
        original = stream.read(1)
        stream.seek(offset)
        stream.write(bytes([original[0] ^ 0x55]))


class ReplicaRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.original = self.root / "original.h5"
        self.source = self.root / "damaged.h5"
        self.replica = self.root / "replica.h5"
        self.baseline = self.root / "baseline.json"
        self.manifest = self.root / "manifest.json"
        self.output = self.root / "restored.h5"
        self.report = self.root / "report.json"

    def make_manifest(self, *, compression: bool = False, replicas: list[Path] | None = None) -> dict:
        fixture(self.original, compression=compression)
        capture_baseline(self.original, SELECTED, self.baseline)
        shutil.copyfile(self.original, self.source)
        shutil.copyfile(self.original, self.replica)
        self.replicas = replicas or [self.replica]
        return self.save_manifest()

    def save_manifest(self) -> dict:
        document = {
            "schema_version": 1,
            "damaged_sha256": digest(self.source),
            "baseline": {"path": str(self.baseline), "sha256": digest(self.baseline)},
            "replicas": [{"path": str(path), "sha256": digest(path)} for path in self.replicas],
        }
        write_json(self.manifest, document)
        return document

    def restore(self) -> dict:
        return restore_from_replicas(self.source, SELECTED, self.manifest, self.output, self.report)

    def test_unfiltered_bit_change_restored_from_independent_copy(self) -> None:
        self.make_manifest()
        mutate_chunk(self.source, (3, 4))
        self.save_manifest()
        before = digest(self.source)
        result = self.restore()
        self.assertEqual(digest(self.source), before)
        self.assertEqual(result, json.loads(self.report.read_text()))
        self.assertTrue(result["complete"])
        self.assertEqual(result["replacements_from_replicas"], 1)
        restored = next(item for item in result["mappings"] if item["coordinate"] == [3, 4])
        self.assertEqual(restored["source_id"], "replica:0")
        self.assertGreaterEqual(len(restored["evidence"]["index_link_ids"]), 2)
        self.assertEqual(restored["evidence"]["extent"]["offset"], restored["source_absolute_offset"])
        with h5py.File(self.original, "r") as truth, h5py.File(self.output, "r") as output:
            np.testing.assert_array_equal(output[SELECTED][...], truth[SELECTED][...])
            self.assertTrue(np.all(output["/_h5reclaim/chunk_status"][...] == 1))

    def test_corrupt_filtered_chunk_is_supplied_by_independent_copy(self) -> None:
        self.make_manifest(compression=True)
        mutate_chunk(self.source, (3, 4))
        self.save_manifest()
        result = self.restore()
        self.assertEqual(result["replacements_from_replicas"], 1)
        with h5py.File(self.original, "r") as truth, h5py.File(self.output, "r") as output:
            np.testing.assert_array_equal(output[SELECTED][...], truth[SELECTED][...])

    def test_conflicting_replicas_leave_coordinate_ambiguous(self) -> None:
        self.make_manifest()
        second = self.root / "second.h5"
        shutil.copyfile(self.replica, second)
        mutate_chunk(self.source, (3, 4))
        mutate_chunk(second, (3, 4))
        self.replicas.append(second)
        self.save_manifest()
        result = self.restore()
        self.assertEqual(result["replacements_from_replicas"], 0)
        self.assertEqual(result["counts"]["ambiguous"], 1)
        self.assertFalse(result["complete"])
        with h5py.File(self.output, "r") as output:
            self.assertEqual(int(output["/_h5reclaim/chunk_status"][1, 2]), 3)
        self.assertEqual(result["unresolved_chunks"][0]["reason"], "independent replicas disagree")

    def test_sparse_hdf5_replica_supplies_only_its_anchored_chunk(self) -> None:
        self.make_manifest()
        with h5py.File(self.original, "r") as original:
            region = original[SELECTED][3:6, 4:6]
        self.replica.unlink()
        with h5py.File(self.replica, "x", libver="latest") as file:
            data = file.create_dataset(SELECTED, shape=(6, 8), chunks=(3, 2), dtype="<u4")
            data[3:6, 4:6] = region
        mutate_chunk(self.source, (3, 4))
        self.save_manifest()
        result = self.restore()
        self.assertTrue(result["complete"])
        self.assertEqual(result["replacements_from_replicas"], 1)
        with h5py.File(self.original, "r") as truth, h5py.File(self.output, "r") as output:
            np.testing.assert_array_equal(output[SELECTED][...], truth[SELECTED][...])

    def test_unrelated_same_shape_replica_does_not_supply_value(self) -> None:
        self.make_manifest()
        mutate_chunk(self.source, (0, 0))
        mutate_chunk(self.replica, (0, 0))
        self.save_manifest()
        result = self.restore()
        self.assertEqual(result["counts"]["unavailable"], 1)
        self.assertEqual(result["replacements_from_replicas"], 0)
        with h5py.File(self.output, "r") as output:
            self.assertEqual(int(output["/_h5reclaim/chunk_status"][0, 0]), 4)

    def test_false_identification_and_manifest_tampering_refused(self) -> None:
        self.make_manifest()
        mutate_chunk(self.replica, (0, 0))
        self.save_manifest()
        mutate_chunk(self.replica, (0, 2))
        with self.assertRaisesRegex(RecoveryError, "replica 0 differs"):
            self.restore()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_copy_with_different_schema_refused(self) -> None:
        self.make_manifest()
        self.replica.unlink()
        fixture(self.replica, dtype="<f8")
        self.save_manifest()
        with self.assertRaisesRegex(RecoveryError, "schema contradicts"):
            self.restore()
        self.assertFalse(self.output.exists())

    def test_hard_linked_file_does_not_count_as_distinct_replica(self) -> None:
        self.make_manifest()
        second = self.root / "same_inode.h5"
        second.hardlink_to(self.replica)
        self.replicas.append(second)
        self.save_manifest()
        with self.assertRaisesRegex(RecoveryError, "must be distinct files"):
            self.restore()

    def test_false_coordinate_and_baseline_substitution_refused(self) -> None:
        self.make_manifest()
        baseline = json.loads(self.baseline.read_text())
        baseline["chunk_hashes"][0]["coordinate"] = [0, 1]
        write_json(self.baseline, baseline)
        self.save_manifest()
        with self.assertRaisesRegex(RecoveryError, "invalid chunk coordinate"):
            self.restore()
        self.assertFalse(self.output.exists())

        baseline["chunk_hashes"][0]["coordinate"] = [0, 0]
        write_json(self.baseline, baseline)
        # Existing manifest is now bound to the former baseline digest.
        with self.assertRaisesRegex(RecoveryError, "baseline differs"):
            self.restore()

    def test_no_prior_checksum_cannot_vote_a_value_into_output(self) -> None:
        self.make_manifest()
        baseline = json.loads(self.baseline.read_text())
        baseline["chunk_hashes"] = baseline["chunk_hashes"][1:]
        write_json(self.baseline, baseline)
        self.save_manifest()
        result = self.restore()
        self.assertEqual(result["counts"]["allocation_unknown"], 1)
        self.assertFalse(result["complete"])
        with h5py.File(self.output, "r") as output:
            self.assertEqual(int(output["/_h5reclaim/chunk_status"][0, 0]), 2)


if __name__ == "__main__":
    unittest.main()
