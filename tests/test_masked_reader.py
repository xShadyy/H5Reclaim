"""Consumer reads must honor the exact status map chosen by each report."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim import MaskedReadError, read_masked


class MaskedReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name) / "recovered.h5"
        self.report = Path(temp.name) / "recovered.report.json"
        self.sha = "a" * 64

    def _write_report(self, dataset: dict, **fields) -> None:
        doc = {"schema_version": 1, "tool": "h5reclaim", "dataset": dataset,
               "source": {"sha256_before": self.sha}, **fields}
        self.report.write_text(json.dumps(doc), encoding="utf-8")

    def test_chunk_grid_selection_expands_only_selected_edges(self) -> None:
        values = np.arange(35, dtype="i4").reshape(5, 7)
        with h5py.File(self.output, "w") as file:
            file.create_dataset("measurements", data=values, chunks=(3, 4))
            group = file.require_group("/_h5reclaim")
            group.attrs["source_sha256"] = self.sha
            group.create_dataset("chunk_status", data=np.array([[1, 4], [0, 1]], dtype="u1"))
        self._write_report({"path": "/measurements", "shape": [5, 7]},
                           validity={"dataset": "/_h5reclaim/chunk_status",
                                     "granularity": "one code per selected dataset chunk",
                                     "codes": {"recovered": 1, "unknown": 0, "unavailable": 4}})

        result = read_masked(self.output, self.report, "/measurements",
                             selection=(slice(2, 5), slice(2, 6)))
        np.testing.assert_array_equal(result.data, values[2:5, 2:6])
        np.testing.assert_array_equal(np.ma.getmaskarray(result),
                                      [[False, False, True, True],
                                       [True, True, False, False],
                                       [True, True, False, False]])
        row = read_masked(self.output, self.report, "/measurements", selection=(3, slice(2, 6)))
        self.assertEqual(row.shape, (4,))
        np.testing.assert_array_equal(np.ma.getmaskarray(row), [True, True, False, False])

    def test_whole_file_uses_nested_element_map_even_when_chunk_map_is_accepted(self) -> None:
        with h5py.File(self.output, "w") as file:
            file.create_dataset("signals", data=np.array([11, 0, 13, 0], dtype="i4"), chunks=(2,))
            group = file.require_group("/_h5reclaim/datasets/d000000")
            group.attrs["source_sha256"] = self.sha
            group.create_dataset("element_status", data=np.array([1, 0, 1, 0], dtype="u1"))
            group.create_dataset("chunk_status", data=np.array([1, 1], dtype="u1"))
        nested = {"schema_version": 1, "tool": "h5reclaim", "dataset": {"path": "/signals", "shape": [4]},
                  "source": {"sha256_before": self.sha},
                  "metadata_group": "/_h5reclaim/datasets/d000000",
                  "validity": {"element_status": "/_h5reclaim/datasets/d000000/element_status",
                               "chunk_status": "/_h5reclaim/datasets/d000000/chunk_status",
                               "element_codes": {"0": "unknown", "1": "verified element"},
                               "codes": {"1": "whole chunk", "4": "unknown"}}}
        whole = {"schema_version": 1, "tool": "h5reclaim", "datasets": [
            {"path": "/signals", "report": nested}]}
        self.report.write_text(json.dumps(whole), encoding="utf-8")
        result = read_masked(self.output, self.report, "/signals", selection=slice(1, 4))
        np.testing.assert_array_equal(result.data, [0, 13, 0])
        np.testing.assert_array_equal(np.ma.getmaskarray(result), [True, False, True])

    def test_missing_ambiguous_or_undeclared_maps_refuse_read(self) -> None:
        with h5py.File(self.output, "w") as file:
            file.create_dataset("signals", data=np.arange(6, dtype="i4"), chunks=(2,))
            group = file.require_group("/_h5reclaim")
            group.attrs["source_sha256"] = self.sha
            group.create_dataset("validity", data=np.array([1, 9, 0], dtype="u1"))
        fields = {"validity_map": "/_h5reclaim/validity",
                  "validity_codes": {"0": "unknown", "1": "read back equal"}}
        self._write_report({"path": "/signals", "shape": [6]}, **fields)
        with self.assertRaisesRegex(MaskedReadError, "code absent"):
            read_masked(self.output, self.report, "/signals", selection=slice(2, 4))
        with self.assertRaisesRegex(MaskedReadError, "does not declare"):
            self._write_report({"path": "/signals", "shape": [6]})
            read_masked(self.output, self.report, "/signals")
        with self.assertRaisesRegex(MaskedReadError, "ambiguous"):
            self._write_report({"path": "/signals", "shape": [6]}, **fields,
                               validity={"dataset": "/_h5reclaim/different",
                                         "granularity": "chunk", "codes": {"recovered": 1}})
            read_masked(self.output, self.report, "/signals")
        with self.assertRaisesRegex(MaskedReadError, "source"):
            self._write_report({"path": "/signals", "shape": [6]}, **fields,
                               source={"sha256_before": "b" * 64})
            read_masked(self.output, self.report, "/signals")

    def test_limits_apply_before_read_and_vlen_is_refused(self) -> None:
        with h5py.File(self.output, "w") as file:
            file.create_dataset("large", shape=(100_000,), chunks=(4,), dtype="i4")
            file.create_dataset("text", data=["hello"], dtype=h5py.string_dtype())
            group = file.require_group("/_h5reclaim")
            group.attrs["source_sha256"] = self.sha
            group.create_dataset("validity", shape=(25_000,), chunks=(100,), dtype="u1")
            group.create_dataset("text_status", data=np.array([1], dtype="u1"))
        self._write_report({"path": "/large", "shape": [100_000]},
                           validity_map="/_h5reclaim/validity",
                           validity_codes={"0": "unknown", "1": "accepted"})
        with self.assertRaisesRegex(MaskedReadError, "element limit"):
            read_masked(self.output, self.report, "/large", max_elements=1024)
        with self.assertRaisesRegex(MaskedReadError, "byte limit"):
            read_masked(self.output, self.report, "/large", selection=slice(0, 16), max_bytes=50)
        value = read_masked(self.output, self.report, "/large", selection=slice(0, 4))
        self.assertTrue(np.ma.getmaskarray(value).all())
        self._write_report({"path": "/text", "shape": [1]},
                           validity_map="/_h5reclaim/text_status",
                           validity_codes={"1": "accepted"})
        with self.assertRaisesRegex(MaskedReadError, "variable-length"):
            read_masked(self.output, self.report, "/text")

    def test_actual_whole_file_report_reads_chunk_and_element_routes(self) -> None:
        from h5reclaim.whole_file import rescue_all

        source = self.output.parent / "source.h5"
        with h5py.File(source, "w") as file:
            file.create_dataset("chunks", data=np.arange(8, dtype="i4"),
                                chunks=(4,), fletcher32=True)
            file.create_dataset("contiguous", data=np.arange(3, dtype="f8"))
        rescue_all(source, self.output, self.report)
        for path, expected in (("/chunks", np.arange(8, dtype="i4")),
                               ("/contiguous", np.arange(3, dtype="f8"))):
            selected = read_masked(self.output, self.report, path)
            np.testing.assert_array_equal(selected.data, expected)
            self.assertFalse(np.ma.getmaskarray(selected).any())


if __name__ == "__main__":
    unittest.main()
