"""The prospectively captured capsule works without damaged HDF5 metadata."""

from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.recovery import RecoveryError, sha256_file
from h5reclaim.recovery_capsule import capture_recovery_capsule, restore_from_capsule


SELECTED = "/science/readings"


class RecoveryCapsuleTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.healthy = self.base / "healthy.h5"
        self.damaged = self.base / "damaged.h5"
        self.capsule = self.base / "capsule.zip"
        self.output = self.base / "output.h5"
        self.report = self.base / "report.json"
        self.values = (np.arange(5 * 8192, dtype="<i4") * 7 + 5).reshape(5, 8192)

    def capture(self, *, filtered: bool = False, sparse: bool = False,
                compound: bool = False) -> dict:
        with h5py.File(self.healthy, "x", libver="latest") as file:
            if compound:
                dtype = np.dtype([("time", "<i8"), ("flux", "<f4")])
                data = np.zeros((5, 8192), dtype=dtype)
                data["time"] = self.values
                data["flux"] = self.values / 8
            else:
                dtype, data = "<i4", self.values
            selected = file.create_dataset(
                SELECTED, shape=data.shape, maxshape=(None, 8192), chunks=(1, 8192),
                dtype=dtype, compression="gzip" if filtered else None,
                shuffle=filtered, fletcher32=filtered,
            )
            selected.attrs["units"] = np.bytes_("counts")
            selected.attrs["revision"] = np.uint32(3)
            if sparse:
                selected[0] = data[0]
                selected[2] = data[2]
                selected[4] = data[4]
            else:
                selected[...] = data
            self.expected = data
        capture = capture_recovery_capsule(self.healthy, SELECTED, self.capsule)
        shutil.copyfile(self.healthy, self.damaged)
        return capture

    def restore(self, capsule_digest: str) -> dict:
        return restore_from_capsule(self.damaged, self.capsule, capsule_digest,
                                    self.output, self.report, dataset_path=SELECTED)

    def corrupt_root(self) -> None:
        with self.damaged.open("r+b") as stream:
            stream.write(b"DESTROYED" + b"\x00" * 247)

    def test_root_destroyed_filtered_chunks_and_exact_schema(self) -> None:
        capture = self.capture(filtered=True)
        self.corrupt_root()
        damaged_sha = sha256_file(self.damaged)
        with self.assertRaises(OSError), h5py.File(self.damaged, "r"):
            pass
        report = self.restore(capture["archive_sha256"])
        self.assertEqual(report, json.loads(self.report.read_text()))
        self.assertTrue(report["complete"])
        self.assertEqual(report["full_chunks"], 5)
        self.assertEqual(sha256_file(self.damaged), damaged_sha)
        with h5py.File(self.healthy, "r") as source, h5py.File(self.output, "r") as result:
            old, new = source[SELECTED], result[SELECTED]
            np.testing.assert_array_equal(new[...], old[...])
            self.assertTrue(new.id.get_type().equal(old.id.get_type()))
            self.assertEqual((new.shape, new.chunks, new.maxshape),
                             (old.shape, old.chunks, old.maxshape))
            original_filters = old.id.get_create_plist().get_nfilters()
            self.assertEqual(new.id.get_create_plist().get_nfilters(), original_filters)
            self.assertEqual(new.attrs["units"], old.attrs["units"])
            self.assertTrue(np.all(result["/_h5reclaim/chunk_status"][...] == 1))

    def test_local_bit_flip_accepts_other_unfiltered_blocks_only(self) -> None:
        capture = self.capture()
        first = capture["records"][0]
        with self.damaged.open("r+b") as stream:
            stream.seek(first["offset"] + 2 * capture["block_bytes"] + 14)
            old = stream.read(1)
            stream.seek(first["offset"] + 2 * capture["block_bytes"] + 14)
            stream.write(bytes([old[0] ^ 0x7f]))
        self.corrupt_root()
        report = self.restore(capture["archive_sha256"])
        self.assertEqual(report["partial_chunks"], 1)
        self.assertEqual(report["full_chunks"], 4)
        self.assertFalse(report["complete"])
        self.assertEqual(report["accepted_elements"], 5 * 8192 - capture["block_bytes"] // 4)
        with h5py.File(self.output, "r") as output:
            validity = output["/_h5reclaim/element_status"][...]
            values = output[SELECTED][...]
            self.assertTrue(np.all(validity[0, 2048:3072] == 0))
            self.assertTrue(np.all(validity[0, :2048] == 1))
            self.assertTrue(np.all(validity[1:] == 1))
            np.testing.assert_array_equal(values[validity == 1], self.expected[validity == 1])

    def test_truncated_source_marks_absent_extent_unknown(self) -> None:
        capture = self.capture(filtered=True)
        last = capture["records"][-1]
        with self.damaged.open("r+b") as stream:
            stream.truncate(last["offset"] + last["length"] // 2)
        self.corrupt_root()
        report = self.restore(capture["archive_sha256"])
        self.assertLess(report["accepted_elements"], 5 * 8192)
        self.assertTrue(any(row["reason"] == "captured extent absent or truncated"
                            for row in report["unresolved_chunks"]))
        with h5py.File(self.output, "r") as output:
            validity = output["/_h5reclaim/element_status"][...]
            np.testing.assert_array_equal(output[SELECTED][...][validity == 1],
                                          self.expected[validity == 1])

    def test_filtered_changed_chunk_is_wholly_unknown(self) -> None:
        capture = self.capture(filtered=True)
        first = capture["records"][0]
        with self.damaged.open("r+b") as stream:
            stream.seek(first["offset"] + 5)
            old = stream.read(1)
            stream.seek(first["offset"] + 5)
            stream.write(bytes([old[0] ^ 0x20]))
        self.corrupt_root()
        report = self.restore(capture["archive_sha256"])
        self.assertEqual((report["full_chunks"], report["partial_chunks"]), (4, 0))
        with h5py.File(self.output, "r") as output:
            self.assertTrue(np.all(output["/_h5reclaim/element_status"][0] == 0))
            self.assertTrue(np.all(output["/_h5reclaim/element_status"][1:] == 1))

    def test_still_readable_schema_change_refuses_prior_capsule(self) -> None:
        capture = self.capture()
        with h5py.File(self.damaged, "r+") as file:
            file[SELECTED].resize((6, 8192))
        with self.assertRaisesRegex(RecoveryError, "schema contradicts"):
            self.restore(capture["archive_sha256"])
        self.assertFalse(self.output.exists())

    def test_complete_removal_of_captured_ranges_keeps_everything_unknown(self) -> None:
        capture = self.capture()
        with self.damaged.open("r+b") as stream:
            stream.truncate(256)
        self.corrupt_root()
        report = self.restore(capture["archive_sha256"])
        self.assertEqual(report["accepted_elements"], 0)
        self.assertEqual(report["unknown_elements"], 5 * 8192)

    def test_sparse_allocation_is_unknown_and_not_counted_as_measurements(self) -> None:
        capture = self.capture(sparse=True)
        self.corrupt_root()
        report = self.restore(capture["archive_sha256"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["full_chunks"], 3)
        self.assertEqual(report["unknown_elements"], 2 * 8192)
        with h5py.File(self.output, "r") as output:
            self.assertTrue(np.all(output["/_h5reclaim/element_status"][1] == 0))
            np.testing.assert_array_equal(output["/_h5reclaim/chunk_status"][...].reshape(-1),
                                          [1, 2, 1, 2, 1])

    def test_fixed_compound_schema_retained(self) -> None:
        capture = self.capture(compound=True)
        self.corrupt_root()
        report = self.restore(capture["archive_sha256"])
        self.assertTrue(report["complete"])
        with h5py.File(self.output, "r") as output:
            np.testing.assert_array_equal(output[SELECTED][...], self.expected)

    def test_tamper_or_wrong_pin_refuses_without_output(self) -> None:
        capture = self.capture()
        with self.capsule.open("ab") as stream:
            stream.write(b"x")
        with self.assertRaisesRegex(RecoveryError, "retained SHA-256"):
            self.restore(capture["archive_sha256"])
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_pinned_but_contradictory_physical_overlap_refuses(self) -> None:
        self.capture()
        with zipfile.ZipFile(self.capsule) as old:
            doc = json.loads(old.read("manifest.json"))
            template = old.read("schema.h5")
        doc["records"][1]["offset"] = doc["records"][0]["offset"]
        forged = self.base / "forged.zip"
        with zipfile.ZipFile(forged, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", json.dumps(doc).encode())
            archive.writestr("schema.h5", template)
        with self.assertRaisesRegex(RecoveryError, "ranges overlap"):
            restore_from_capsule(self.damaged, forged, sha256_file(forged),
                                 self.output, self.report, dataset_path=SELECTED)
        self.assertFalse(self.output.exists())

    def test_wrong_dataset_path_refuses(self) -> None:
        capture = self.capture()
        with self.assertRaisesRegex(RecoveryError, "contradicts capsule path"):
            restore_from_capsule(self.damaged, self.capsule, capture["archive_sha256"],
                                 self.output, self.report, dataset_path="/science/other")

    def test_authentic_gwosc_bytes_with_destroyed_root(self) -> None:
        original = (Path(__file__).resolve().parents[1] / "corpus" / "files" /
                    "H-H1_GWOSC_4KHZ_R1-1126259447-32.hdf5")
        if not original.is_file():
            self.skipTest("the bundled authentic GWOSC file is unavailable")
        from_manifest = json.loads((original.parents[1] / "manifest.json").read_text())
        # A pinned source identity prevents an altered or replaced fixture from
        # being counted as an authentic-data success.
        entries = from_manifest["entries"]
        source_entry = next(entry for entry in entries
                            if entry["path"] == "files/" + original.name)
        self.assertEqual(sha256_file(original), source_entry["sha256"])
        dataset = "/strain/Strain"
        capture = capture_recovery_capsule(original, dataset, self.capsule)
        shutil.copyfile(original, self.damaged)
        self.corrupt_root()
        report = restore_from_capsule(self.damaged, self.capsule,
                                      capture["archive_sha256"], self.output,
                                      self.report, dataset_path=dataset)
        self.assertTrue(report["complete"])
        self.assertEqual(report["full_chunks"], 64)
        with h5py.File(original, "r") as truth, h5py.File(self.output, "r") as restored:
            self.assertEqual(
                hashlib.sha256(truth[dataset][...].tobytes()).digest(),
                hashlib.sha256(restored[dataset][...].tobytes()).digest(),
            )


if __name__ == "__main__":
    unittest.main()
