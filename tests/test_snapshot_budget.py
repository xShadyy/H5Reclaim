"""Resource and change-detection checks for private source images."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from h5reclaim.metadata import UnsupportedCase
from h5reclaim.recovery import RecoveryError, sha256_file, source_snapshot
from h5reclaim.snapshot_io import (
    MIB, SnapshotBudget, SnapshotSourceChanged, copy_and_hash,
)


class SnapshotBudgetTests(unittest.TestCase):
    def test_sparse_source_larger_than_old_limit_keeps_original_and_private_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sparse.h5"
            with source.open("wb") as handle:
                handle.write(b"\x89HDF\r\n\x1a\n")
                handle.truncate(129 * MIB + 3)
            original_hash = sha256_file(source)
            with source_snapshot(source) as (snapshot, digest, identity, size):
                self.assertEqual(size, 129 * MIB + 3)
                self.assertEqual(snapshot.stat().st_size, size)
                self.assertEqual(digest, original_hash)
                with snapshot.open("rb") as image:
                    self.assertEqual(image.read(8), b"\x89HDF\r\n\x1a\n")
                self.assertEqual(identity[2], size)
            self.assertEqual(sha256_file(source), original_hash)

    def test_quota_and_disk_preflight_refuse_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.h5"
            source.write_bytes(b"untouched")
            with self.assertRaisesRegex(UnsupportedCase, "8-byte limit"):
                with source_snapshot(source, max_source_bytes=8):
                    pass
            with patch("h5reclaim.snapshot_io.shutil.disk_usage", return_value=SimpleNamespace(free=5)):
                with self.assertRaisesRegex(UnsupportedCase, "free space"):
                    with source_snapshot(source):
                        pass
            self.assertEqual(source.read_bytes(), b"untouched")

    def test_cooperative_time_quota_and_source_growth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.h5"
            source.write_bytes(b"test")
            with patch("h5reclaim.snapshot_io.time.monotonic", side_effect=[0.0, 1.0]):
                with self.assertRaisesRegex(UnsupportedCase, "elapsed-time"):
                    with source_snapshot(source, max_seconds=0.5):
                        pass
            self.assertEqual(source.read_bytes(), b"test")

            with self.assertRaises(SnapshotSourceChanged):
                copy_and_hash(
                    io.BytesIO(b"growth"), io.BytesIO(), expected_size=4,
                    target_parent=Path(directory),
                    budget=SnapshotBudget(max_source_bytes=100, disk_reserve_bytes=0),
                )

    def test_concurrent_source_edit_rejected_before_image_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.h5"
            source.write_bytes(b"a" * 100)
            from h5reclaim import recovery
            real_copy = recovery.copy_and_hash

            def mutate(*args: object, **kwargs: object) -> tuple[str, int]:
                before = source.stat()
                with source.open("r+b") as handle:
                    handle.write(b"b")
                # Rapid same-size writes can share a Windows timestamp tick.
                # This case exercises the metadata-change check explicitly.
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
                return real_copy(*args, **kwargs)

            with patch("h5reclaim.recovery.copy_and_hash", side_effect=mutate):
                with self.assertRaisesRegex(RecoveryError, "changed"):
                    with source_snapshot(source):
                        pass

    def test_publication_hash_rejects_byte_changes_even_when_metadata_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.h5"
            source.write_bytes(b"input A")
            from h5reclaim import recovery
            with source_snapshot(source) as (_snapshot, digest, identity, _size):
                pass
            source.write_bytes(b"input B")
            with patch("h5reclaim.recovery._identity", return_value=identity):
                with self.assertRaisesRegex(RecoveryError, "input bytes changed"):
                    recovery._verify_source(source, identity, digest)


if __name__ == "__main__":
    unittest.main()
