"""Rank-one raw B-tree and continued v1 object-header parser regressions."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from h5reclaim.format import FormatError, H5File, UnsupportedFormat


OBJECT = 0x100
ROOT = 0x400
LEAVES = (0x1000, 0x2000, 0x3000)
CONTINUATION = 0x3900
PAYLOADS = (0x5000, 0x5010, 0x5020)
UNDEFINED = (1 << 64) - 1


def _key(index: int, *, size: int = 32, element: int = 0) -> bytes:
    return struct.pack("<IIQQ", size, 0, index, element)


def _node(
    level: int,
    entries: list[tuple[int, int]],
    end: int,
    *,
    left: int = UNDEFINED,
    right: int = UNDEFINED,
    final_element: int = 0,
) -> bytes:
    data = bytearray(b"TREE" + bytes((1, level)) + struct.pack("<HQQ", len(entries), left, right))
    for coordinate, address in entries:
        data += _key(coordinate) + struct.pack("<Q", address)
    data += _key(end, size=0 if final_element else 32, element=final_element)
    return bytes(data)


def _image(*, broken: bool = False, duplicate_layout: bool = False) -> bytearray:
    image = bytearray(0x7000)
    header = bytearray(56)
    header[:8] = b"\x89HDF\r\n\x1a\n"
    header[13:15] = b"\x08\x08"
    struct.pack_into("<HH", header, 16, 4, 16)
    struct.pack_into("<QQQQ", header, 24, 0, UNDEFINED, len(image), UNDEFINED)
    image[:56] = header

    layout = (bytes((3, 2, 2)) + struct.pack("<QII", ROOT, 4, 8)).ljust(24, b"\x00")
    first_messages = (
        struct.pack("<HHB3x", 8, len(layout), 0)
        + layout
        + struct.pack("<HHB3xQQ", 16, 16, 0, CONTINUATION, 32 if duplicate_layout else 8)
        + b"\x00" * 8
    )
    primary = struct.pack("<BBHIII", 1, 0, 4, 1, len(first_messages), 0) + first_messages
    image[OBJECT : OBJECT + len(primary)] = primary
    image[CONTINUATION : CONTINUATION + (32 if duplicate_layout else 8)] = (
        struct.pack("<HHB3x", 8, 24, 0) + layout
        if duplicate_layout
        else b"\x00" * 8
    )

    root = _node(
        1,
        [(0, LEAVES[0]), (4, UNDEFINED if broken else LEAVES[1]), (8, LEAVES[2])],
        12,
        final_element=8,
    )
    image[ROOT : ROOT + len(root)] = root
    for i, leaf_addr in enumerate(LEAVES):
        leaf = _node(
            0,
            [(4 * i, PAYLOADS[i])],
            4 * (i + 1),
            left=LEAVES[i - 1] if i else UNDEFINED,
            right=LEAVES[i + 1] if i + 1 < len(LEAVES) else UNDEFINED,
            final_element=8 if i == len(LEAVES) - 1 else 0,
        )
        image[leaf_addr : leaf_addr + len(leaf)] = leaf
    return image


class GwoscFormatTests(unittest.TestCase):
    def _path(self, image: bytes, directory: str) -> Path:
        path = Path(directory) / "sample.h5"
        path.write_bytes(image)
        return path

    def test_rank_one_continuation_and_anchored_missing_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with H5File(self._path(_image(broken=True), directory)) as file:
                layout = file.read_dataset_layout(OBJECT, rank=1)
                self.assertEqual((layout.root_address, layout.chunk_shape, layout.element_size),
                                 (ROOT, (4,), 8))
                root = file.read_tree(ROOT, rank=1, element_size=8)
                self.assertEqual(root.level, 1)
                self.assertEqual(root.entries[1].pointer_offset, ROOT + 24 + 32 + 24)
                self.assertEqual(root.final_key.offsets, (12, 8))
                walk = file.walk_tree(ROOT, rank=1, element_size=8)
                self.assertEqual((len(walk.nodes), len(walk.broken_links)), (3, 1))
                (candidate,) = file.find_missing_child_candidates(ROOT, rank=1, element_size=8)
                self.assertEqual(candidate.node.address, LEAVES[1])
                self.assertEqual((candidate.left_anchor, candidate.right_anchor),
                                 (LEAVES[0], LEAVES[2]))
                self.assertIn((CONTINUATION, CONTINUATION + 8, "object header continuation"),
                              file.metadata_ranges)

    def test_rejects_rank_mismatch_and_unsupported_element_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with H5File(self._path(_image(), directory)) as file:
                with self.assertRaises(UnsupportedFormat):
                    file.read_dataset_layout(OBJECT)  # Declared default is rank two.
                with self.assertRaises(UnsupportedFormat):
                    file.read_tree(ROOT, rank=1, element_size=4)

    def test_rejects_overlapping_continuation_and_duplicate_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = _image()
            # The continuation pointer is relative and is located after the
            # first 32-byte layout message, at the start of its message body.
            struct.pack_into("<Q", image, OBJECT + 16 + 32 + 8, OBJECT)
            with H5File(self._path(image, directory)) as file:
                with self.assertRaises(FormatError):
                    file.read_dataset_layout(OBJECT, rank=1)
        with tempfile.TemporaryDirectory() as directory:
            with H5File(self._path(_image(duplicate_layout=True), directory)) as file:
                with self.assertRaises(FormatError):
                    file.read_dataset_layout(OBJECT, rank=1)

    def test_candidate_requires_reciprocal_neighbors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = _image(broken=True)
            struct.pack_into("<Q", image, LEAVES[2] + 8, UNDEFINED)
            with H5File(self._path(image, directory)) as file:
                self.assertEqual(
                    file.find_missing_child_candidates(ROOT, rank=1, element_size=8), ()
                )


if __name__ == "__main__":
    unittest.main()
