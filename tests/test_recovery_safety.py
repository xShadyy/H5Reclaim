"""Adversarial checks for evidence attribution and source preservation."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.format import KEY_SIZE, FormatError, H5File
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.recovery import RecoveryError, STATUS_CODES, analyze, recover, source_snapshot
from h5reclaim import recovery as recovery_module


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.make_broken_link_fixture import make_damage  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OutputPathSafetyTests(unittest.TestCase):
    def test_all_destinations_that_alias_source_are_refused(self) -> None:
        for target_kind in ("same", "symlink", "hardlink"):
            for destination in ("output", "report"):
                with self.subTest(target_kind=target_kind, destination=destination):
                    with tempfile.TemporaryDirectory() as directory:
                        base = Path(directory)
                        source = base / "evidence.h5"
                        source.write_bytes(b"original evidence")
                        prior_hash = digest(source)
                        alias = base / "alias"
                        if target_kind == "same":
                            alias = source
                        elif target_kind == "symlink":
                            try:
                                alias.symlink_to(source)
                            except OSError as exc:
                                if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                                    continue  # Windows account cannot create symbolic links.
                                raise
                        else:
                            os.link(source, alias)
                        output = alias if destination == "output" else base / "result.h5"
                        report = alias if destination == "report" else base / "report.json"

                        with self.assertRaises(RecoveryError):
                            recover(source, "/measurements", output, report)

                        self.assertEqual(digest(source), prior_hash)
                        self.assertFalse((base / "result.h5").exists())
                        self.assertFalse((base / "report.json").exists())
                        self.assertFalse(list(base.glob(".h5reclaim-*")))

    def test_output_and_report_alias_through_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "evidence.h5"
            source.write_bytes(b"original evidence")
            prior_hash = digest(source)
            folder = base / "destinations"
            folder.mkdir()
            try:
                (base / "other_name").symlink_to(folder, target_is_directory=True)
            except OSError as exc:
                if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows account cannot create symbolic links")
                raise
            output = folder / "result"
            report = base / "other_name" / "result"

            with self.assertRaisesRegex(RecoveryError, "must differ"):
                recover(source, "/measurements", output, report)

            self.assertEqual(digest(source), prior_hash)
            self.assertFalse(output.exists())


class BrokenLinkSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)
        cls.pristine = cls.base / "pristine.h5"
        cls.damaged = cls.base / "damaged.h5"
        manifest_path = cls.base / "truth" / "manifest.json"
        shape = (512, 512)
        with h5py.File(cls.pristine, "x", libver=("earliest", "v108")) as handle:
            dataset = handle.create_dataset("measurements", shape=shape, dtype="<u4", chunks=(16, 16))
            dataset[...] = np.arange(shape[0] * shape[1], dtype="<u4").reshape(shape)
        cls.manifest = make_damage(cls.pristine, cls.damaged, manifest_path, "/measurements")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def _altered_file(self, directory: Path, image: bytearray) -> Path:
        path = directory / "adversarial.h5"
        path.write_bytes(image)
        return path

    def test_publication_failures_leave_no_result_or_temporary_files(self) -> None:
        before = digest(self.damaged)
        for failure in ("second_directory", "report_link"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                output, report = folder / "result.h5", folder / "report.json"
                if failure == "second_directory":
                    real_temporary_directory = tempfile.TemporaryDirectory
                    attempts = 0

                    def make_directory(*args: object, **kwargs: object) -> tempfile.TemporaryDirectory:
                        nonlocal attempts
                        attempts += 1
                        if attempts == 2:
                            raise OSError("injected second temporary directory failure")
                        return real_temporary_directory(*args, **kwargs)

                    with patch("h5reclaim.recovery.tempfile.TemporaryDirectory", side_effect=make_directory):
                        with self.assertRaisesRegex(OSError, "injected second"):
                            recover(self.damaged, "/measurements", output, report)
                else:
                    real_link = os.link
                    attempts = 0

                    def publish_link(source: Path, destination: Path) -> None:
                        nonlocal attempts
                        attempts += 1
                        if attempts == 2:
                            raise OSError("injected report publication failure")
                        real_link(source, destination)

                    with patch("h5reclaim.recovery.os.link", side_effect=publish_link):
                        with self.assertRaisesRegex(OSError, "injected report"):
                            recover(self.damaged, "/measurements", output, report)

                self.assertFalse(output.exists())
                self.assertFalse(report.exists())
                self.assertFalse(list(folder.glob(".h5reclaim-*")))
                self.assertEqual(digest(self.damaged), before)

    def _assert_unresolved_gap(self, path: Path, directory: Path) -> None:
        before = digest(path)
        analysis = analyze(path, "/measurements")
        expected_missing = {tuple(value) for value in self.manifest["affected_chunk_offsets"]}
        recovered_coords = {record.coordinate for record in analysis.records}
        self.assertTrue(expected_missing.isdisjoint(recovered_coords))
        self.assertEqual(analysis.report["reconstructed_chunks"], 0)
        self.assertEqual(analysis.report["counts"]["allocation_unknown"], len(expected_missing))
        self.assertFalse(analysis.report["complete"])
        self.assertEqual(len(analysis.report["unresolved_links"]), 1)

        output, report = directory / "partial.h5", directory / "report.json"
        result = recover(path, "/measurements", output, report)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(digest(path), before)
        self.assertEqual(json.loads(report.read_text())["source"]["sha256_after"], before)
        with h5py.File(output, "r") as handle:
            status = handle["/_h5reclaim/chunk_status"][...]
            self.assertEqual(handle["/measurements"].attrs["h5reclaim_complete"], False)
            for row, col in expected_missing:
                self.assertEqual(int(status[row // 16, col // 16]), STATUS_CODES["allocation_unknown"])

    def test_absent_reciprocal_sibling_refuses_detached_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            image = bytearray(self.damaged.read_bytes())
            with H5File(self.damaged) as reader:
                left = reader.absolute(self.manifest["left_leaf_address"])
                right_sibling_field = left + 8 + reader.superblock.offset_size
                image[right_sibling_field : right_sibling_field + reader.superblock.offset_size] = (
                    b"\xff" * reader.superblock.offset_size
                )
            self._assert_unresolved_gap(self._altered_file(folder, image), folder)

    def test_candidate_with_wrong_parent_boundary_is_not_assigned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            image = bytearray(self.damaged.read_bytes())
            with H5File(self.damaged) as reader:
                candidate = reader.read_tree(self.manifest["affected_leaf_address"])
                self.assertGreater(len(candidate.entries), 1)
                first = candidate.entries[0].key.offsets
                second = candidate.entries[1].key.offsets
                changed = (first[0], first[1] + 1, first[2])
                self.assertLess(changed, second)
                column_field = candidate.entries[0].pointer_offset - KEY_SIZE + 16
                struct.pack_into("<Q", image, column_field, changed[1])
            source = self._altered_file(folder, image)
            before = digest(source)
            with H5File(source) as reader:
                self.assertEqual(reader.find_missing_child_candidates(self.manifest["btree_root_address"]), ())
            # A contradicted detached candidate cannot be assigned; intact
            # neighboring chunks may still be exported with the gap unknown.
            self._assert_unresolved_gap(source, folder)
            self.assertEqual(digest(source), before)

    def test_reachable_child_key_conflict_is_rejected_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            image = bytearray(self.damaged.read_bytes())
            with H5File(self.damaged) as reader:
                root = reader.read_tree(self.manifest["btree_root_address"])
                first = root.entries[0]
                struct.pack_into("<Q", image, first.pointer_offset - KEY_SIZE + 16, first.key.offsets[1] + 1)
            source = self._altered_file(folder, image)
            before = digest(source)
            output, report = folder / "result.h5", folder / "report.json"

            with H5File(source) as reader:
                with self.assertRaisesRegex(FormatError, "lower bound mismatch"):
                    reader.walk_tree(self.manifest["btree_root_address"])
            with self.assertRaises((FormatError, UnsupportedCase, RecoveryError)):
                recover(source, "/measurements", output, report)

            self.assertEqual(digest(source), before)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_chunk_pointer_crossing_end_of_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            image = bytearray(self.damaged.read_bytes())
            with H5File(self.damaged) as reader:
                root = reader.read_tree(self.manifest["btree_root_address"])
                leaf = reader.read_tree(root.entries[0].address)
                pointer_field = leaf.entries[0].pointer_offset
                chunk_size = leaf.entries[0].key.stored_size
                target = reader.superblock.eof_address - reader.superblock.base_address - chunk_size + 1
                image[pointer_field : pointer_field + reader.superblock.offset_size] = target.to_bytes(
                    reader.superblock.offset_size, "little"
                )
            source = self._altered_file(folder, image)
            before = digest(source)
            output, report = folder / "result.h5", folder / "report.json"

            with self.assertRaisesRegex(FormatError, "crosses HDF5 end-of-file"):
                recover(source, "/measurements", output, report)

            self.assertEqual(digest(source), before)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_chunk_pointer_into_index_metadata_is_not_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            image = bytearray(self.damaged.read_bytes())
            with H5File(self.damaged) as reader:
                root = reader.read_tree(self.manifest["btree_root_address"])
                leaf = reader.read_tree(root.entries[0].address)
                pointer = leaf.entries[0].pointer_offset
                image[pointer : pointer + reader.superblock.offset_size] = root.address.to_bytes(
                    reader.superblock.offset_size, "little"
                )
            source = self._altered_file(folder, image)
            before = digest(source)
            output, report = folder / "result.h5", folder / "report.json"
            with self.assertRaisesRegex(FormatError, "overlaps .* B-tree node"):
                recover(source, "/measurements", output, report)
            self.assertEqual(digest(source), before)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_replaced_input_path_is_refused_even_with_identical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "source.h5"
            source.write_bytes(self.damaged.read_bytes())
            replacement = folder / "replacement.h5"
            replacement.write_bytes(source.read_bytes())
            real_analysis = recovery_module.analyze

            def replace_after_analysis(*args: object) -> object:
                result = real_analysis(*args)
                # The original read handle has closed, so Windows can replace
                # the pathname while the publication check is still pending.
                os.replace(replacement, source)
                return result

            output, report = folder / "result.h5", folder / "report.json"
            with patch("h5reclaim.recovery.analyze", side_effect=replace_after_analysis):
                with self.assertRaisesRegex(RecoveryError, "identity"):
                    recover(source, "/measurements", output, report)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())


class SnapshotPortabilityTests(unittest.TestCase):
    def test_windows_style_stat_and_fstat_mismatch_does_not_reject_unchanged_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.h5"
            source.write_bytes(b"unchanged scientific input")
            real_fstat = os.fstat
            source_info = source.stat()

            def windows_like_fstat(fd: int) -> os.stat_result | SimpleNamespace:
                actual = real_fstat(fd)
                if (actual.st_dev, actual.st_ino) != (source_info.st_dev, source_info.st_ino):
                    return actual
                return SimpleNamespace(
                    st_mode=actual.st_mode,
                    st_dev=actual.st_dev + 7,
                    st_ino=actual.st_ino + 7,
                    st_size=actual.st_size,
                    st_mtime_ns=actual.st_mtime_ns + 100,
                    st_ctime_ns=actual.st_ctime_ns + 100,
                )

            with patch.object(recovery_module, "_WINDOWS_STAT", True):
                with patch("h5reclaim.recovery.os.fstat", side_effect=windows_like_fstat):
                    with source_snapshot(source) as (snapshot, expected_hash, identity, size):
                        self.assertEqual(snapshot.read_bytes(), source.read_bytes())
                        self.assertEqual(size, source.stat().st_size)
                        self.assertEqual(identity, recovery_module._identity(source.stat()))
                    recovery_module._verify_source(source, identity, expected_hash)

    def test_same_size_mutation_and_identical_byte_path_swap_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.h5"
            source.write_bytes(b"input A")
            replacement = Path(directory) / "replacement.h5"
            replacement.write_bytes(source.read_bytes())
            with source_snapshot(source) as (_snapshot, expected_hash, identity, _size):
                pass
            source.write_bytes(b"input B")
            with self.assertRaises(RecoveryError):
                recovery_module._verify_source(source, identity, expected_hash)

            # Re-establish the baseline so the second check isolates a path
            # replacement whose contents have the same hash.
            source.write_bytes(b"input A")
            with source_snapshot(source) as (_snapshot, expected_hash, identity, _size):
                pass
            os.replace(replacement, source)
            with self.assertRaisesRegex(RecoveryError, "identity"):
                recovery_module._verify_source(source, identity, expected_hash)

    def test_path_replacement_between_stat_and_open_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.h5"
            source.write_bytes(b"first input")
            replacement = Path(directory) / "replacement.h5"
            replacement.write_bytes(b"second input")
            real_open = Path.open
            swapped = False

            def replace_before_open(path: Path, *args: object, **kwargs: object):
                nonlocal swapped
                if path == source and not swapped:
                    os.replace(replacement, source)
                    swapped = True
                return real_open(path, *args, **kwargs)

            with patch.object(Path, "open", replace_before_open):
                with self.assertRaisesRegex(RecoveryError, "while it was being opened"):
                    with source_snapshot(source):
                        pass


if __name__ == "__main__":
    unittest.main()
