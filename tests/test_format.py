"""Small byte-level examples exercise parser boundaries and attribution rules."""

from __future__ import annotations

import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

from h5reclaim.format import FormatError, H5File, UnsupportedFormat


BASE = 512
OBJECT = 0x100
ROOT = 0x200
LEAVES = (0xD00, 0x1900, 0x2500)
PAYLOADS = (0x3100, 0x3110, 0x3120)
UNDEFINED = 0xFFFFFFFFFFFFFFFF


def _key(column: int, *, size: int = 8) -> bytes:
    return struct.pack("<IIQQQ", size, 0, 0, column, 0)


def _node(
    level: int,
    entries: list[tuple[int, int]],
    end: int,
    left: int = UNDEFINED,
    right: int = UNDEFINED,
    end_size: int = 0,
) -> bytes:
    result = bytearray(b"TREE" + bytes((1, level)) + struct.pack("<HQQ", len(entries), left, right))
    for column, address in entries:
        result += _key(column) + struct.pack("<Q", address)
    result += _key(end, size=end_size)
    return bytes(result)


def _sample(*, broken: bool = False, userblock: bool = True) -> bytearray:
    base = BASE if userblock else 0
    output = bytearray(base + 0x4000)
    superblock = bytearray(24 + 32)
    superblock[:8] = b"\x89HDF\r\n\x1a\n"
    superblock[8] = 0
    superblock[13:15] = b"\x08\x08"
    struct.pack_into("<HH", superblock, 16, 4, 16)
    superblock[24:56] = struct.pack("<QQQQ", base, UNDEFINED, len(output), UNDEFINED)
    output[base : base + len(superblock)] = superblock

    layout = bytes((3, 2, 3)) + struct.pack("<QIII", ROOT, 1, 2, 4) + b"\x00"
    object_header = struct.pack("<BBHIII", 1, 0, 1, 1, 8 + len(layout), 0)
    object_header += struct.pack("<HHB3x", 8, len(layout), 0) + layout
    output[base + OBJECT : base + OBJECT + len(object_header)] = object_header
    root = _node(
        1,
        [(0, LEAVES[0]), (2, UNDEFINED if broken else LEAVES[1]), (4, LEAVES[2])],
        6,
    )
    output[base + ROOT : base + ROOT + len(root)] = root
    for index, address in enumerate(LEAVES):
        leaf = _node(
            0,
            [(index * 2, PAYLOADS[index])],
            index * 2 + 2,
            LEAVES[index - 1] if index else UNDEFINED,
            LEAVES[index + 1] if index < 2 else UNDEFINED,
            end_size=8 if index < 2 else 0,
        )
        output[base + address : base + address + len(leaf)] = leaf
        output[base + PAYLOADS[index] : base + PAYLOADS[index] + 8] = struct.pack(
            "<II", index * 2, index * 2 + 1
        )
    return output


class FormatParserTests(unittest.TestCase):
    def _file(self, image: bytes, directory: str) -> Path:
        path = Path(directory) / "case.h5"
        path.write_bytes(image)
        return path

    def test_userblock_base_layout_and_healthy_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._file(_sample(), directory)
            before = hashlib.sha256(path.read_bytes()).digest()
            with H5File(path) as source:
                self.assertEqual(source.superblock.signature_offset, BASE)
                self.assertEqual(source.absolute(ROOT), BASE + ROOT)
                layout = source.read_dataset_layout(OBJECT)
                self.assertEqual(
                    (layout.root_address, layout.chunk_shape, layout.element_size),
                    (ROOT, (1, 2), 4),
                )
                walk = source.walk_tree(layout.root_address)
                self.assertEqual(len(walk.nodes), 4)
                self.assertEqual(walk.broken_links, ())
                self.assertEqual(source.find_missing_child_candidates(ROOT), ())
                self.assertEqual(
                    source.read_tree(ROOT).entries[1].pointer_offset,
                    BASE + ROOT + 24 + (32 + 8) + 32,
                )
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).digest())

    def test_two_sided_reciprocal_candidate_for_undefined_parent_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with H5File(self._file(_sample(broken=True), directory)) as source:
                walk = source.walk_tree(ROOT)
                self.assertEqual(len(walk.nodes), 3)
                self.assertEqual(len(walk.broken_links), 1)
                self.assertEqual(walk.broken_links[0].entry_index, 1)
                (candidate,) = source.find_missing_child_candidates(ROOT)
                self.assertEqual(candidate.node.address, LEAVES[1])
                self.assertEqual(
                    (candidate.left_anchor, candidate.right_anchor),
                    (LEAVES[0], LEAVES[2]),
                )
                self.assertEqual(candidate.node.entries[0].key.offsets, (0, 2, 0))

    def test_nonreciprocal_or_out_of_bounds_candidate_is_refused(self) -> None:
        for corruption in ("sibling", "bounds"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                image = _sample(broken=True)
                if corruption == "sibling":
                    # Right neighbor no longer independently identifies leaf 2.
                    struct.pack_into("<Q", image, BASE + LEAVES[2] + 8, UNDEFINED)
                else:
                    # Detached leaf's lower boundary disagrees with parent.
                    image[BASE + LEAVES[1] + 24 : BASE + LEAVES[1] + 56] = _key(3)
                with H5File(self._file(image, directory)) as source:
                    self.assertEqual(source.find_missing_child_candidates(ROOT), ())

    def test_truncated_pointer_and_wrong_node_type_fail_closed(self) -> None:
        for corruption in ("pointer", "node_type"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                image = _sample()
                if corruption == "pointer":
                    struct.pack_into("<Q", image, BASE + ROOT + 24 + 32, len(image) + 1)
                else:
                    image[BASE + ROOT + 4] = 0
                with H5File(self._file(image, directory)) as source:
                    with self.assertRaises(FormatError):
                        source.walk_tree(ROOT)

    def test_rejects_unsupported_superblock_and_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = _sample(userblock=False)
            image[8] = 3
            with self.assertRaises(UnsupportedFormat):
                H5File(self._file(image, directory))
            image = _sample(userblock=False)
            image[OBJECT + 16 + 8] = 4
            with H5File(self._file(image, directory)) as source:
                with self.assertRaises(UnsupportedFormat):
                    source.read_dataset_layout(OBJECT)

    def test_rejects_malformed_layout_header_and_bad_tree_count(self) -> None:
        for corruption in ("header", "tree"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                image = _sample(userblock=False)
                if corruption == "header":
                    struct.pack_into("<H", image, OBJECT + 16 + 2, 0xFFFE)
                else:
                    struct.pack_into("<H", image, ROOT + 6, 65)
                with H5File(self._file(image, directory)) as source:
                    with self.assertRaises(FormatError):
                        if corruption == "header":
                            source.read_dataset_layout(OBJECT)
                        else:
                            source.read_tree(ROOT)


if __name__ == "__main__":
    unittest.main()
