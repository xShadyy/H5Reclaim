"""Allocation pagination and real sparse captures on supported filesystems."""

import ctypes
import errno
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from h5reclaim.large_streaming import LargeBudget, sparse_snapshot
from h5reclaim import sparse_io
from h5reclaim.sparse_io import prepare_sparse_file, sparse_extents


class WindowsAllocatedRangeTests(unittest.TestCase):
    def test_multiple_pages_and_allocation_boundaries_cover_only_file_bytes(self):
        requests = []

        def query(fd, control, request, output):
            requests.append((request.offset, request.length))
            self.assertEqual(control, sparse_io._QUERY_ALLOCATED_RANGES)
            if request.offset == 0:
                output[0] = sparse_io._AllocatedRange(0, 4096)
                output[1] = sparse_io._AllocatedRange(8192, 4096)
                return False, 234, 2 * ctypes.sizeof(sparse_io._AllocatedRange)
            output[0] = sparse_io._AllocatedRange(16384, 4096)
            return True, 0, ctypes.sizeof(sparse_io._AllocatedRange)

        with patch("h5reclaim.sparse_io._device_io", side_effect=query):
            ranges = list(sparse_io._windows_extents(1, 20000))
        self.assertEqual(ranges, [(0, 4096), (8192, 12288), (16384, 20000)])
        self.assertEqual(requests, [(0, 20000), (12288, 7712)])

    def test_query_does_not_treat_os_errors_as_empty_holes(self):
        for code, expected_errno in ((50, errno.ENOTSUP), (5, errno.EIO)):
            with self.subTest(code=code), patch("h5reclaim.sparse_io._device_io", return_value=(False, code, 0)):
                with self.assertRaises(OSError) as raised:
                    list(sparse_io._windows_extents(1, 4096))
                self.assertEqual(raised.exception.errno, expected_errno)

    def test_malformed_or_nonprogressing_pages_refuse(self):
        for result in ((True, 0, 1), (False, 234, 0), (True, 0, 100000)):
            with self.subTest(result=result), patch("h5reclaim.sparse_io._device_io", return_value=result):
                with self.assertRaises(OSError):
                    list(sparse_io._windows_extents(1, 4096))

    def test_overlapping_allocations_refuse(self):
        def query(fd, control, request, output):
            output[0] = sparse_io._AllocatedRange(0, 2048)
            output[1] = sparse_io._AllocatedRange(1024, 2048)
            return True, 0, 2 * ctypes.sizeof(sparse_io._AllocatedRange)

        with patch("h5reclaim.sparse_io._device_io", side_effect=query):
            with self.assertRaisesRegex(OSError, "invalid allocated ranges"):
                list(sparse_io._windows_extents(1, 4096))


class SparseCaptureTests(unittest.TestCase):
    def test_sparse_capture_preserves_holes_and_hashes_all_logical_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.h5"
            size = 16 * 1024**2 + 17
            with source.open("w+b") as file:
                prepare_sparse_file(file.fileno())
                file.write(b"header")
                file.seek(size - 4)
                file.write(b"tail")
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            with sparse_snapshot(source, budget=LargeBudget(max_copied_bytes=1024**2)) as (image, digest, _, captured, copied):
                self.assertEqual(digest, before)
                self.assertEqual(captured, size)
                self.assertLess(copied, 1024**2)
                self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), before)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    @unittest.skipIf(os.name == "nt", "POSIX buffered-descriptor position regression")
    def test_extent_probes_preserve_buffered_read_position(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(b"a" * 8192 + b"b" * 8192)
            with source.open("rb") as file:
                self.assertEqual(file.read(7), b"a" * 7)
                self.assertTrue(list(sparse_extents(file.fileno(), source.stat().st_size)))
                self.assertEqual(file.read(16377), b"a" * 8185 + b"b" * 8192)


if __name__ == "__main__":
    unittest.main()
