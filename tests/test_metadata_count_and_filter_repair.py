"""Recover real writer-generated structures against separately held values."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.checked_view import checked_root_view, metadata_corrections
from h5reclaim.filter_metadata import pipeline_fields, validate_native_buffers
from h5reclaim.format import FormatError, H5File, UnsupportedFormat
from h5reclaim.large_streaming import LargeBudget
from h5reclaim.legacy_counts import count_corrections
from h5reclaim.metadata_fallback import _messages, _old_messages
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.preflight import preflight
from h5reclaim.repair_plan import _object_header, apply_repair, plan_repair
from h5reclaim.rescue import auto_rescue
from h5reclaim.whole_file import rescue_all


class MetadataRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.output, self.plan, self.audit = (
            self.root / name for name in ("source.h5", "out.h5", "plan.json", "audit.jsonl")
        )
        self.report = self.root / "report.json"
        self.values = np.arange(120, dtype="i4").reshape(12, 10)

    def write_at(self, offset, value):
        with self.source.open("r+b") as stream:
            stream.seek(offset)
            stream.write(value)

    def legacy(self, *, userblock=0, continuation=False):
        with h5py.File(self.source, "w", userblock_size=userblock) as handle:
            selected = handle.create_dataset(
                "data", data=self.values, chunks=(4, 5), compression="gzip"
            )
            handle.create_dataset("second", data=self.values + 1000)
            if continuation:
                for index in range(35):
                    selected.attrs[f"attribute-{index}"] = "value" * 10
            address = int(h5py.h5o.get_info(selected.id).addr)
        return address

    def filtered(self, identifier=6, *, userblock=0):
        with h5py.File(self.source, "w", libver="latest", userblock_size=userblock) as handle:
            if identifier == 6:
                selected = handle.create_dataset(
                    "data", data=self.values, chunks=(4, 5), scaleoffset=0
                )
            else:
                typ = h5py.h5t.STD_I32LE.copy()
                typ.set_precision(16)
                creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                creation.set_chunk((4, 5))
                creation.set_filter(5, 0, ())
                space = h5py.h5s.create_simple(self.values.shape)
                selected = h5py.Dataset(
                    h5py.h5d.create(handle.id, b"data", typ, space, dcpl=creation)
                )
                selected[:] = self.values
            address = int(h5py.h5o.get_info(selected.id).addr)
        with ModernH5File(self.source) as reader:
            start, raw = _object_header(reader, address)
            messages = _messages(reader, address)
            message = next(m for m in messages if m.kind == 11)
            _, relative = pipeline_fields(message.data, 4)[0]
        return start, raw, message.absolute_offset + relative

    def damage_filter(self, identifier=6, *, userblock=0, fields=(2, 4), reseal=False):
        start, raw, params = self.filtered(identifier, userblock=userblock)
        proposal = bytearray(raw)
        for field in fields:
            at = params + field * 4 - start
            proposal[at : at + 4] = (12345).to_bytes(4, "little")
        if reseal:
            proposal[-4:] = lookup3(proposal[:-4]).to_bytes(4, "little")
        self.write_at(start, proposal)
        return start, raw, params

    def apply_and_compare(self, *, allow_reseal=False):
        before = self.source.read_bytes()
        result = plan_repair(self.source, self.plan, allow_reseal=allow_reseal)
        self.assertTrue(result["executable"], result)
        self.assertTrue(result["actions"], result)
        apply_repair(self.source, self.plan, self.output, self.audit)
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(preflight(self.output)["decision"], "accept")
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["data"][:], self.values)
        return result

    def test_legacy_message_counts_include_continuation_messages(self):
        address = self.legacy(continuation=True)
        with H5File(self.source) as reader:
            messages = _old_messages(reader, address)
            self.assertTrue(any(m.kind == 16 for m in messages))
            offset = reader.absolute(address) + 2
        self.write_at(offset, b"\xff\xff")
        self.apply_and_compare()

    def test_legacy_root_and_dataset_message_counts_together(self):
        address = self.legacy()
        self.write_at(96 + 2, b"\0\0")
        self.write_at(address + 2, b"\0\0")
        result = self.apply_and_compare()
        self.assertEqual(len(result["actions"]), 2)

    def test_symbol_count_overflow_and_understatement_with_userblocks(self):
        for block in (0, 512, 4096):
            for count in (0, 1, 65535):
                with self.subTest(userblock=block, count=count):
                    self.legacy(userblock=block)
                    raw = self.source.read_bytes()
                    at = raw.index(b"SNOD", block)
                    self.write_at(at + 6, count.to_bytes(2, "little"))
                    changes = count_corrections(self.source)
                    self.assertEqual(changes[0]["after"], 2)
                    with checked_root_view(self.source) as (view, correction):
                        self.assertEqual(correction["checksum_bytes_changed"], 0)
                        with h5py.File(view, "r") as handle:
                            np.testing.assert_array_equal(handle["data"][:], self.values)

    def test_unrooted_symbol_signature_is_not_repaired(self):
        self.legacy()
        with self.source.open("ab") as stream:
            stream.write(b"SNOD\x01\0\xff\xff" + b"\0" * 500)
        self.assertEqual(count_corrections(self.source), [])

    def test_noncontiguous_symbol_entries_are_not_inferred(self):
        self.legacy()
        at = self.source.read_bytes().index(b"SNOD")
        self.write_at(at + 6, b"\0\0")
        self.write_at(at + 8, b"\0" * 40)
        with self.assertRaisesRegex(FormatError, "noncontiguous"):
            count_corrections(self.source)
        self.assertFalse(plan_repair(self.source)["executable"])

    def test_legacy_count_scan_honors_object_budget(self):
        self.legacy()
        with self.assertRaisesRegex(UnsupportedFormat, "object"):
            count_corrections(self.source, budget=LargeBudget(max_objects=1))

    def test_legacy_count_repair_is_automatic_for_selected_and_whole_file(self):
        address = self.legacy()
        self.write_at(address + 2, b"\0\0")
        before = self.source.read_bytes()
        selected = auto_rescue(self.source, "/data", self.output, self.report)
        self.assertEqual(selected["outcome"], "complete")
        result = rescue_all(self.source, self.root / "whole.h5", self.root / "whole.json")
        self.assertEqual(result["datasets_exported"], 2, result)
        self.assertEqual(result["outcome"], "complete", result)
        self.assertEqual(before, self.source.read_bytes())
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["data"][:], self.values)

    def test_nbit_and_scaleoffset_redundant_fields_restore_original_checksum(self):
        for identifier in (5, 6):
            for fields in ((2,), (4,), (2, 4)):
                with self.subTest(filter=identifier, fields=fields):
                    self.plan.unlink(missing_ok=True)
                    self.output.unlink(missing_ok=True)
                    self.audit.unlink(missing_ok=True)
                    self.damage_filter(identifier, fields=fields, userblock=512)
                    result = self.apply_and_compare()
                    self.assertEqual(len(result["actions"]), 1)
                    self.assertEqual(len(result["actions"][0]["repairs"]), len(fields))
                    self.assertIn(
                        "original header checksum restored", result["actions"][0]["evidence"][0]
                    )

    def test_checksum_valid_parameter_mismatch_needs_explicit_reseal(self):
        self.damage_filter(reseal=True)
        before = self.source.read_bytes()
        with patch("h5py.File", side_effect=AssertionError("native open forbidden")):
            inspection = preflight(self.source)
        self.assertEqual(inspection["decision"], "reject_corrupt")
        self.assertTrue(
            any(f["code"] == "FILTER_PARAMETER_MISMATCH" for f in inspection["findings"])
        )
        self.assertFalse(plan_repair(self.source)["executable"])
        self.assertEqual(before, self.source.read_bytes())
        result = self.apply_and_compare(allow_reseal=True)
        self.assertIn("recomputed", result["actions"][0]["evidence"][0])

    def test_native_decode_rejects_inconsistent_buffer_before_reading(self):
        self.damage_filter(reseal=True)
        with h5py.File(self.source, "r") as handle:
            with self.assertRaisesRegex(FormatError, "element count"):
                validate_native_buffers(handle["data"])

    def test_unrelated_filter_corruption_cannot_be_hidden_by_buffer_repair(self):
        start, raw, params = self.damage_filter()
        self.write_at(params + 3 * 4, (99).to_bytes(4, "little"))
        self.assertFalse(plan_repair(self.source, allow_reseal=True)["executable"])

    def test_filter_repairs_recover_automatically_and_embed_the_original_source(self):
        self.damage_filter()
        before = self.source.read_bytes()
        selected = auto_rescue(self.source, "/data", self.output, self.report)
        self.assertEqual(selected["accepted_elements"], self.values.size)
        self.assertEqual(selected["checked_root_correction"]["checksum_bytes_changed"], 0)
        whole = rescue_all(self.source, self.root / "whole.h5", self.root / "whole.json")
        self.assertEqual(whole["outcome"], "complete", whole)
        self.assertEqual(before, self.source.read_bytes())
        with h5py.File(self.output, "r") as handle:
            np.testing.assert_array_equal(handle["data"][:], self.values)
            embedded = json.loads(handle[selected["metadata_group"] + "/report_json"][()])
            self.assertEqual(embedded, selected)

    def test_valid_scaleoffset_descriptors_with_twenty_parameters_pass_preflight(self):
        self.filtered()
        result = preflight(self.source)
        self.assertEqual(result["decision"], "accept", result)

    def test_filter_correction_proposals_honor_the_metadata_byte_budget(self):
        self.damage_filter()
        from h5reclaim.metadata import UnsupportedCase

        with self.assertRaisesRegex(UnsupportedCase, "metadata byte budget"):
            metadata_corrections(self.source, LargeBudget(max_metadata_bytes=64))

    def test_understated_legacy_symbol_count_is_not_accepted_as_a_complete_namespace(self):
        self.legacy()
        at = self.source.read_bytes().index(b"SNOD")
        self.write_at(at + 6, (1).to_bytes(2, "little"))
        result = preflight(self.source)
        self.assertEqual(result["decision"], "reject_corrupt", result)


if __name__ == "__main__":
    unittest.main()
