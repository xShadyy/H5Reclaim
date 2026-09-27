"""Real public command on one deliberately damaged checked index link."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.modern_link_repair import candidate_addresses
from h5reclaim.modern_indexes import ModernH5File


class ModernLinkRepairCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_candidate_scan_stops_at_physical_eof_when_declared_eof_is_larger(self):
        class TailReader:
            size = 9
            superblock = type("Superblock", (), {
                "eof_address": 20, "base_address": 0, "offset_size": 8,
            })()

            def _read_absolute(self, start, length):
                payload = b".....EAIB"
                if start + length > len(payload):
                    raise AssertionError("scanner read beyond the physical tail")
                return payload[start:start+length]

        self.assertEqual(list(candidate_addresses(TailReader(), b"EAIB")), [5])

    def rescue(self, source: Path) -> dict[str, object]:
        output = self.folder / "recovered.h5"
        report = self.folder / "recovered.json"
        completed = subprocess.run(
            [sys.executable, "-m", "h5reclaim", "rescue", str(source),
             "--dataset", "/science", "--output", str(output),
             "--report", str(report)],
            text=True, capture_output=True, timeout=45,
        )
        self.assertEqual(completed.returncode, 0,
                         f"stdout={completed.stdout}\nstderr={completed.stderr}")
        self.assertTrue(output.exists())
        return json.loads(report.read_text())

    def test_eaib_checked_child_pointer_damage(self):
        source = self.folder / "extensible.h5"
        with h5py.File(source, "w", libver="latest") as file:
            ds = file.create_dataset("science", shape=(0,), maxshape=(None,),
                                     chunks=(10,), dtype="<u4")
            ds.resize((400,))
            ds[...] = np.arange(400, dtype="<u4")
        with h5py.File(source) as file, ModernH5File(source) as raw:
            address = h5py.h5o.get_info(file["science"].id).addr
            idx = raw.read_index(address, (400,), (10,), 4,
                                 maxshape=(None,), filters=())
            edge = next(step for chunk in idx.chunks for step in chunk.evidence["index_chain"]
                        if step["kind"] == "eaib_to_eadb")
        damaged = bytearray(source.read_bytes())
        damaged[edge["pointer_offset"]] ^= 0x10
        source.write_bytes(damaged)
        report = self.rescue(source)
        self.assertTrue(report["structural_repair"])
        self.assertEqual(report["counts"]["recovered"], 40)
        self.assertEqual(report["unresolved_links"][0]["kind"], "eaib_to_child")
        with h5py.File(self.folder / "recovered.h5") as output:
            np.testing.assert_array_equal(output["science"][:], np.arange(400, dtype="<u4"))
        self.assertEqual(source.read_bytes(), bytes(damaged))

    def test_bthd_checked_root_pointer_damage(self):
        source = self.folder / "btree.h5"
        with h5py.File(source, "w", libver="latest") as file:
            ds = file.create_dataset("science", shape=(60, 60),
                                     maxshape=(None, None), chunks=(2, 2), dtype="<u4")
            ds[...] = np.arange(3600, dtype="<u4").reshape((60, 60))
        with h5py.File(source) as file, ModernH5File(source) as raw:
            address = h5py.h5o.get_info(file["science"].id).addr
            idx = raw.read_index(address, (60, 60), (2, 2), 4,
                                 maxshape=(None, None), filters=())
            root = idx.base_address
        damaged = bytearray(source.read_bytes())
        damaged[root+16] ^= 0x40
        source.write_bytes(damaged)
        report = self.rescue(source)
        self.assertTrue(report["structural_repair"])
        self.assertEqual(report["counts"]["recovered"], 900)
        self.assertEqual(report["unresolved_links"][0]["kind"], "bthd_to_root")
        with h5py.File(self.folder / "recovered.h5") as output:
            np.testing.assert_array_equal(
                output["science"][:], np.arange(3600, dtype="<u4").reshape(60, 60))
        self.assertTrue(any("selected object native hard-link count unavailable" in reason
                            for reason in report["ownership_inventory"]["incomplete_reasons"]))
        self.assertEqual(source.read_bytes(), bytes(damaged))


if __name__ == "__main__":
    unittest.main()
