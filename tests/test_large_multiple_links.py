"""Independent damaged index parents must each provide their own repair proof."""

from __future__ import annotations

import tempfile
import subprocess
import sys
import json
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError, UnsupportedFormat
from h5reclaim.large_streaming import LargeBudget
from h5reclaim.large_structural import _repair_dependencies, recover_large_structural
from h5reclaim.modern_indexes import ModernH5File


class MultipleLinkTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.source = self.folder / "source.h5"

    def _index(self, shape, chunks, maxshape, *, filtered=False, max_chunks=8192):
        with h5py.File(self.source, "r") as file:
            dataset = file["data"]
            address = h5py.h5o.get_info(dataset.id).addr
            plist = dataset.id.get_create_plist()
            filters = tuple(plist.get_filter(i)[0] for i in range(plist.get_nfilters()))
        with ModernH5File(self.source, large_sparse_scan=True, max_chunks=max_chunks) as reader:
            return reader.read_index(address, shape, chunks, 4,
                maxshape=maxshape, filters=filters,
                max_chunks=max_chunks)

    def _damage(self, offsets):
        original = self.source.read_bytes()
        broken = bytearray(original)
        for offset in offsets:
            broken[offset] ^= 0x20
        self.source.write_bytes(broken)
        return bytes(broken)

    def test_ea_header_and_secondary_parent_repairs_preserve_all_values(self):
        truth = np.arange(4, dtype="<u4").reshape((2, 2))
        with h5py.File(self.source, "w", libver="latest") as file:
            data = file.create_dataset("data", shape=(1, 0), maxshape=(1_000_000, None),
                chunks=(1, 1), dtype="<u4", compression="gzip", shuffle=True, fletcher32=True)
            data.resize((2, 2))
            data[...] = truth
        index = self._index((2, 2), (1, 1), (1_000_000, None), filtered=True)
        secondary = next(step for record in index.chunks
                         for step in record.evidence["index_chain"]
                         if step["kind"] == "easb_to_eadb")
        damaged = self._damage((index.data_block_pointer_offset, secondary["pointer_offset"]))
        output, report_path = self.folder / "result.h5", self.folder / "result.json"
        report = recover_large_structural(self.source, "/data", output, report_path)
        self.assertEqual(report["index"]["type"], "extensible_array")
        self.assertEqual([x["kind"] for x in report["index"]["repairs"]],
                         ["eahd_to_eaib", "easb_to_eadb"])
        self.assertEqual(report["accepted_elements"], truth.size)
        self.assertEqual(self.source.read_bytes(), damaged)
        with h5py.File(output) as saved:
            np.testing.assert_array_equal(saved["data"][:], truth)
            evidence = saved["/_h5reclaim/physical_evidence"][:]
            self.assertTrue(np.any(evidence["repair_link_count"] == 2))
            self.assertTrue(np.any(evidence["repair_link_count"] == 1))
            self.assertTrue(np.all(evidence["parent_pointer_offset"][
                evidence["repair_link_count"] == 2] == 0))
        cli_output, cli_report = self.folder / "cli.h5", self.folder / "cli.json"
        completed = subprocess.run([sys.executable, "-m", "h5reclaim", "rescue",
                                    str(self.source), "--dataset", "/data",
                                    "--output", str(cli_output), "--report", str(cli_report)],
                                   capture_output=True, text=True, timeout=45)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        cli = json.loads(cli_report.read_text())
        self.assertEqual(len(cli.get("unresolved_links", cli.get("index", {}).get("repairs", []))), 2)
        with h5py.File(cli_output) as saved:
            np.testing.assert_array_equal(saved["data"][:], truth)

    def test_two_btree_internal_parents_repair_independently(self):
        truth = np.arange(180 * 180, dtype="<u4").reshape((180, 180))
        with h5py.File(self.source, "w", libver="latest") as file:
            file.create_dataset("data", data=truth, chunks=(2, 2),
                                maxshape=(None, None))
        index = self._index((180, 180), (2, 2), (None, None))
        self.assertEqual(index.index_type, "v2_btree")
        candidates = {}
        for item in index.chunks:
            path = item.evidence["link_path"]
            if len(path) >= 3:
                parent = path[-1]["source_address"]
                candidates.setdefault(parent, path[-1]["pointer_offset"])
            if len(candidates) == 2:
                break
        self.assertEqual(len(candidates), 2)
        self._damage(tuple(candidates.values()))
        with ModernH5File(self.source, large_sparse_scan=True) as reader:
            repaired = reader.read_index(index.object_address, (180, 180), (2, 2), 4,
                maxshape=(None, None), max_chunks=8192)
        self.assertEqual(len(repaired.reconstructed_links), 2)
        self.assertEqual({item["source_address"] for item in repaired.reconstructed_links},
                         set(candidates))
        self.assertEqual([item.address for item in repaired.chunks],
                         [item.address for item in index.chunks])
        counts = [_repair_dependencies(repaired, item, repaired.reconstructed_links)
                  for item in repaired.chunks]
        self.assertTrue(any(len(dependencies) == 1 for dependencies in counts))
        self.assertTrue(any(len(dependencies) == 0 for dependencies in counts))
        saved_path = self.folder / "btree-result.h5"
        report = recover_large_structural(self.source, "/data", saved_path,
                                          self.folder / "btree-report.json")
        self.assertEqual(len(report["index"]["repairs"]), 2)
        self.assertEqual(report["accepted_elements"], truth.size)
        with h5py.File(saved_path) as saved:
            np.testing.assert_array_equal(saved["data"][:], truth)
            ledger = saved["/_h5reclaim/physical_evidence"][:]
            self.assertTrue(np.any(ledger["repair_link_count"] == 0))
            self.assertTrue(np.any(ledger["repair_link_count"] == 1))

    def test_multiple_repairs_share_one_physical_scan_quota(self):
        with h5py.File(self.source, "w", libver="latest") as file:
            ds = file.create_dataset("data", shape=(1, 0), maxshape=(1_000_000, None),
                chunks=(1, 1), dtype="<u4", compression="gzip", shuffle=True, fletcher32=True)
            ds.resize((2, 2))
            ds[...] = np.arange(4, dtype="<u4").reshape((2, 2))
        index = self._index((2, 2), (1, 1), (1_000_000, None), filtered=True)
        secondary = next(step for record in index.chunks
                         for step in record.evidence["index_chain"]
                         if step["kind"] == "easb_to_eadb")
        original = self._damage((index.data_block_pointer_offset, secondary["pointer_offset"]))
        budget = LargeBudget(max_scan_bytes=self.source.stat().st_size + 16)
        with self.assertRaisesRegex(UnsupportedFormat, "cumulative byte budget"):
            recover_large_structural(self.source, "/data", self.folder / "out.h5",
                                     self.folder / "out.json", budget=budget)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse((self.folder / "out.h5").exists())

    def test_two_eaib_links_in_one_parent_recover_with_both_dependencies(self):
        with h5py.File(self.source, "w", libver="latest") as file:
            ds = file.create_dataset("data", shape=(0,), maxshape=(None,),
                                     chunks=(10,), dtype="<u4")
            ds.resize((400,))
            ds[:] = np.arange(400, dtype="<u4")
        index = self._index((400,), (10,), (None,))
        slots = list(dict.fromkeys(step["pointer_offset"] for chunk in index.chunks
                   for step in chunk.evidence["index_chain"] if step["kind"] == "eaib_to_eadb"))
        self.assertGreaterEqual(len(slots), 2)
        damaged = self._damage(slots[:2])
        report = recover_large_structural(self.source, "/data", self.folder / "out.h5",
                                          self.folder / "out.json")
        self.assertEqual([r["source_address"] for r in report["index"]["repairs"]],
                         [index.data_block_address] * 2)
        self.assertEqual({r["pointer_offset"] for r in report["index"]["repairs"]},
                         set(slots[:2]))
        self.assertEqual(report["accepted_elements"], 400)
        self.assertEqual(self.source.read_bytes(), damaged)
        with h5py.File(self.folder / "out.h5") as result:
            np.testing.assert_array_equal(result["data"][:], np.arange(400, dtype="<u4"))
        cli_output, cli_report = self.folder / "same-parent-cli.h5", self.folder / "same-parent-cli.json"
        completed = subprocess.run([sys.executable, "-m", "h5reclaim", "rescue",
                                    str(self.source), "--dataset", "/data",
                                    "--output", str(cli_output), "--report", str(cli_report)],
                                   capture_output=True, text=True, timeout=45)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(json.loads(cli_report.read_text())["unresolved_links"]), 2)
        with h5py.File(cli_output) as result:
            np.testing.assert_array_equal(result["data"][:], np.arange(400, dtype="<u4"))

    def test_two_easb_links_in_one_parent_recover_filtered_values(self):
        truth = np.arange(64, dtype="<u4").reshape((8, 8))
        with h5py.File(self.source, "w", libver="latest") as file:
            ds = file.create_dataset("data", shape=(1_000_000, 0),
                                     maxshape=(1_000_000, None), chunks=(1, 1),
                                     dtype="<u4", compression="gzip", shuffle=True,
                                     fletcher32=True)
            ds.resize((8, 8))
            ds[...] = truth
        index = self._index((8, 8), (1, 1), (1_000_000, None), filtered=True)
        parents: dict[int, set[int]] = {}
        for chunk in index.chunks:
            for step in chunk.evidence["index_chain"]:
                if step["kind"] == "easb_to_eadb":
                    parents.setdefault(step["parent_address"], set()).add(step["pointer_offset"])
        parent, slots = next((address, sorted(offsets))
                             for address, offsets in parents.items() if len(offsets) >= 2)
        damaged = self._damage(slots[:2])
        output, report_path = self.folder / "secondary-result.h5", self.folder / "secondary-report.json"
        report = recover_large_structural(self.source, "/data", output, report_path)
        repairs = [r for r in report["index"]["repairs"] if r["source_address"] == parent]
        self.assertEqual({r["pointer_offset"] for r in repairs}, set(slots[:2]))
        self.assertEqual(report["accepted_elements"], truth.size)
        self.assertEqual(self.source.read_bytes(), damaged)
        with h5py.File(output) as saved:
            np.testing.assert_array_equal(saved["data"][:], truth)

    def test_nested_damaged_parent_and_child_refuse_without_checked_child(self):
        with h5py.File(self.source, "w", libver="latest") as file:
            ds = file.create_dataset("data", shape=(0,), maxshape=(None,),
                                     chunks=(10,), dtype="<u4")
            ds.resize((400,))
            ds[:] = np.arange(400, dtype="<u4")
        index = self._index((400,), (10,), (None,))
        index_pointer = next(step["pointer_offset"] for item in index.chunks
            for step in item.evidence["index_chain"] if step["kind"] == "eaib_to_eadb")
        self._damage((index.data_block_pointer_offset, index_pointer))
        with self.assertRaises(FormatError):
            recover_large_structural(self.source, "/data", self.folder / "out.h5",
                                     self.folder / "out.json")
        self.assertFalse((self.folder / "out.h5").exists())

    def test_high_rank_ea_and_btree_round_trip_through_checked_repair(self):
        shape = (4, 4, 1, 1, 1, 1)
        chunks = (1,) * len(shape)
        truth = np.arange(16, dtype="<u4").reshape(shape)
        for kind, maximum in (("extensible_array", (None, 4, 1, 1, 1, 1)),
                              ("v2_btree", (None, None, 1, 1, 1, 1))):
            with self.subTest(kind=kind):
                with h5py.File(self.source, "w", libver="latest") as file:
                    dataset = file.create_dataset("data", shape=(0, *shape[1:]),
                        maxshape=maximum, chunks=chunks, dtype="<u4",
                        compression="gzip")
                    dataset.resize(shape)
                    dataset[...] = truth
                healthy = self._index(shape, chunks, maximum, filtered=False)
                self.assertEqual(healthy.index_type, kind)
                if kind == "extensible_array":
                    pointer = healthy.data_block_pointer_offset
                else:
                    pointer = healthy.base_address + 16
                self._damage((pointer,))
                output = self.folder / f"{kind}.h5"
                report = recover_large_structural(self.source, "/data", output,
                                                   self.folder / f"{kind}.json")
                self.assertEqual(report["accepted_elements"], truth.size)
                with h5py.File(output) as saved:
                    np.testing.assert_array_equal(saved["data"][:], truth)

    def test_million_slot_sparse_fixed_array_is_recovered_with_explicit_grid_quota(self):
        n = 1_000_001
        expected = {0: 17, n // 2: 91, n - 1: 211}
        with h5py.File(self.source, "w", libver="latest") as file:
            data = file.create_dataset("data", shape=(n,), chunks=(1,), dtype="<u4")
            for position, value in expected.items():
                data[position] = value
        healthy = self._index((n,), (1,), (n,), max_chunks=n)
        self.assertEqual(healthy.index_type, "fixed_array")
        self.assertEqual(len(healthy.chunks), len(expected))
        self._damage((healthy.data_block_pointer_offset,))
        result = self.folder / "million.h5"
        report = recover_large_structural(self.source, "/data", result,
            self.folder / "million.json", budget=LargeBudget(max_chunks=n, max_grid=n))
        self.assertEqual(report["accepted_elements"], len(expected))
        self.assertEqual(report["unknown_elements"], n - len(expected))
        with h5py.File(result) as saved:
            for position, value in expected.items():
                self.assertEqual(saved["data"][position], value)
                self.assertEqual(saved["/_h5reclaim/validity"][position], 1)
            self.assertEqual(saved["/_h5reclaim/validity"][1], 0)


if __name__ == "__main__":
    unittest.main()
