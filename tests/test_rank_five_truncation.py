"""A complete fixed-array index can still locate surviving rank-five chunks."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.chunk_truncation import recover_truncated
from h5reclaim.format import UnsupportedFormat
from h5reclaim.modern_indexes import ModernH5File


class RankFiveTruncationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.source = self.directory / "cut.h5"
        self.output = self.directory / "recovered.h5"
        self.report = self.directory / "report.json"
        self.original = np.arange(64, dtype="<i4").reshape(2, 2, 2, 2, 4)

    def make_source(self, *, maxshape: tuple[int | None, ...] | None = None) -> None:
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset(
                "science", data=self.original, chunks=(1, 1, 1, 1, 4),
                maxshape=maxshape, compression="gzip", fletcher32=True,
            )

    def cut_last_payload(self) -> bytes:
        with h5py.File(self.source) as handle:
            dataset = handle["science"]
            chunks = [dataset.id.get_chunk_info(i) for i in range(dataset.id.get_num_chunks())]
            last = max(chunks, key=lambda chunk: chunk.byte_offset + chunk.size)
            cut_at = last.byte_offset + last.size // 2
        damaged = self.source.read_bytes()[:cut_at]
        self.source.write_bytes(damaged)
        return damaged

    def assert_exact_survivors(self, result: dict, damaged: bytes) -> None:
        self.assertEqual(result["index"]["type"], "fixed_array")
        self.assertEqual(result["counts"]["recovered"], 15)
        self.assertEqual(result["counts"]["unavailable"], 1)
        self.assertEqual(result["validity"]["granularity"], "chunk")
        self.assertEqual(result["validity"]["codes"]["recovered"], 1)
        self.assertEqual(result["source"]["sha256_before"], hashlib.sha256(damaged).hexdigest())
        self.assertEqual(self.source.read_bytes(), damaged)
        with h5py.File(self.output) as handle:
            dataset = handle["science"]
            status = handle[result["validity"]["dataset"]][:]
            self.assertEqual(status.shape, (2, 2, 2, 2, 1))
            self.assertEqual(int(np.count_nonzero(status == 1)), 15)
            self.assertEqual(int(np.count_nonzero(status == 4)), 1)
            self.assertEqual(int(status[1, 1, 1, 1, 0]), 4)
            for chunk_index in np.ndindex(*status.shape):
                if status[chunk_index] != 1:
                    continue
                coordinate = chunk_index[:-1] + (slice(None),)
                np.testing.assert_array_equal(dataset[coordinate], self.original[coordinate])
        self.assertEqual(len(result["mappings"]), 15)
        self.assertEqual(len(result["truncated_payloads"]), 1)
        self.assertEqual(result["truncated_payloads"][0]["coordinate"], [1, 1, 1, 1, 0])

    def test_selected_rank_five_tail_export(self) -> None:
        self.make_source()
        damaged = self.cut_last_payload()
        result = recover_truncated(self.source, "/science", self.output, self.report)
        self.assert_exact_survivors(result, damaged)
        self.assertEqual(json.loads(self.report.read_text(encoding="utf-8")), result)

    def test_automatic_rank_five_tail_export(self) -> None:
        self.make_source()
        damaged = self.cut_last_payload()
        process = subprocess.run(
            [sys.executable, "-m", "h5reclaim", "rescue", str(self.source),
             "--output", str(self.output), "--report", str(self.report)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["outcome"], "partial")
        self.assertEqual(report["datasets_exported"], 1)
        self.assert_exact_survivors(report["datasets"][0]["report"], damaged)

    def test_rank_five_non_fixed_index_remains_outside_raw_parser(self) -> None:
        self.make_source(maxshape=(None, None, 2, 2, 4))
        with h5py.File(self.source) as handle, ModernH5File(self.source) as reader:
            selected = handle["science"]
            with self.assertRaisesRegex(UnsupportedFormat, "rank-five.*fixed-array"):
                reader.read_index(
                    int(h5py.h5o.get_info(selected.id).addr), selected.shape,
                    selected.chunks, selected.dtype.itemsize,
                    maxshape=selected.maxshape, filters=(1, 3),
                )


if __name__ == "__main__":
    unittest.main()
