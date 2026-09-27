"""A valid-looking redirected hard link must not claim another dataset's values."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.metadata_fallback import _messages, read_dataset_spec_fallback
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.recovery import recover


def _patch_checked_header(path: Path, address: int, offset: int, value: int):
    raw = bytearray(path.read_bytes())
    flags = raw[address + 5]
    width = 1 << (flags & 3)
    extra = (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0)
    body = address + 6 + extra + width
    length = int.from_bytes(raw[body - width:body], "little")
    checksum = body + length
    assert address <= offset < checksum
    raw[offset] = value
    raw[checksum:checksum + 4] = lookup3(raw[address:checksum]).to_bytes(4, "little")
    path.write_bytes(raw)


class RootedModernLinkCountTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "damaged.h5"

    def _create(self):
        with h5py.File(self.source, "w", libver="latest") as handle:
            group = handle.create_group("lab")
            selected = group.create_dataset("science",
                data=np.arange(16, dtype="<u4").reshape(4, 4), chunks=(4, 4))
            distractor = group.create_dataset("distractor",
                data=np.full((4, 4), 99, dtype="<u4"), chunks=(4, 4))
            handle.create_group("elsewhere")["alias"] = distractor
            return (int(h5py.h5o.get_info(group.id).addr),
                    int(h5py.h5o.get_info(selected.id).addr),
                    int(h5py.h5o.get_info(distractor.id).addr))

    def test_valid_cross_group_alias_count_is_accepted(self):
        _group, selected, other = self._create()
        self.assertEqual(read_dataset_spec_fallback(
            self.source, "/lab/science").spec.object_address, selected)
        self.assertEqual(read_dataset_spec_fallback(
            self.source, "/elsewhere/alias").spec.object_address, other)

    def test_rechecksums_cannot_make_redirected_link_own_sibling_values(self):
        group, _selected, distractor = self._create()
        with ModernH5File(self.source) as reader:
            link = next(m for m in _messages(reader, group)
                        if m.kind == 6 and b"science" in m.data)
            fill = next(m for m in _messages(reader, distractor) if m.kind == 5)
            osize = reader.superblock.offset_size
        pointer = link.absolute_offset + len(link.data) - osize
        for index, byte in enumerate(distractor.to_bytes(osize, "little")):
            _patch_checked_header(self.source, group, pointer + index, byte)
        # The target's own optional fill message is invalid, making native
        # selected-object open fail and entering the raw metadata route.
        _patch_checked_header(self.source, distractor, fill.absolute_offset, 255)
        with self.assertRaisesRegex(FormatError, "rooted hard-link count contradicts"):
            read_dataset_spec_fallback(self.source, "/lab/science")
        output, report = self.root / "out.h5", self.root / "out.json"
        with self.assertRaisesRegex(UnsupportedCase, "rooted hard-link count contradicts"):
            recover(self.source, "/lab/science", output, report)
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
