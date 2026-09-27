"""Independent VDS value, coordinate, and conservative refusal checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import h5py
import numpy as np

from h5reclaim.metadata import UnsupportedCase
from h5reclaim.vds_export import _selected_coordinates, export_vds


def _manifest(*files: tuple[str, Path]) -> dict:
    return {"schema_version": 1, "files": [
        {"declared_name": name, "path": str(path.absolute()),
         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for name, path in files]}


def _vds(path: Path, maps: list[tuple[str, str, tuple, tuple]], shape: tuple[int, ...],
         dtype="i4", fill=-999) -> None:
    layout = h5py.VirtualLayout(shape=shape, dtype=dtype)
    for item in maps:
        filename, source_path, target_sel, source_sel = item[:4]
        source_shape = item[4] if len(item) == 5 else shape
        src = h5py.VirtualSource(filename, source_path, shape=source_shape)
        layout[target_sel] = src[source_sel]
    with h5py.File(path, "w") as handle:
        handle.create_virtual_dataset("/observations", layout, fillvalue=fill)


class VDSExportTests(unittest.TestCase):
    def test_multiaxis_blocks_use_c_coordinate_order(self) -> None:
        space = h5py.h5s.create_simple((4, 6))
        space.select_hyperslab(start=(0, 0), stride=(2, 3), count=(2, 2), block=(2, 2))
        coordinates = _selected_coordinates(space, (4, 6))
        self.assertEqual(coordinates[:8], ((0, 0), (0, 1), (0, 3), (0, 4),
                                           (1, 0), (1, 1), (1, 3), (1, 4)))
        self.assertEqual(len(coordinates), 16)

    def test_two_pinned_sources_exact_values_and_bitmaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, vds = (root / name for name in ("first.h5", "second.h5", "virtual.h5"))
            for path, start in ((first, 10), (second, 100)):
                with h5py.File(path, "w") as handle:
                    handle.create_dataset("/data", data=np.arange(start, start + 8, dtype="i4").reshape(2, 4),
                                          chunks=(1, 4))
            _vds(vds, [("first.h5", "/data", (slice(0, 2), slice(None)),
                         (slice(None), slice(None)), (2, 4)),
                       ("second.h5", "/data", (slice(2, 4), slice(None)),
                         (slice(None), slice(None)), (2, 4))], (4, 4))
            source_hashes = {p: hashlib.sha256(p.read_bytes()).digest() for p in (first, second, vds)}
            output, report = root / "out.h5", root / "report.json"
            result = export_vds(vds, "/observations", output, report,
                                _manifest(("first.h5", first), ("second.h5", second)))
            self.assertEqual(result["accepted_elements"], 16)
            self.assertEqual(result["unknown_elements"], 0)
            self.assertEqual(result["outcome"], "complete")
            self.assertEqual([m["status"] for m in result["mappings"]], ["accepted", "accepted"])
            with h5py.File(output, "r") as handle:
                expected = np.vstack((np.arange(10, 18).reshape(2, 4),
                                      np.arange(100, 108).reshape(2, 4)))
                np.testing.assert_array_equal(handle["/observations"][...], expected)
                np.testing.assert_array_equal(handle["/_h5reclaim/validity"][...], np.ones((4, 4), "u1"))
                self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()])["accepted_elements"], 16)
            self.assertEqual(json.loads(report.read_text()), result)
            self.assertTrue(all(hashlib.sha256(p.read_bytes()).digest() == digest
                                for p, digest in source_hashes.items()))

    def test_missing_source_and_unmapped_values_are_unknown_not_fill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, vds = (root / name for name in ("a.h5", "b.h5", "virtual.h5"))
            with h5py.File(first, "w") as handle:
                handle["/data"] = np.arange(4, dtype="i4")
            _vds(vds, [("a.h5", "/data", (slice(0, 2),), (slice(0, 2),)),
                       ("b.h5", "/data", (slice(2, 4),), (slice(0, 2),))], (4,))
            output, report = root / "out.h5", root / "report.json"
            result = export_vds(vds, "/observations", output, report, _manifest(("a.h5", first)))
            self.assertEqual(result["accepted_elements"], 2)
            self.assertEqual(result["outcome"], "partial")
            self.assertEqual(result["mappings"][1]["status"], "not_supplied")
            with h5py.File(output) as handle:
                np.testing.assert_array_equal(handle["/_h5reclaim/validity"][...], [1, 1, 0, 0])
                np.testing.assert_array_equal(handle["/observations"][:2], [0, 1])
                self.assertTrue(np.all(handle["/observations"][2:] != -999))

    def test_multiaxis_strided_hyperslab_pairs_in_c_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, vds = root / "source.h5", root / "virtual.h5"
            data = np.arange(6 * 8, dtype="i4").reshape(6, 8)
            with h5py.File(source, "w") as handle:
                handle["/data"] = data
            layout = h5py.VirtualLayout(shape=(6, 8), dtype="i4")
            src = h5py.VirtualSource("source.h5", "/data", shape=data.shape)
            layout[::2, 1::2] = src[1::2, ::2]
            with h5py.File(vds, "w") as handle:
                handle.create_virtual_dataset("/observations", layout, fillvalue=-999)
            result = export_vds(vds, "/observations", root / "out.h5", root / "report.json",
                                _manifest(("source.h5", source)))
            self.assertEqual(result["accepted_elements"], 12)
            self.assertEqual(result["unknown_elements"], 36)
            with h5py.File(root / "out.h5") as handle:
                np.testing.assert_array_equal(handle["/observations"][::2, 1::2], data[1::2, ::2])
                validity = np.zeros((6, 8), dtype="u1")
                validity[::2, 1::2] = 1
                np.testing.assert_array_equal(handle["/_h5reclaim/validity"][...], validity)

    def test_unallocated_source_chunk_and_incorrect_hash_are_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, vds = root / "source.h5", root / "virtual.h5"
            with h5py.File(source, "w") as handle:
                handle.create_dataset("/data", shape=(8,), chunks=(4,), dtype="i4", fillvalue=-7)[:4] = [1, 2, 3, 4]
            _vds(vds, [("source.h5", "/data", (slice(None),), (slice(None),))], (8,))
            output, report = root / "out.h5", root / "report.json"
            result = export_vds(vds, "/observations", output, report, _manifest(("source.h5", source)))
            self.assertEqual(result["accepted_elements"], 4)
            with h5py.File(output) as handle:
                np.testing.assert_array_equal(handle["/_h5reclaim/validity"][...], [1] * 4 + [0] * 4)
            output.unlink()
            report.unlink()
            bad = _manifest(("source.h5", source))
            bad["files"][0]["sha256"] = "0" * 64
            result = export_vds(vds, "/observations", output, report, bad)
            self.assertEqual(result["accepted_elements"], 0)
            self.assertEqual(result["mappings"][0]["status"], "hash_mismatch")

    def test_refuses_overlap_and_transitive_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, vds = root / "source.h5", root / "virtual.h5"
            with h5py.File(source, "w") as handle:
                handle["/data"] = np.arange(4, dtype="i4")
            _vds(vds, [("source.h5", "/data", (slice(0, 3),), (slice(0, 3),)),
                       ("source.h5", "/data", (slice(2, 4),), (slice(0, 2),))], (4,))
            with self.assertRaisesRegex(UnsupportedCase, "overlapping"):
                export_vds(vds, "/observations", root / "out.h5", root / "report.json",
                           _manifest(("source.h5", source)))
            self.assertFalse((root / "out.h5").exists())
            # A source which itself depends on another file cannot be silently
            # converted into a current native fill value.
            nested = root / "nested.h5"
            _vds(nested, [("source.h5", "/data", (slice(None),), (slice(None),))], (4,))
            _vds(vds, [("nested.h5", "/observations", (slice(None),), (slice(None),))], (4,))
            with self.assertRaisesRegex(UnsupportedCase, "transitive"):
                export_vds(vds, "/observations", root / "out.h5", root / "report.json",
                           _manifest(("nested.h5", nested)))


if __name__ == "__main__":
    unittest.main()
