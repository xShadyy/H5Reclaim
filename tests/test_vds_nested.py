"""Pinned two-hop VDS routes, provenance, unknowns and contradictions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import h5py
import numpy as np

from h5reclaim.metadata import UnsupportedCase
from h5reclaim.recovery import RecoveryError
from h5reclaim.vds_export import export_vds


def manifest(*pairs: tuple[str, Path]) -> dict:
    return {"schema_version": 1, "files": [
        {"declared_name": name, "path": str(path.absolute()),
         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for name, path in pairs]}


def virtual(path: Path, name: str, size: int, mappings: list[tuple[str, str, slice, slice]]) -> None:
    layout = h5py.VirtualLayout(shape=(size,), dtype="<i4")
    for filename, source_path, destination, selected in mappings:
        source = h5py.VirtualSource(filename, source_path, shape=(size,))
        layout[destination] = source[selected]
    with h5py.File(path, "w") as handle:
        handle.create_virtual_dataset(name, layout, fillvalue=-1979)


class NestedVdsTests(unittest.TestCase):
    def test_two_independent_leaf_files_and_reordered_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, mid, outer = (root / name for name in
                                         ("first.h5", "second.h5", "middle.h5", "outer.h5"))
            with h5py.File(first, "w") as handle:
                handle["/data"] = np.array([10, 11, 12, 13, 14, 15, 16, 17], dtype="<i4")
            with h5py.File(second, "w") as handle:
                handle["/data"] = np.array([20, 21, 22, 23, 24, 25, 26, 27], dtype="<i4")
            virtual(mid, "/nested", 8,
                    [("second.h5", "/data", slice(0, 4), slice(4, 8)),
                     ("first.h5", "/data", slice(4, 8), slice(0, 4))])
            virtual(outer, "/observations", 8,
                    [("middle.h5", "/nested", slice(0, 4), slice(4, 8)),
                     ("middle.h5", "/nested", slice(4, 8), slice(0, 4))])
            result = export_vds(
                outer, "/observations", root / "out.h5", root / "report.json",
                manifest(("middle.h5", mid), ("first.h5", first), ("second.h5", second)))
            self.assertEqual(result["accepted_elements"], 8)
            self.assertEqual({r["declared_name"] for m in result["mappings"]
                              for r in m["nested_mappings"] if r["accepted_requested_elements"]},
                             {"first.h5", "second.h5"})
            with h5py.File(root / "out.h5", "r") as out:
                np.testing.assert_array_equal(out["/observations"][...],
                                              [10, 11, 12, 13, 24, 25, 26, 27])
                self.assertTrue(out["/_h5reclaim/validity"][...].all())

    def test_two_hop_allocated_and_unallocated_chunks_keep_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leaf, mid, outer = (root / name for name in ("leaf.h5", "middle.h5", "outer.h5"))
            with h5py.File(leaf, "w") as handle:
                data = handle.create_dataset("/data", shape=(8,), chunks=(4,), dtype="<i4", fillvalue=-13)
                data[:4] = [91, 92, 93, 94]
            virtual(mid, "/nested", 8, [("leaf.h5", "/data", slice(None), slice(None))])
            virtual(outer, "/observations", 8,
                    [("middle.h5", "/nested", slice(None), slice(None))])
            hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (leaf, mid, outer)]
            result = export_vds(outer, "/observations", root / "out.h5", root / "report.json",
                                manifest(("middle.h5", mid), ("leaf.h5", leaf)))
            self.assertEqual(result["accepted_elements"], 4)
            self.assertEqual(result["unknown_elements"], 4)
            self.assertEqual(result["mappings"][0]["storage_layout"], "virtual")
            nested = result["mappings"][0]["nested_mappings"][0]
            self.assertEqual(nested["accepted_requested_elements"], 4)
            self.assertEqual(nested["source_sha256"], hashes[0])
            self.assertEqual(len(nested["source_chunks"]), 1)
            with h5py.File(root / "out.h5", "r") as out:
                np.testing.assert_array_equal(out["/_h5reclaim/validity"][...], [1] * 4 + [0] * 4)
                np.testing.assert_array_equal(out["/observations"][:4], [91, 92, 93, 94])
                self.assertFalse(np.any(out["/observations"][4:] == -1979))
            self.assertEqual([hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in (leaf, mid, outer)], hashes)

    def test_hash_mismatch_in_second_hop_never_becomes_fill_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leaf, mid, outer = (root / name for name in ("leaf.h5", "middle.h5", "outer.h5"))
            with h5py.File(leaf, "w") as handle:
                handle["/data"] = np.arange(8, dtype="<i4")
            virtual(mid, "/nested", 8, [("leaf.h5", "/data", slice(None), slice(None))])
            virtual(outer, "/observations", 8,
                    [("middle.h5", "/nested", slice(None), slice(None))])
            supplied = manifest(("middle.h5", mid), ("leaf.h5", leaf))
            supplied["files"][1]["sha256"] = "0" * 64
            result = export_vds(outer, "/observations", root / "out.h5", root / "report.json", supplied)
            self.assertEqual(result["accepted_elements"], 0)
            self.assertEqual(result["mappings"][0]["nested_mappings"][0]["status"], "hash_mismatch")
            with h5py.File(root / "out.h5") as out:
                self.assertFalse(out["/_h5reclaim/validity"][...].any())

    def test_nested_overlapping_mappings_refuse_even_if_points_unrequested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leaf, mid, outer = (root / name for name in ("leaf.h5", "middle.h5", "outer.h5"))
            with h5py.File(leaf, "w") as handle:
                handle["/data"] = np.arange(8, dtype="<i4")
            virtual(mid, "/nested", 8,
                    [("leaf.h5", "/data", slice(0, 5), slice(0, 5)),
                     ("leaf.h5", "/data", slice(4, 8), slice(4, 8))])
            virtual(outer, "/observations", 8,
                    [("middle.h5", "/nested", slice(0, 2), slice(0, 2))])
            with self.assertRaisesRegex(UnsupportedCase, "overlapping nested"):
                export_vds(outer, "/observations", root / "out.h5", root / "report.json",
                           manifest(("middle.h5", mid), ("leaf.h5", leaf)))
            self.assertFalse((root / "out.h5").exists())

    def test_third_virtual_layer_refuses_without_reading_through(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leaf, mid, outer, top = (root / name for name in
                                    ("leaf.h5", "middle.h5", "outer.h5", "top.h5"))
            with h5py.File(leaf, "w") as handle:
                handle["/data"] = np.arange(8, dtype="<i4")
            virtual(mid, "/nested", 8, [("leaf.h5", "/data", slice(None), slice(None))])
            virtual(outer, "/observations", 8,
                    [("middle.h5", "/nested", slice(None), slice(None))])
            virtual(top, "/observations", 8,
                    [("outer.h5", "/observations", slice(None), slice(None))])
            with self.assertRaisesRegex(UnsupportedCase, "third VDS layer"):
                export_vds(top, "/observations", root / "out.h5", root / "report.json",
                           manifest(("outer.h5", outer), ("middle.h5", mid), ("leaf.h5", leaf)))

    def test_leaf_datatype_conversion_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leaf, mid, outer = (root / name for name in ("leaf.h5", "middle.h5", "outer.h5"))
            with h5py.File(leaf, "w") as handle:
                handle["/data"] = np.arange(8, dtype="<f4")
            virtual(mid, "/nested", 8, [("leaf.h5", "/data", slice(None), slice(None))])
            virtual(outer, "/observations", 8,
                    [("middle.h5", "/nested", slice(None), slice(None))])
            with self.assertRaisesRegex(UnsupportedCase, "datatype differs"):
                export_vds(outer, "/observations", root / "out.h5", root / "report.json",
                           manifest(("middle.h5", mid), ("leaf.h5", leaf)))
            self.assertFalse((root / "out.h5").exists())

    def test_related_destination_alias_refuses_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leaf, outer = root / "leaf.h5", root / "outer.h5"
            with h5py.File(leaf, "w") as handle:
                handle["/data"] = np.arange(8, dtype="<i4")
            virtual(outer, "/observations", 8,
                    [("leaf.h5", "/data", slice(None), slice(None))])
            with self.assertRaisesRegex(RecoveryError, "destination already exists"):
                export_vds(outer, "/observations", leaf, root / "report.json",
                           manifest(("leaf.h5", leaf)))
            self.assertTrue(leaf.exists())

    def test_public_rescue_command_exports_two_hops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leaf, mid, outer = (root / name for name in ("leaf.h5", "middle.h5", "outer.h5"))
            with h5py.File(leaf, "w") as handle:
                handle["/data"] = np.arange(8, dtype="<i4") * 7
            virtual(mid, "/nested", 8, [("leaf.h5", "/data", slice(None), slice(None))])
            virtual(outer, "/observations", 8,
                    [("middle.h5", "/nested", slice(None), slice(None))])
            related = root / "related.json"
            related.write_text(json.dumps(manifest(("middle.h5", mid), ("leaf.h5", leaf))))
            output, report = root / "out.h5", root / "report.json"
            result = subprocess.run(
                [sys.executable, "-m", "h5reclaim", "rescue", str(outer),
                 "--dataset", "/observations", "--related-files", str(related),
                 "--output", str(output), "--report", str(report)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(report.read_text())["accepted_elements"], 8)
            with h5py.File(output) as published:
                np.testing.assert_array_equal(published["/observations"][...], np.arange(8) * 7)


if __name__ == "__main__":
    unittest.main()
