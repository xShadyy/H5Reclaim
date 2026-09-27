"""Bounded, read-only parser for the first H5Reclaim recovery case.

This module accepts v0/v1 superblocks, v1 object headers with a v3 chunked
layout, and v1 type-1 B-tree nodes for a rank-two dataset. Addresses exposed
by HDF5 metadata are relative to the superblock base; pointer_offset is an
absolute byte offset in the source file. Parsing a plausible TREE signature
does not establish that a node belongs to a dataset.

Format reference: https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
sections II.A, III.A.1, IV.A.1.a, IV.A.3.i, and IV.A.3.q.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


SIGNATURE = b"\x89HDF\r\n\x1a\n"
KEY_SIZE = 32  # 4-byte size, 4-byte mask, three 8-byte offsets (rank 2 + 1).
DEFAULT_ISTORE_K = 32
MAX_TREE_NODES = 100_000
MAX_HEADER_BYTES = 1 << 20
MAX_READ_BYTES = 16 << 20
MAX_CHUNK_BYTES = MAX_READ_BYTES


class FormatError(ValueError):
    """Truncated, contradictory, or corrupt data in the supported format."""


class UnsupportedFormat(FormatError):
    """A structurally valid-looking variant outside the declared envelope."""


@dataclass(frozen=True)
class Superblock:
    version: int
    signature_offset: int
    base_address: int
    offset_size: int
    length_size: int
    eof_address: int
    istore_k: int

    @property
    def undefined_address(self) -> int:
        return (1 << (8 * self.offset_size)) - 1


@dataclass(frozen=True)
class DatasetLayout:
    root_address: int
    chunk_shape: tuple[int, int]
    element_size: int
    message_version: int


@dataclass(frozen=True)
class ChunkKey:
    stored_size: int
    filter_mask: int
    offsets: tuple[int, int, int]


@dataclass(frozen=True)
class ChildEntry:
    key: ChunkKey
    address: int | None
    pointer_offset: int


@dataclass(frozen=True)
class TreeNode:
    address: int
    level: int
    left_sibling: int | None
    right_sibling: int | None
    entries: tuple[ChildEntry, ...]
    final_key: ChunkKey


@dataclass(frozen=True)
class BrokenChild:
    parent_address: int
    entry_index: int
    pointer_offset: int
    key_start: ChunkKey
    key_end: ChunkKey


@dataclass(frozen=True)
class TreeWalk:
    nodes: tuple[TreeNode, ...]
    broken_links: tuple[BrokenChild, ...]


@dataclass(frozen=True)
class MissingChildCandidate:
    parent_address: int
    entry_index: int
    node: TreeNode
    left_anchor: int
    right_anchor: int


def _uint(data: bytes) -> int:
    return int.from_bytes(data, "little")


class H5File:
    """Own a file opened only for reading; all reads are file-size bounded."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file: BinaryIO = self.path.open("rb")
        try:
            self.size = self._file.seek(0, 2)
            self.superblock = self._parse_superblock()
            sb = self.superblock
            superblock_size = (28 if sb.version == 1 else 24) + 4 * sb.offset_size
            self.metadata_ranges: list[tuple[int, int, str]] = [
                (sb.signature_offset, sb.signature_offset + superblock_size, "superblock")
            ]
        except BaseException:
            self._file.close()
            raise

    def __enter__(self) -> H5File:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._file.close()

    def _read_absolute(self, offset: int, length: int) -> bytes:
        if self._file.closed:
            raise ValueError("source file is closed")
        if offset < 0 or length < 0 or offset > self.size or length > self.size - offset:
            raise FormatError(f"read outside source file: offset={offset}, length={length}")
        self._file.seek(offset)
        data = self._file.read(length)
        if len(data) != length:
            raise FormatError(f"short read at byte offset {offset}")
        return data

    def absolute(self, address: int) -> int:
        """Convert a defined HDF5 relative address to a physical byte offset."""
        if not isinstance(address, int) or address < 0:
            raise FormatError("invalid relative address")
        if address == self.superblock.undefined_address:
            raise FormatError("undefined address cannot be read")
        offset = self.superblock.base_address + address
        if offset >= self.size or offset >= self.superblock.eof_address:
            raise FormatError(f"relative address {address} is outside HDF5 data")
        return offset

    def read_at(self, address: int, length: int) -> bytes:
        """Read from a defined relative address without crossing the HDF5 EOF."""
        offset = self.absolute(address)
        if length > MAX_READ_BYTES:
            raise UnsupportedFormat("single-read size limit exceeded")
        if length < 0 or length > self.superblock.eof_address - offset:
            raise FormatError("read crosses HDF5 end-of-file address")
        return self._read_absolute(offset, length)

    def _parse_superblock(self) -> Superblock:
        signature_offset = 0
        while signature_offset < self.size:
            if (
                self.size - signature_offset >= 8
                and self._read_absolute(signature_offset, 8) == SIGNATURE
            ):
                break
            signature_offset = 512 if signature_offset == 0 else signature_offset * 2
        else:
            raise FormatError("HDF5 signature not found at a permitted offset")

        prefix = self._read_absolute(signature_offset, 24)
        version = prefix[8]
        if version not in (0, 1):
            raise UnsupportedFormat(f"superblock version {version} is unsupported")
        offset_size, length_size = prefix[13], prefix[14]
        if offset_size not in (2, 4, 8) or length_size not in (2, 4, 8):
            raise UnsupportedFormat("only 2-, 4-, and 8-byte offsets and lengths are supported")
        if prefix[11] or prefix[15] or prefix[9] or prefix[10] or prefix[12]:
            raise UnsupportedFormat("unexpected superblock version or reserved bytes")
        if not _uint(prefix[16:18]) or not _uint(prefix[18:20]):
            raise FormatError("invalid group B-tree K in superblock")
        header_length = 28 if version == 1 else 24
        header = self._read_absolute(signature_offset, header_length + 4 * offset_size)
        if version == 1:
            istore_k = _uint(header[24:26])
            if not istore_k or header[26:28] != b"\x00\x00":
                raise FormatError("invalid indexed-storage B-tree K")
        else:
            istore_k = DEFAULT_ISTORE_K
        if istore_k > 32767:
            raise UnsupportedFormat("indexed-storage B-tree K exceeds parser limits")
        base = _uint(header[header_length : header_length + offset_size])
        eof = _uint(header[header_length + 2 * offset_size : header_length + 3 * offset_size])
        # Files moved relative to a stored base require HDF5's rebasing rules.
        # The target fixture and ordinary user-block files store base==signature.
        if base != signature_offset:
            raise UnsupportedFormat("relocated superblock base address is unsupported")
        if eof > self.size or eof <= signature_offset + header_length + 4 * offset_size:
            raise FormatError("HDF5 end-of-file address is inconsistent with source size")
        driver = _uint(header[header_length + 3 * offset_size : header_length + 4 * offset_size])
        if driver != (1 << (8 * offset_size)) - 1:
            raise UnsupportedFormat("non-default HDF5 file drivers are unsupported")
        return Superblock(version, signature_offset, base, offset_size, length_size, eof, istore_k)

    def read_dataset_layout(self, object_address: int) -> DatasetLayout:
        """Read the selected object's own v1 header, never scan for a layout.

        The object address must come from an independent trusted dataset-name
        lookup. A continuation, shared layout, or duplicate layout is refused.
        """
        prefix = self.read_at(object_address, 16)
        if prefix[0] != 1:
            raise UnsupportedFormat(f"object header version {prefix[0]} is unsupported")
        if prefix[1] or prefix[12:16] != b"\x00" * 4:
            raise FormatError("invalid v1 object header reserved bytes")
        count = _uint(prefix[2:4])
        size = _uint(prefix[8:12])
        if not 1 <= count <= 4096 or not 8 <= size <= MAX_HEADER_BYTES:
            raise UnsupportedFormat("object header message count or size is out of scope")
        block = self.read_at(object_address + 16, size)
        cursor = 0
        layout: DatasetLayout | None = None
        found_count = 0
        while cursor < size:
            if size - cursor < 8:
                raise FormatError("truncated v1 object header message")
            kind = _uint(block[cursor : cursor + 2])
            message_size = _uint(block[cursor + 2 : cursor + 4])
            flags = block[cursor + 4]
            if block[cursor + 5 : cursor + 8] != b"\x00" * 3:
                raise FormatError("invalid object header message reserved bytes")
            cursor += 8
            if message_size % 8 or message_size > size - cursor:
                raise FormatError("object header message size is invalid")
            data = block[cursor : cursor + message_size]
            cursor += message_size
            found_count += 1
            if kind == 0x0010:
                raise UnsupportedFormat("v1 object header continuation is unsupported")
            if kind == 0x0008:
                if layout is not None:
                    raise FormatError("duplicate data layout message")
                if flags & 0x02:
                    raise UnsupportedFormat("shared layout message is unsupported")
                layout = self._parse_layout(data)
        if found_count != count:
            raise FormatError("object header message count mismatch")
        if layout is None:
            raise FormatError("selected object has no layout message")
        start = self.absolute(object_address)
        self.metadata_ranges.append((start, start + 16 + size, "selected object header"))
        return layout

    def _parse_layout(self, data: bytes) -> DatasetLayout:
        if len(data) < 3 or data[0] != 3 or data[1] != 2:
            raise UnsupportedFormat("only version-3 chunked data layouts are supported")
        if data[2] != 3:
            raise UnsupportedFormat("only rank-two chunked layouts are supported")
        offset_size = self.superblock.offset_size
        fields_end = 3 + offset_size + 12
        if len(data) < fields_end:
            raise FormatError("truncated chunked layout message")
        root = _uint(data[3 : 3 + offset_size])
        if root == self.superblock.undefined_address:
            raise UnsupportedFormat("selected dataset has no allocated chunk index")
        self.absolute(root)
        dimensions = tuple(
            _uint(data[3 + offset_size + 4 * i : 7 + offset_size + 4 * i])
            for i in range(3)
        )
        if any(value == 0 for value in dimensions):
            raise FormatError("zero chunk dimension or datatype element size")
        if dimensions[2] != 4:
            raise UnsupportedFormat("only four-byte datatype elements are supported")
        if any(data[fields_end:]):
            raise FormatError("nonzero layout message padding")
        return DatasetLayout(root, (dimensions[0], dimensions[1]), dimensions[2], 3)

    def _address_or_none(self, raw: bytes) -> int | None:
        value = _uint(raw)
        return None if value == self.superblock.undefined_address else value

    @staticmethod
    def _key(data: bytes) -> ChunkKey:
        offsets = (_uint(data[8:16]), _uint(data[16:24]), _uint(data[24:32]))
        return ChunkKey(_uint(data[0:4]), _uint(data[4:8]), offsets)

    def read_tree(self, address: int) -> TreeNode:
        """Decode only used entries of a rank-two, type-1 v1 B-tree node."""
        header = self.read_at(address, 8 + 2 * self.superblock.offset_size)
        if header[:4] != b"TREE":
            raise FormatError(f"no TREE signature at relative address {address}")
        if header[4] != 1:
            raise UnsupportedFormat(f"B-tree node type {header[4]} is unsupported")
        level, used = header[5], _uint(header[6:8])
        if used == 0 or used > 2 * self.superblock.istore_k:
            raise FormatError(f"B-tree node at {address} has invalid entries-used count {used}")
        offsize = self.superblock.offset_size
        left = self._address_or_none(header[8 : 8 + offsize])
        right = self._address_or_none(header[8 + offsize : 8 + 2 * offsize])
        if left == address or right == address or (left is not None and left == right):
            raise FormatError(f"invalid sibling link at B-tree node {address}")
        prefix_length = 8 + 2 * offsize
        # Version-1 nodes reserve 2K child slots and 2K+1 key slots even
        # when only a prefix is used. Their entire allocation is metadata.
        allocated_length = prefix_length + 2 * self.superblock.istore_k * (
            KEY_SIZE + offsize
        ) + KEY_SIZE
        if allocated_length > self.superblock.eof_address - self.absolute(address):
            raise FormatError(f"B-tree node at {address} crosses HDF5 end-of-file")
        content = self.read_at(address + prefix_length, used * (KEY_SIZE + offsize) + KEY_SIZE)
        entries: list[ChildEntry] = []
        previous_offset: tuple[int, int, int] | None = None
        for i in range(used + 1):
            start = i * (KEY_SIZE + offsize)
            key = self._key(content[start : start + KEY_SIZE])
            # HDF5's terminal, unallocated key can use the datatype-element
            # size as its final coordinate (4 for our supported uint32 case).
            if key.offsets[2] != 0 and not (
                i == used and key.stored_size == 0 and key.offsets[2] == 4
            ):
                raise FormatError(f"nonzero datatype-element offset in node {address}")
            if previous_offset is not None and previous_offset >= key.offsets:
                raise FormatError(f"unordered chunk keys in node {address}")
            previous_offset = key.offsets
            if i == used:
                final = key
                break
            pointer_offset = self.absolute(address) + prefix_length + start + KEY_SIZE
            target = self._address_or_none(content[start + KEY_SIZE : start + KEY_SIZE + offsize])
            if level == 0 and (target is None or key.stored_size == 0):
                raise FormatError(f"leaf at {address} has missing chunk data")
            if level == 0 and target is not None:
                if key.stored_size > MAX_CHUNK_BYTES:
                    raise UnsupportedFormat("chunk stored-size limit exceeded")
                physical = self.absolute(target)
                if key.stored_size > self.superblock.eof_address - physical:
                    raise FormatError(f"chunk at {target} crosses HDF5 end-of-file")
            entries.append(ChildEntry(key, target, pointer_offset))
        start = self.absolute(address)
        self.metadata_ranges.append((start, start + allocated_length, "B-tree node"))
        return TreeNode(address, level, left, right, tuple(entries), final)

    def walk_tree(self, root_address: int, *, max_nodes: int = MAX_TREE_NODES) -> TreeWalk:
        """Traverse rooted child links; preserve undefined internal links as gaps."""
        if max_nodes <= 0:
            raise ValueError("max_nodes must be positive")
        if max_nodes > MAX_TREE_NODES:
            raise UnsupportedFormat("requested traversal node limit exceeds parser cap")
        visited: dict[int, TreeNode] = {}
        broken: list[BrokenChild] = []
        stack: list[
            tuple[int, int | None, ChunkKey | None, ChunkKey | None]
        ] = [(root_address, None, None, None)]
        while stack:
            address, expected_level, lower, upper = stack.pop()
            if address in visited:
                raise FormatError(f"B-tree child is repeated or cyclic at {address}")
            if len(visited) >= max_nodes:
                raise UnsupportedFormat("B-tree traversal node limit reached")
            node = self.read_tree(address)
            if expected_level is not None and node.level != expected_level:
                raise FormatError(f"B-tree level mismatch at {address}")
            if lower is not None and node.entries[0].key != lower:
                raise FormatError(f"B-tree lower bound mismatch at {address}")
            if upper is not None and node.final_key != upper:
                raise FormatError(f"B-tree upper bound mismatch at {address}")
            visited[address] = node
            if node.level:
                for i, entry in reversed(tuple(enumerate(node.entries))):
                    end_key = (
                        node.entries[i + 1].key if i + 1 < len(node.entries) else node.final_key
                    )
                    if entry.address is None:
                        broken.append(
                            BrokenChild(node.address, i, entry.pointer_offset, entry.key, end_key)
                        )
                    else:
                        stack.append(
                            (entry.address, node.level - 1, entry.key, end_key)
                        )
        ordered_broken = tuple(sorted(broken, key=lambda b: (b.parent_address, b.entry_index)))
        return TreeWalk(tuple(visited.values()), ordered_broken)

    def find_missing_child_candidates(
        self, root_address: int, *, max_nodes: int = MAX_TREE_NODES
    ) -> tuple[MissingChildCandidate, ...]:
        """Find only gaps with two reachable neighbors pointing at one node.

        Both adjacent siblings must independently name the same missing node,
        which must point reciprocally at them, have the expected level and exact
        parent key boundaries. This provides attribution evidence, not byte
        integrity evidence. Other malformed links are rejected by walk_tree.
        """
        walk = self.walk_tree(root_address, max_nodes=max_nodes)
        nodes = {node.address: node for node in walk.nodes}
        candidates: list[MissingChildCandidate] = []
        for gap in walk.broken_links:
            parent = nodes[gap.parent_address]
            i = gap.entry_index
            if i == 0 or i + 1 >= len(parent.entries):
                continue  # The initial case requires interior two-sided anchors.
            left_addr, right_addr = parent.entries[i - 1].address, parent.entries[i + 1].address
            if left_addr is None or right_addr is None:
                continue
            left, right = nodes[left_addr], nodes[right_addr]
            target = left.right_sibling
            if target is None or target != right.left_sibling or target in nodes:
                continue
            node = self.read_tree(target)
            if (
                node.level != parent.level - 1
                or node.left_sibling != left_addr
                or node.right_sibling != right_addr
                or node.entries[0].key != gap.key_start
                or node.final_key != gap.key_end
            ):
                continue
            candidates.append(MissingChildCandidate(parent.address, i, node, left_addr, right_addr))
        return tuple(candidates)
