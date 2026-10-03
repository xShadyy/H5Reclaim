"""Regression cases for automatic routes, logical types and recovery sessions."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.format import FormatError
from h5reclaim.hints import HintsError
from h5reclaim.large_streaming import LargeBudget, export_large_readable
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.native_io import read_fixed_block
from h5reclaim.native_stream import export_native_stream
from h5reclaim.object_discovery import discover, export_object
from h5reclaim.recovery import RecoveryError, sha256_file
from h5reclaim.rescue import auto_rescue
from h5reclaim.whole_file import rescue_all


class UniversalityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source, self.output, self.report = (self.base / name for name in ("input.h5", "output.h5", "report.json"))

    def test_scaleoffset_automatic_fallback_recovers_values(self):
        values = np.arange(32, dtype="i4")
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("data", data=values, chunks=(8,), scaleoffset=0)
        before = sha256_file(self.source)
        report = auto_rescue(self.source, "/data", self.output, self.report, audit_science_context=False)
        self.assertEqual(report["outcome"], "complete")
        self.assertGreater(len(report["automatic_routing"]["attempted"]), 1)
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle["data"][:], values)
        self.assertEqual(sha256_file(self.source), before)
        self.assertEqual(report["output_path"], str(self.output))

    def _damaged_rank5(self):
        values = np.arange(64, dtype="i4").reshape((2, 2, 2, 2, 4))
        with h5py.File(self.source, "w") as handle:
            dataset = handle.create_dataset("data", data=values, chunks=(1, 1, 1, 1, 4),
                                            compression="gzip", fletcher32=True)
            mask, raw = dataset.id.read_direct_chunk((0,) * 5)
            dataset.id.write_direct_chunk((0,) * 5, raw[:-1] + bytes([raw[-1] ^ 1]), filter_mask=mask)
        return values

    def test_automatic_partial_native_export_retains_other_chunks(self):
        values = self._damaged_rank5()
        before = sha256_file(self.source)
        report = auto_rescue(self.source, "/data", self.output, self.report, audit_science_context=False)
        self.assertEqual((report["accepted_elements"], report["unknown_elements"]), (60, 4))
        self.assertEqual(report["outcome"], "partial")
        with h5py.File(self.output) as handle:
            status = handle[report["validity_map"]][:]
            self.assertEqual(status[(0,) * 5], 6)
            self.assertEqual(int(np.count_nonzero(status == 1)), 15)
            np.testing.assert_array_equal(handle["data"][1], values[1])
            self.assertFalse(np.any(handle["/_h5reclaim/historical_status"][:]))
        self.assertEqual(sha256_file(self.source), before)

    def test_explicit_large_export_retains_good_chunks(self):
        self._damaged_rank5()
        report = export_large_readable(self.source, "/data", self.output, self.report)
        self.assertEqual(report["accepted_elements"], 60)
        self.assertEqual(len(report["failed_chunks"]), 1)

    def test_empty_and_null_datasets_in_whole_file(self):
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("empty", shape=(0,), maxshape=(None,), chunks=(8,), dtype="i4")
            handle.create_dataset("null", data=h5py.Empty("f8"))
        report = rescue_all(self.source, self.output, self.report)
        self.assertEqual(report["outcome"], "complete")
        with h5py.File(self.output) as handle:
            self.assertEqual(handle["empty"].shape, (0,))
            self.assertEqual(handle["empty"].maxshape, (None,))
            self.assertIsNone(handle["null"].shape)
            self.assertIsInstance(handle["null"][()], h5py.Empty)

    def test_compound_variable_string_and_nested_array_fields(self):
        dtype = np.dtype([("id", ">i4"), ("label", h5py.string_dtype()), ("vector", "<f8", (2,))])
        rows = np.array([(1, b"one", [1.5, -2]), (2, b"two", [3.5, 4])], dtype=dtype)
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("data", data=rows, chunks=(1,))
        report = auto_rescue(self.source, "/data", self.output, self.report, audit_science_context=False)
        self.assertEqual(report["accepted_elements"], 2)
        with h5py.File(self.source) as source, h5py.File(self.output) as output:
            self.assertTrue(source["data"].id.get_type().equal(output["data"].id.get_type()))
            np.testing.assert_array_equal(output["data"][:]["id"], rows["id"])
            np.testing.assert_array_equal(output["data"][:]["vector"], rows["vector"])
            np.testing.assert_array_equal(output["data"][:]["label"], rows["label"])

    def test_raw_record_io_refuses_nested_variable_string_pointers(self):
        dtype = np.dtype([("label", h5py.string_dtype())])
        with h5py.File(self.source, "w") as handle:
            selected = handle.create_dataset("data", data=np.array([(b"one",)], dtype=dtype))
            with self.assertRaises(ValueError):
                read_fixed_block(selected, (slice(0, 1),))

    def test_compound_complex_and_variable_string_fields(self):
        dtype = np.dtype([("wave", ">c16"), ("label", h5py.string_dtype())])
        rows = np.array([(1.25 - 2.5j, b"wave"), (3.75 + 4j, b"signal")], dtype=dtype)
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("data", data=rows)
        report = export_native_stream(self.source, "/data", self.output, self.report)
        self.assertEqual(report["accepted_elements"], 2)
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle["data"][:]["wave"], rows["wave"])
            np.testing.assert_array_equal(handle["data"][:]["label"], rows["label"])

    def test_big_endian_ragged_numeric_values(self):
        with h5py.File(self.source, "w") as handle:
            dataset = handle.create_dataset("data", shape=(2,), dtype=h5py.vlen_dtype(np.dtype(">i4")))
            from h5reclaim.variable_readable import _write_value
            for index, values in enumerate(([1, -2, 3], [4])):
                _write_value(dataset, (index,), np.array(values, dtype=">i4"), "numeric", np.dtype(">i4"))
        report = export_native_stream(self.source, "/data", self.output, self.report)
        self.assertEqual(report["accepted_elements"], 2)
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle["data"][0], [1, -2, 3])
            self.assertEqual(handle["data"].id.get_type().get_super().get_order(), h5py.h5t.ORDER_BE)

    def _references(self):
        with h5py.File(self.source, "w") as handle:
            data = handle.create_dataset("data", data=np.arange(10))
            refs = handle.create_dataset("refs", shape=(3,), dtype=h5py.ref_dtype)
            refs[:] = [data.ref, refs.ref, h5py.Reference()]
            regions = handle.create_dataset("regions", shape=(1,), dtype=h5py.regionref_dtype)
            regions[0] = data.regionref[2:5]
            handle.attrs["reference"] = data.ref
            data.attrs["other"] = refs.ref
            handle.attrs["strings"] = np.array(["one", "two"], dtype=h5py.string_dtype())
            handle["soft"] = h5py.SoftLink("/data")
            handle["relative"] = h5py.SoftLink("data")

    def test_whole_file_cross_references_attributes_and_soft_links(self):
        self._references()
        before = sha256_file(self.source)
        report = rescue_all(self.source, self.output, self.report)
        self.assertEqual(report["outcome"], "complete")
        with h5py.File(self.output) as handle:
            self.assertEqual([handle[ref].name if ref else None for ref in handle["refs"][:]],
                             ["/data", "/refs", None])
            np.testing.assert_array_equal(handle["data"][handle["regions"][0]], [2, 3, 4])
            self.assertEqual(handle[handle.attrs["reference"]].name, "/data")
            self.assertEqual(handle[handle["data"].attrs["other"]].name, "/refs")
            self.assertEqual(handle.attrs["strings"].tolist(), ["one", "two"])
            self.assertEqual(handle.get("soft", getlink=True).path, "/data")
            self.assertEqual(handle.get("relative", getlink=True).path, "data")
            np.testing.assert_array_equal(handle["soft"][:], handle["data"][:])
        self.assertEqual(sha256_file(self.source), before)

    def test_compound_reference_members_remap_to_recovered_targets(self):
        dtype = np.dtype([("id", "i4"), ("target", h5py.ref_dtype), ("label", h5py.string_dtype())])
        with h5py.File(self.source, "w") as handle:
            target = handle.create_dataset("target", data=[7, 8])
            handle.create_dataset("records", data=np.array([(12, target.ref, b"target")], dtype=dtype))
        report = rescue_all(self.source, self.output, self.report)
        self.assertEqual(report["outcome"], "complete")
        with h5py.File(self.output) as handle:
            row = handle["records"][0]
            self.assertEqual(handle[row["target"]].name, "/target")
            self.assertEqual(row["id"], 12)
            self.assertEqual(row["label"], b"target")

    def test_selected_cross_references_remain_unknown_until_targets_exist(self):
        self._references()
        report = auto_rescue(self.source, "/refs", self.output, self.report,
                             prefer_streaming=True, audit_science_context=False)
        self.assertEqual(report["outcome"], "partial")
        self.assertEqual(report["accepted_elements"], 2)
        with h5py.File(self.output) as handle:
            self.assertEqual(handle[report["element_status"]][:].tolist(), [4, 1, 1])

    def test_one_source_snapshot_serves_whole_file_routes(self):
        with h5py.File(self.source, "w") as handle:
            for index in range(3):
                handle.create_dataset(str(index), data=np.arange(10))
        import h5reclaim.large_streaming as module
        original = module._copy_sparse
        with patch.object(module, "_copy_sparse", wraps=original) as copy:
            report = rescue_all(self.source, self.output, self.report)
        self.assertEqual(copy.call_count, 1)
        self.assertEqual(report["datasets_exported"], 3)

    def test_completed_dataset_checkpoints_are_reused(self):
        self._references()
        directory = self.base / "checkpoint"
        rescue_all(self.source, self.output, self.report, resume_dir=directory)
        with patch("h5reclaim.rescue.auto_rescue", side_effect=AssertionError("completed dataset was rerun")):
            result = rescue_all(self.source, self.base / "resumed.h5", self.base / "resumed.json", resume_dir=directory)
        self.assertEqual(result["checkpoint"]["datasets_reused"], 3)
        with h5py.File(self.base / "resumed.h5") as handle:
            self.assertEqual(handle[handle["refs"][0]].name, "/data")

    def test_checkpoints_reject_changed_source_and_cached_output(self):
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("data", data=[1, 2])
        directory = self.base / "checkpoint"
        rescue_all(self.source, self.output, self.report, resume_dir=directory)
        cached = next(directory.glob("*.h5"))
        with cached.open("r+b") as stream:
            stream.seek(-1, 2)
            value = stream.read(1)
            stream.seek(-1, 2)
            stream.write(bytes([value[0] ^ 1]))
        with self.assertRaises(RecoveryError):
            rescue_all(self.source, self.base / "again.h5", self.base / "again.json", resume_dir=directory)
        self.assertFalse((self.base / "again.h5").exists())
        with h5py.File(self.source, "r+") as handle:
            handle["data"][0] = 3
        with self.assertRaises(RecoveryError):
            rescue_all(self.source, self.base / "changed.h5", self.base / "changed.json", resume_dir=directory)

    def test_whole_file_dependencies_materialize_only_pinned_sources(self):
        related, raw = self.base / "related.h5", self.base / "values.raw"
        with h5py.File(related, "w") as handle:
            handle.create_dataset("source", data=np.arange(5, dtype="i4"))
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle["linked"] = h5py.ExternalLink("related.h5", "/source")
            layout = h5py.VirtualLayout(shape=(5,), dtype="i4")
            layout[:] = h5py.VirtualSource("related.h5", "source", shape=(5,))
            handle.create_virtual_dataset("virtual", layout, fillvalue=-999)
            dataset = handle.create_dataset("external", shape=(5,), dtype="i4", external=[(str(raw), 0, 20)])
            dataset[:] = np.arange(5, dtype="i4")
        manifest = self.base / "related.json"
        manifest.write_text(json.dumps({"schema_version": 1, "files": [
            {"declared_name": "related.h5", "path": str(related), "sha256": sha256_file(related)},
            {"declared_name": str(raw), "path": str(raw), "sha256": sha256_file(raw)}]}), encoding="utf-8")
        report = rescue_all(self.source, self.output, self.report, related_files=manifest)
        self.assertEqual(report["outcome"], "complete", report["failures"])
        self.assertEqual(report["datasets_exported"], 3)
        with h5py.File(self.output) as handle:
            for name in ("linked", "virtual", "external"):
                np.testing.assert_array_equal(handle[name][:], np.arange(5))
                self.assertFalse(handle[name].is_virtual)
                self.assertEqual(handle[name].id.get_create_plist().get_external_count(), 0)

    def _broken_root(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            root = h5py.h5o.get_info(handle["/"].id).addr
            dataset = handle.create_dataset("data", data=np.arange(16, dtype="i4"), chunks=(4,),
                                           compression="gzip", fletcher32=True)
            address = h5py.h5o.get_info(dataset.id).addr
        with self.source.open("r+b") as stream:
            stream.seek(root)
            stream.write(b"BAD!")
        return address

    def test_checked_detached_header_discovery_and_export(self):
        address = self._broken_root()
        before = sha256_file(self.source)
        inventory = discover(self.source)
        self.assertEqual([entry["address"] for entry in inventory["datasets"]], [address])
        self.assertFalse(inventory["complete_namespace"])
        report = export_object(self.source, address, "/data", self.output, self.report)
        self.assertEqual(report["accepted_elements"], 16)
        self.assertFalse(report["ownership_inventory"]["complete"])
        self.assertEqual(report["metadata_discovery"]["inspection_view"]["kind"], "disposable_empty_root")
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle["data"][:], np.arange(16))
        self.assertEqual(sha256_file(self.source), before)

    def test_whole_file_discovers_surviving_objects_after_root_damage(self):
        address = self._broken_root()
        report = rescue_all(self.source, self.output, self.report)
        self.assertEqual(report["outcome"], "partial")
        self.assertEqual(report["datasets_exported"], 1)
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle[f"/recovered/object_{address:x}"][:], np.arange(16))

    def test_group_damage_retains_known_names_and_discovers_lost_names(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("good", data=[1, 2])
            group = handle.create_group("broken")
            damaged_address = h5py.h5o.get_info(group.id).addr
            dataset = group.create_dataset("data", data=np.arange(5))
            data_address = h5py.h5o.get_info(dataset.id).addr
        with self.source.open("r+b") as stream:
            stream.seek(damaged_address)
            stream.write(b"BAD!")
        report = rescue_all(self.source, self.output, self.report)
        self.assertEqual(report["datasets_exported"], 2)
        self.assertEqual(report["outcome"], "partial")
        with h5py.File(self.output) as handle:
            np.testing.assert_array_equal(handle["good"][:], [1, 2])
            np.testing.assert_array_equal(handle[f"/recovered/object_{data_address:x}"][:], np.arange(5))

    def test_metadata_only_file_preserves_groups_and_attributes(self):
        with h5py.File(self.source, "w") as handle:
            group = handle.create_group("experiment")
            group.attrs["name"] = "measurements pending"
            handle.attrs["version"] = 3
        report = rescue_all(self.source, self.output, self.report)
        self.assertEqual(report["outcome"], "complete")
        self.assertEqual(report["datasets_exported"], 0)
        with h5py.File(self.output) as handle:
            self.assertEqual(handle["experiment"].attrs["name"], "measurements pending")
            self.assertEqual(handle.attrs["version"], 3)

    def test_discovery_ignores_signature_without_valid_header_checksum(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            handle.create_dataset("data", data=np.frombuffer(b"OHDR\x02\x00\x04broken", dtype="u1"))
        result = discover(self.source)
        self.assertEqual(len(result["datasets"]), 1)
        self.assertGreaterEqual(result["rejected_headers"], 1)

    def test_object_address_and_hint_conflicts_publish_nothing(self):
        address = self._broken_root()
        hints = self.base / "hints.json"
        hints.write_text(json.dumps({"schema_version": 1, "dataset": {"path": "/data", "shape": [99]}}))
        from h5reclaim.hints import load_hints
        with self.assertRaises(HintsError):
            export_object(self.source, address, "/data", self.output, self.report, hints=load_hints(hints))
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())
        with self.assertRaises(UnsupportedCase):
            export_object(self.source, address + 1, "/data", self.output, self.report)
        self.assertFalse(self.output.exists())

    def test_rescue_hint_conflict_publishes_nothing(self):
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("data", data=np.arange(5))
        hints = self.base / "hints.json"
        hints.write_text(json.dumps({"schema_version": 1, "dataset": {"path": "/data", "shape": [6]}}))
        with self.assertRaises(HintsError):
            auto_rescue(self.source, "/data", self.output, self.report, hints=hints, audit_science_context=False)
        self.assertFalse(self.output.exists())

    def test_small_configured_budgets_are_applied_to_auto_routes(self):
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("data", data=np.arange(16), chunks=(4,))
        with self.assertRaises(UnsupportedCase):
            auto_rescue(self.source, "/data", self.output, self.report,
                         streaming_budget={"max_logical_bytes": 16}, audit_science_context=False)
        self.assertFalse(self.output.exists())
        with self.assertRaises(UnsupportedCase):
            auto_rescue(self.source, "/data", self.output, self.report,
                         streaming_budget={"max_grid": 2}, audit_science_context=False)
        self.assertFalse(self.output.exists())

    def test_fixed_stream_reads_bounded_pieces_inside_large_chunks(self):
        values = np.arange(4096, dtype="i4")
        with h5py.File(self.source, "w") as handle:
            handle.create_dataset("data", data=values, chunks=(4096,), compression="gzip")
        import h5reclaim.native_stream as module
        original = module.read_fixed_block
        sizes = []
        def read(dataset, selection):
            result = original(dataset, selection)
            sizes.append(result.nbytes)
            return result
        with patch.object(module, "read_fixed_block", side_effect=read):
            report = export_native_stream(self.source, "/data", self.output, self.report,
                                           budget=LargeBudget(block_bytes=128))
        self.assertEqual(report["accepted_elements"], 4096)
        self.assertLessEqual(max(sizes), 128)

    def test_userblock_native_evidence_records_actual_physical_payload(self):
        values = np.arange(32, dtype="i4")
        with h5py.File(self.source, "w", userblock_size=512) as handle:
            handle.create_dataset("data", data=values, chunks=(8,), compression="gzip")
            handle.create_dataset("sibling", data=values + 1000, chunks=(8,))
        report = export_native_stream(self.source, "/data", self.output, self.report)
        self.assertEqual(report["accepted_elements"], 32)
        with h5py.File(self.source) as handle, self.source.open("rb") as raw:
            for record in report["source_chunk_records"]:
                _, payload = handle["data"].id.read_direct_chunk(tuple(record["origin"]))
                raw.seek(record["address"])
                self.assertEqual(raw.read(record["stored_bytes"]), payload)
                self.assertEqual(record["raw_sha256"], hashlib.sha256(payload).hexdigest())

    def test_discovery_cli_json_and_object_rescue(self):
        address = self._broken_root()
        completed = subprocess.run([sys.executable, "-m", "h5reclaim", "discover", str(self.source), "--json"],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["datasets"][0]["address"], address)
        completed = subprocess.run([sys.executable, "-m", "h5reclaim", "rescue", str(self.source),
            "--dataset", "/data", "--object-address", hex(address), "--output", str(self.output),
            "--report", str(self.report), "--no-context-audit"], capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(self.report.read_text())["accepted_elements"], 16)


try:
    import hdf5plugin
except ImportError:
    hdf5plugin = None


@unittest.skipIf(hdf5plugin is None, "optional hdf5plugin codecs are not installed")
class OptionalCodecTests(unittest.TestCase):
    def test_packaged_codecs_decode_independently_attributed_raw_chunks(self):
        from h5reclaim.metadata import read_dataset_spec
        from h5reclaim.schema_codec import decode_chunk
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            values = np.arange(64, dtype="i4").reshape(8, 8)
            for name, options in {"zstd": hdf5plugin.Zstd(), "blosc": hdf5plugin.Blosc(),
                                  "blosc2": hdf5plugin.Blosc2(), "bitshuffle": hdf5plugin.Bitshuffle(),
                                  "lz4": hdf5plugin.LZ4()}.items():
                with self.subTest(codec=name):
                    source = base / (name + ".h5")
                    with h5py.File(source, "w") as handle:
                        dataset = handle.create_dataset("data", data=values, chunks=(4, 4), fletcher32=True, **options)
                        mask, raw = dataset.id.read_direct_chunk((0, 0))
                    spec = read_dataset_spec(source, "/data")
                    self.assertEqual(decode_chunk(raw, spec, mask), values[:4, :4].tobytes())

    def test_lossless_packaged_codecs_through_automatic_routes(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            values = (np.arange(4096, dtype="i4") % 13).reshape((64, 64))
            codecs = {"zstd": hdf5plugin.Zstd(), "blosc": hdf5plugin.Blosc(),
                      "blosc2": hdf5plugin.Blosc2(), "bitshuffle": hdf5plugin.Bitshuffle(),
                      "lz4": hdf5plugin.LZ4()}
            for name, options in codecs.items():
                with self.subTest(codec=name):
                    source, output, report_path = (base / (name + suffix) for suffix in (".h5", "-out.h5", ".json"))
                    with h5py.File(source, "w") as handle:
                        dataset = handle.create_dataset("data", data=values, chunks=(32, 32), **options)
                        self.assertEqual(dataset.id.get_chunk_info_by_coord((0, 0)).filter_mask, 0)
                    report = auto_rescue(source, "/data", output, report_path, audit_science_context=False)
                    self.assertEqual(report["outcome"], "complete")
                    with h5py.File(output) as handle:
                        np.testing.assert_array_equal(handle["data"][:], values)


if __name__ == "__main__":
    unittest.main()
