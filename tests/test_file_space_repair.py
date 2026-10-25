"""Rooted persistent file-space repair must retain independent section evidence."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.file_space_repair import (
    _information_addresses, _rooted_addresses, inspect_file_space, repair_file_space,
)
from h5reclaim.format import FormatError
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.metadata_fallback import HeaderMessage, _messages
from h5reclaim.repair_plan import plan_repair, apply_repair
from h5reclaim.preflight import inspect_metadata_local


class FileSpaceRepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "space.h5"
        with h5py.File(self.source, "w", libver="latest", fs_strategy="fsm", fs_persist=True) as handle:
            handle.create_dataset("deleted", data=np.arange(5000, dtype="i4"), chunks=(50,), compression="gzip")
            handle.create_dataset("kept", data=np.arange(50, dtype="i4"))
            del handle["deleted"]

    def damage(self, at):
        with self.source.open("r+b") as stream:
            stream.seek(at)
            value = stream.read(1)
            stream.seek(at)
            stream.write(bytes([value[0] ^ 0x40]))

    def test_rooted_file_space_sections_are_checked(self):
        inventory = inspect_file_space(self.source)
        self.assertGreater(len(inventory["managers"]), 0)
        self.assertTrue(all(item["serialized_sections"] > 0 for item in inventory["managers"]))
        self.assertTrue(any("section list" in item[2] for item in inventory["metadata_ranges"]))
        preflight = inspect_metadata_local(self.source)
        self.assertEqual(preflight["decision"], "accept")
        self.assertEqual(preflight["file_space"]["manager_count"], len(inventory["managers"]))

    def test_original_checksum_restores_one_header_field(self):
        manager = inspect_file_space(self.source)["managers"][0]
        pristine = self.source.read_bytes()
        self.damage(manager["address"] + 6)
        with self.assertRaises(FormatError):
            inspect_file_space(self.source)
        touches = []
        repair_file_space(self.source, touches)
        self.assertEqual(self.source.read_bytes(), pristine)
        self.assertEqual(touches[0][2:], ("restore_file_space_header_field", "original checksum restored"))

    def test_two_damaged_fixed_header_bytes_restore_original_checksum(self):
        manager = inspect_file_space(self.source)["managers"][0]
        pristine = self.source.read_bytes()
        self.damage(manager["address"])
        self.damage(manager["address"] + 1)
        with self.assertRaises(FormatError):
            inspect_file_space(self.source)
        touches = []
        repair_file_space(self.source, touches)
        self.assertEqual(self.source.read_bytes(), pristine)
        self.assertIn("restore_file_space_header_prefix", [item[2] for item in touches])

    def test_checked_header_repair_is_planned_and_applied_to_new_copy(self):
        manager = inspect_file_space(self.source)["managers"][0]
        self.damage(manager["address"] + 6)
        before = self.source.read_bytes()
        preflight = inspect_metadata_local(self.source)
        self.assertEqual(preflight["decision"], "reject_corrupt")
        self.assertTrue(any(finding["code"] == "CORRUPT_FILE_SPACE" for finding in preflight["findings"]))
        plan_path, output, audit = (self.root / name for name in ("plan.json", "output.h5", "audit.jsonl"))
        plan = plan_repair(self.source, plan_path)
        self.assertTrue(plan["executable"], plan)
        self.assertTrue(any("restore_file_space_header_field" in action["repairs"]
                            for action in plan["actions"]))
        apply_repair(self.source, plan_path, output, audit)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertGreater(len(inspect_file_space(output)["managers"]), 0)
        self.assertEqual(inspect_metadata_local(output)["decision"], "accept")
        with h5py.File(output, "r") as handle:
            np.testing.assert_array_equal(handle["kept"][:], np.arange(50, dtype="i4"))
        self.assertIn("repair_verified", audit.read_text())

    def test_broken_file_space_declaration_is_restored_before_manager_walk(self):
        pristine = self.source.read_bytes()
        with ModernH5File(self.source, large_sparse_scan=True) as reader:
            sb = reader.superblock
            raw = reader._read_absolute(sb.signature_offset, 16 + 4 * sb.offset_size)
            extension = int.from_bytes(raw[12 + sb.offset_size:12 + 2 * sb.offset_size], "little")
            info = next(message for message in _messages(reader, extension) if message.kind == 0x17)
            field = next(position for position in range(29, len(info.data) - sb.offset_size + 1, sb.offset_size)
                         if info.data[position:position + sb.offset_size] != bytes([255]) * sb.offset_size)
            damaged_at = info.absolute_offset + field
        self.damage(damaged_at)
        self.assertEqual(inspect_metadata_local(self.source)["decision"], "reject_corrupt")
        plan_path, output, audit = (self.root / name for name in ("info.json", "info.h5", "info.jsonl"))
        planned = plan_repair(self.source, plan_path)
        self.assertTrue(planned["executable"], planned)
        self.assertTrue(any("restore_file_space_info_field" in action["repairs"]
                            for action in planned["actions"]))
        apply_repair(self.source, plan_path, output, audit)
        self.assertEqual(output.read_bytes(), pristine)
        self.assertEqual(inspect_metadata_local(output)["decision"], "accept")

    def test_declaration_and_its_manager_can_be_repaired_in_one_plan(self):
        manager_address = inspect_file_space(self.source)["managers"][0]["address"]
        pristine = self.source.read_bytes()
        with ModernH5File(self.source, large_sparse_scan=True) as reader:
            sb = reader.superblock
            raw = reader._read_absolute(sb.signature_offset, 16 + 4 * sb.offset_size)
            extension = int.from_bytes(raw[12 + sb.offset_size:12 + 2 * sb.offset_size], "little")
            info = next(message for message in _messages(reader, extension) if message.kind == 0x17)
            positions = range(29, len(info.data) - sb.offset_size + 1, sb.offset_size)
            field = next(pos for pos in positions
                         if int.from_bytes(info.data[pos:pos + sb.offset_size], "little") == manager_address)
            info_byte = info.absolute_offset + field
        self.damage(manager_address + 6)
        self.damage(info_byte)
        plan_path, output, audit = (self.root / name for name in ("both.json", "both.h5", "both.jsonl"))
        planned = plan_repair(self.source, plan_path)
        self.assertTrue(planned["executable"], planned)
        actions = {kind for action in planned["actions"] for kind in action["repairs"]}
        self.assertIn("restore_file_space_info_field", actions)
        self.assertIn("restore_file_space_header_field", actions)
        apply_repair(self.source, plan_path, output, audit)
        self.assertEqual(output.read_bytes(), pristine)

    def test_file_space_extension_checksum_reseal_needs_opt_in(self):
        with ModernH5File(self.source, large_sparse_scan=True) as reader:
            sb = reader.superblock
            raw = reader._read_absolute(sb.signature_offset, 16 + 4 * sb.offset_size)
            extension = int.from_bytes(raw[12 + sb.offset_size:12 + 2 * sb.offset_size], "little")
            prefix = reader.read_at(extension, 6)
            flags = prefix[5]
            head_size = 6 + (16 if flags & 32 else 0) + (4 if flags & 16 else 0)
            width = 1 << (flags & 3)
            size = int.from_bytes(reader.read_at(extension + head_size, width), "little")
            checksum_at = reader.absolute(extension) + head_size + width + size
        self.damage(checksum_at + 3)
        self.assertFalse(plan_repair(self.source)["executable"])
        planned = plan_repair(self.source, allow_reseal=True)
        self.assertTrue(planned["executable"], planned)
        self.assertTrue(any("reseal_file_space_info_header" in action["repairs"]
                            for action in planned["actions"]))

    def test_checksum_reseal_needs_opt_in_and_intact_sections(self):
        manager = inspect_file_space(self.source)["managers"][0]
        self.damage(manager["metadata_ranges"][0][1] - 1)
        self.assertFalse(plan_repair(self.source)["executable"])
        planned = plan_repair(self.source, allow_reseal=True)
        self.assertTrue(planned["executable"], planned)
        self.assertTrue(any("reseal_file_space_header" in action["repairs"]
                            for action in planned["actions"]))

    def test_original_section_checksum_repairs_detached_section_field(self):
        manager = inspect_file_space(self.source)["managers"][0]
        section = manager["section_list_address"]
        pristine = self.source.read_bytes()
        self.damage(section + 13)
        self.assertEqual(inspect_metadata_local(self.source)["decision"], "reject_corrupt")
        plan_path, output, audit = (self.root / name for name in ("sections.json", "sections.h5", "sections.jsonl"))
        plan = plan_repair(self.source, plan_path)
        self.assertTrue(plan["executable"], plan)
        self.assertTrue(any("restore_file_space_section_field" in action["repairs"]
                            for action in plan["actions"]))
        apply_repair(self.source, plan_path, output, audit)
        self.assertEqual(output.read_bytes(), pristine)

    def test_two_damaged_reciprocal_owner_bytes_restore_original_checksum(self):
        manager = inspect_file_space(self.source)["managers"][0]
        pristine = self.source.read_bytes()
        section = manager["section_list_address"]
        self.damage(section + 5)
        self.damage(section + 6)
        with self.assertRaises(FormatError):
            inspect_file_space(self.source)
        touches = []
        repair_file_space(self.source, touches)
        self.assertEqual(self.source.read_bytes(), pristine)
        self.assertIn("restore_file_space_section_prefix", [item[2] for item in touches])

    def test_section_checksum_reseal_requires_opt_in(self):
        manager = inspect_file_space(self.source)["managers"][0]
        section_end = manager["metadata_ranges"][1][1]
        self.damage(section_end - 1)
        self.assertFalse(plan_repair(self.source)["executable"])
        planned = plan_repair(self.source, allow_reseal=True)
        self.assertTrue(planned["executable"], planned)
        self.assertTrue(any("reseal_file_space_section_list" in action["repairs"]
                            for action in planned["actions"]))

    def test_bad_section_list_cannot_be_published_by_resealing_header(self):
        manager = inspect_file_space(self.source)["managers"][0]
        start = manager["metadata_ranges"][1][0]
        # Count and size are variable historical values, unlike the fixed
        # signature and reciprocal owner, so neither can be invented.
        self.damage(start + 13)
        self.damage(start + 14)
        with self.assertRaises(FormatError):
            inspect_file_space(self.source)
        with self.assertRaises(FormatError):
            repair_file_space(self.source, [], allow_reseal=True)
        self.assertFalse(plan_repair(self.source, allow_reseal=True)["executable"])

    def test_absent_persistent_managers_are_empty_inventory(self):
        basic = self.root / "default.h5"
        with h5py.File(basic, "w", libver="latest") as handle:
            handle.create_dataset("data", data=np.arange(8))
        self.assertEqual(inspect_file_space(basic)["managers"], [])

    def test_version_zero_six_manager_layout_and_narrow_offsets(self):
        sb = SimpleNamespace(offset_size=4, length_size=4, undefined_address=(1 << 32) - 1)
        addresses = (0x100, sb.undefined_address, 0x400, sb.undefined_address,
                     sb.undefined_address, sb.undefined_address)
        message = bytes([0, 1]) + (1).to_bytes(4, "little")
        message += b"".join(address.to_bytes(4, "little") for address in addresses)
        roots, version, page_size = _information_addresses(message, 0, sb)
        self.assertEqual((roots, version, page_size),
                         ([0x100, 0x400], "v0_file_space_info_and_v0_file_manager", 0))
        with self.assertRaisesRegex(FormatError, "duplicate"):
            _information_addresses(message[:-4] + (0x100).to_bytes(4, "little"), 0, sb)
        with self.assertRaisesRegex(FormatError, "length"):
            _information_addresses(message + b"\x00", 0, sb)
        for strategy in (2, 3, 4):
            roots, _, _ = _information_addresses(bytes([0, strategy]) + (1).to_bytes(4, "little"), 0, sb)
            self.assertEqual(roots, [])

    def test_historical_type_eighteen_is_distinguished_from_cache_image(self):
        sb = SimpleNamespace(offset_size=4, length_size=4, undefined_address=(1 << 32) - 1,
                             signature_offset=0)
        superblock = bytearray(16 + 4 * sb.offset_size)
        superblock[12 + sb.offset_size:12 + 2 * sb.offset_size] = (0x200).to_bytes(4, "little")
        reader = SimpleNamespace(superblock=sb, _read_absolute=lambda *args: bytes(superblock),
                                 metadata_ranges=[(0, 32, "superblock"), (0x200, 0x230, "extension")])
        address_table = [0x100, sb.undefined_address, 0x400,
                         sb.undefined_address, sb.undefined_address, sb.undefined_address]
        old = bytes([0, 1]) + (1).to_bytes(4, "little")
        old += b"".join(value.to_bytes(4, "little") for value in address_table)
        with patch("h5reclaim.file_space_repair._messages", return_value=(HeaderMessage(0x18, 0, old, 0),)):
            roots, _, label, page = _rooted_addresses(reader)
        self.assertEqual((roots, page), ([0x100, 0x400], 0))
        self.assertIn("legacy_type_0x18", label)
        cache = bytes([0]) + (0x400).to_bytes(4, "little") + (200).to_bytes(4, "little")
        with patch("h5reclaim.file_space_repair._messages", return_value=(HeaderMessage(0x18, 0, cache, 0),)):
            roots, _, label, page = _rooted_addresses(reader)
        self.assertEqual((roots, label, page), ([], "no_file_space_info", 0))

    def test_paged_managers_accept_small_and_large_section_types(self):
        paged = self.root / "paged.h5"
        with h5py.File(paged, "w", libver="latest", fs_strategy="page", fs_persist=True) as handle:
            for name in "abc":
                handle.create_dataset(name, data=np.arange(100000, dtype="u1"))
            del handle["b"]
        inventory = inspect_file_space(paged)
        self.assertEqual(inventory["version"], "v1_file_space_info_and_v0_file_manager")
        types = {typ for manager in inventory["managers"]
                 for typ in manager["serialized_section_types"]}
        self.assertEqual(types, {1, 2})
        self.assertEqual(inspect_metadata_local(paged)["decision"], "accept")
        with h5py.File(paged, "r") as handle:
            np.testing.assert_array_equal(handle["a"][:], np.arange(100000, dtype="u1"))

    def test_paged_section_repair_requires_original_checksum_or_explicit_reseal(self):
        paged = self.root / "paged-repair.h5"
        with h5py.File(paged, "w", libver="latest", fs_strategy="page", fs_persist=True) as handle:
            handle.create_dataset("kept", data=np.arange(32))
            handle.create_dataset("deleted", data=np.arange(256), chunks=(16,))
            del handle["deleted"]
        manager = next(m for m in inspect_file_space(paged)["managers"]
                       if m["section_list_used_bytes"])
        end = manager["section_list_address"] + manager["section_list_used_bytes"]
        with paged.open("r+b") as stream:
            stream.seek(end - 1)
            current = stream.read(1)
            stream.seek(end - 1)
            stream.write(bytes([current[0] ^ 0x40]))
        self.assertFalse(plan_repair(paged)["executable"])
        plan_path, output, audit = (self.root / name for name in ("paged.json", "paged-out.h5", "paged-audit.jsonl"))
        planned = plan_repair(paged, plan_path, allow_reseal=True)
        self.assertTrue(planned["executable"], planned)
        self.assertTrue(any("reseal_file_space_section_list" in action["repairs"]
                            for action in planned["actions"]))
        apply_repair(paged, plan_path, output, audit)
        self.assertEqual(inspect_metadata_local(output)["decision"], "accept")
        with h5py.File(output, "r") as handle:
            np.testing.assert_array_equal(handle["kept"][:], np.arange(32))

    def test_large_structurally_valid_section_list_can_be_explicitly_resealed(self):
        large = self.root / "many-sections.h5"
        with h5py.File(large, "w", libver="latest", fs_strategy="fsm", fs_persist=True) as handle:
            for index in range(400):
                handle.create_dataset(f"x{index:04d}", data=np.arange(100, dtype="u1"), chunks=(20,))
            for index in range(0, 400, 2):
                del handle[f"x{index:04d}"]
        manager = next(m for m in inspect_file_space(large)["managers"]
                       if m["section_list_used_bytes"] > 1024)
        end = manager["section_list_address"] + manager["section_list_used_bytes"]
        with large.open("r+b") as stream:
            stream.seek(end - 1)
            value = stream.read(1)
            stream.seek(end - 1)
            stream.write(bytes([value[0] ^ 0x40]))
        self.assertFalse(plan_repair(large)["executable"])
        planned = plan_repair(large, allow_reseal=True)
        self.assertTrue(planned["executable"], planned)
        self.assertTrue(any("reseal_file_space_section_list" in action["repairs"]
                            for action in planned["actions"]))

    def test_unknown_paged_section_type_rejected_even_with_reseal(self):
        paged = self.root / "invalid-paged-type.h5"
        with h5py.File(paged, "w", libver="latest", fs_strategy="page", fs_persist=True) as handle:
            handle.create_dataset("deleted", data=np.arange(256), chunks=(16,))
            del handle["deleted"]
        manager = next(m for m in inspect_file_space(paged)["managers"]
                       if 1 in m["serialized_section_types"])
        address = manager["section_list_address"]
        size = manager["section_list_used_bytes"]
        with paged.open("r+b") as stream:
            stream.seek(address)
            raw = bytearray(stream.read(size))
            # This fixture has one section set with 1-byte count and 8-byte size/address fields.
            raw[5 + 8 + 1 + 8 + 8] = 3
            raw[-4:] = lookup3(raw[:-4]).to_bytes(4, "little")
            stream.seek(address)
            stream.write(raw)
        with self.assertRaisesRegex(FormatError, "invalid type"):
            inspect_file_space(paged)
        self.assertFalse(plan_repair(paged, allow_reseal=True)["executable"])


if __name__ == "__main__":
    unittest.main()
