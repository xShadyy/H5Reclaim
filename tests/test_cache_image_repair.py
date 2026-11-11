"""Cache-image plans are checked against real HDF5-generated cache blocks."""

import contextlib
import ctypes
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.__main__ import main
from h5reclaim.cache_image import inspect_cache_image
from h5reclaim.format import UnsupportedFormat
from h5reclaim.large_streaming import LargeBudget
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.native_bindings import public_function
from h5reclaim.preflight import preflight
from h5reclaim.repair_plan import apply_repair, plan_repair


class CacheConfig(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_int),
        ("generate_image", ctypes.c_bool),
        ("save_resize_status", ctypes.c_bool),
        ("entry_ageout", ctypes.c_int),
    ]


class CacheImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.plan, self.output, self.audit = (
            self.root / name for name in ("source.h5", "plan.json", "out.h5", "audit.jsonl")
        )
        self.values = np.arange(120, dtype="i4").reshape(12, 10)

    def fixture(self, *, flushed=False, userblock=0):
        try:
            configure = public_function(
                "H5Pset_mdc_image_config", [ctypes.c_longlong, ctypes.POINTER(CacheConfig)]
            )
        except UnsupportedCase as exc:
            self.skipTest(str(exc))
        config = CacheConfig(1, True, False, -1)
        access = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
        access.set_libver_bounds(h5py.h5f.LIBVER_LATEST, h5py.h5f.LIBVER_LATEST)
        self.assertEqual(configure(access.id, ctypes.byref(config)), 0)
        creation = h5py.h5p.create(h5py.h5p.FILE_CREATE)
        if userblock:
            creation.set_userblock(userblock)
        with h5py.File(
            h5py.h5f.create(bytes(self.source), h5py.h5f.ACC_TRUNC, fcpl=creation, fapl=access)
        ) as handle:
            for index in range(5):
                handle.create_dataset(
                    f"data{index}",
                    data=self.values + index * 1000,
                    chunks=(4, 5),
                    compression="gzip",
                )
            if flushed:
                handle.flush()
        image = inspect_cache_image(self.source)
        self.assertTrue(image["present"])
        return image

    def check_values(self):
        with h5py.File(self.output, "r") as handle:
            for index in range(5):
                np.testing.assert_array_equal(handle[f"data{index}"][:], self.values + index * 1000)
        result = preflight(self.output)
        self.assertEqual(result["decision"], "accept", result)
        self.assertFalse(result["cache_image"]["present"])

    def corrupt(self, image):
        with self.source.open("r+b") as stream:
            stream.seek(image["offset"] + image["bytes"] - 1)
            old = stream.read(1)
            stream.seek(-1, 1)
            stream.write(bytes([old[0] ^ 1]))

    def test_replay_dirty_cached_metadata_then_detach_and_read_values(self):
        image = self.fixture(userblock=512)
        before = self.source.read_bytes()
        result = plan_repair(self.source, self.plan, discard_cache_image=True)
        self.assertTrue(result["executable"], result)
        self.assertTrue(any("restore_cached_metadata" in a["repairs"] for a in result["actions"]))
        apply_repair(self.source, self.plan, self.output, self.audit)
        self.check_values()
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(before[image["offset"] :], self.output.read_bytes()[image["offset"] :])

    def test_corrupt_cache_can_be_detached_when_ordinary_metadata_survives(self):
        image = self.fixture(flushed=True)
        self.corrupt(image)
        before = self.source.read_bytes()
        self.assertTrue(
            any(f["code"] == "CORRUPT_CACHE_IMAGE" for f in preflight(self.source)["findings"])
        )
        self.assertFalse(plan_repair(self.source)["executable"])
        result = plan_repair(self.source, self.plan, discard_cache_image=True)
        self.assertTrue(result["executable"], result)
        apply_repair(self.source, self.plan, self.output, self.audit)
        self.check_values()
        self.assertEqual(before, self.source.read_bytes())

    def test_corrupt_cache_with_missing_ordinary_metadata_has_no_executable_plan(self):
        self.corrupt(self.fixture())
        before = self.source.read_bytes()
        result = plan_repair(self.source, self.plan, discard_cache_image=True)
        self.assertFalse(result["executable"], result)
        with self.assertRaises(UnsupportedCase):
            apply_repair(self.source, self.plan, self.output, self.audit)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.audit.exists())
        self.assertEqual(before, self.source.read_bytes())

    def test_cache_inspection_remains_independent_of_native_opens(self):
        self.fixture(flushed=True)
        with patch("h5py.File", side_effect=AssertionError("native open forbidden")):
            result = preflight(self.source)
        self.assertEqual(result["decision"], "accept", result)
        self.assertTrue(result["cache_image"]["checksum_valid"])
        self.assertFalse(result["boundary"]["native_file_opens"])

    def test_cache_replay_requires_independent_coverage_for_every_changed_byte(self):
        self.fixture()
        from h5reclaim.cache_image import require_covered_updates

        with self.assertRaisesRegex(UnsupportedFormat, "independently checked"):
            require_covered_updates(
                [(10, 20, "restore_cached_metadata", "")], [(10, 19, "checked")]
            )

    def test_cache_budget_is_checked_before_decode_or_replay(self):
        self.fixture()
        result = plan_repair(
            self.source, discard_cache_image=True, budget=LargeBudget(max_metadata_bytes=64)
        )
        self.assertFalse(result["executable"], result)
        self.assertFalse(self.output.exists())

    def test_cli_cache_plan_flag_is_retained_for_regenerated_application(self):
        self.fixture(flushed=True)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(
                [
                    "plan-repair",
                    str(self.source),
                    "--discard-cache-image",
                    "--output",
                    str(self.plan),
                ]
            )
        self.assertEqual(code, 0)
        saved = json.loads(self.plan.read_text())
        self.assertTrue(saved["options"]["discard_cache_image"])
        self.assertFalse(saved["options"]["allow_reseal"])
        apply_repair(self.source, self.plan, self.output, self.audit)
        self.check_values()


if __name__ == "__main__":
    unittest.main()
