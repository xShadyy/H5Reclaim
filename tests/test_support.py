"""Schema coverage plus refusals outside the structural recovery envelope."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.recovery import recover
from tools.make_broken_link_fixture import make_damage


class SupportEnvelopeTests(unittest.TestCase):
    def test_filtered_edge_float_and_big_endian_datasets_export_exact_values(self) -> None:
        for case in ("filtered", "edge", "float", "big_endian"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.h5"
                shape = (11, 12) if case == "edge" else (12, 12)
                values = np.arange(shape[0] * shape[1], dtype="<u4").reshape(shape)
                if case == "float":
                    values = values.astype("<f4")
                elif case == "big_endian":
                    values = values.astype(">u4")
                with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                    handle.create_dataset(
                        "measurements",
                        data=values,
                        chunks=(2, 2) if case == "edge" else (1, 1),
                        compression="gzip" if case == "filtered" else None,
                    )
                before = source.read_bytes()
                output, report = root / "recovered.h5", root / "report.json"
                result = recover(source, "/measurements", output, report)
                self.assertEqual(source.read_bytes(), before)
                self.assertTrue(result["complete"])
                self.assertEqual(result["counts"]["recovered"],
                                 int(np.prod(result["dataset"]["chunk_grid"])))
                self.assertTrue(report.is_file())
                with h5py.File(output, "r") as handle:
                    exported = handle["/measurements"]
                    self.assertEqual(exported.dtype, values.dtype)
                    np.testing.assert_array_equal(exported[:], values)
                    self.assertTrue(np.all(handle["/_h5reclaim/chunk_status"][:] == 1))

    def test_selected_nested_dataset_with_identical_shape_distractor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pristine, damaged = root / "pristine.h5", root / "damaged.h5"
            shape = (512, 512)
            selected_values = np.arange(shape[0] * shape[1], dtype="<u4").reshape(shape)
            with h5py.File(pristine, "x", libver=("earliest", "v108")) as handle:
                group = handle.require_group("lab/run")
                selected = group.create_dataset(
                    "measurements", data=selected_values, chunks=(16, 16)
                )
                selected.attrs["units"] = "counts"
                handle.create_dataset(
                    "distractor", data=selected_values + 123456, chunks=(16, 16)
                )
            make_damage(pristine, damaged, root / "mutation.json", "/lab/run/measurements")
            output, report = root / "recovered.h5", root / "report.json"
            result = recover(damaged, "/lab/run/measurements", output, report)
            self.assertEqual(result["outcome"], "complete")
            self.assertGreater(result["reconstructed_chunks"], 0)
            self.assertIn("not preserved", result["metadata_note"])
            with h5py.File(output, "r") as handle:
                np.testing.assert_array_equal(handle["/lab/run/measurements"][...], selected_values)
                self.assertNotIn("/distractor", handle)
                self.assertNotIn("units", handle["/lab/run/measurements"].attrs)

    def test_paged_latest_fixed_array_matches_native_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "latest.h5"
            with h5py.File(source, "x", libver="latest") as handle:
                handle.create_dataset(
                    "measurements",
                    data=np.arange(1089, dtype="<u4").reshape(33, 33),
                    chunks=(1, 1),
                )
            before = source.read_bytes()
            output, report = root / "out.h5", root / "out.json"
            result = recover(source, "/measurements", output, report)
            self.assertEqual(source.read_bytes(), before)
            self.assertTrue(result["complete"])
            self.assertEqual(result["counts"]["recovered"], 1089)
            with h5py.File(output, "r") as handle:
                np.testing.assert_array_equal(
                    handle["/measurements"][:],
                    np.arange(1089, dtype="<u4").reshape(33, 33),
                )
                self.assertTrue(np.all(handle["/_h5reclaim/chunk_status"][:] == 1))


if __name__ == "__main__":
    unittest.main()
