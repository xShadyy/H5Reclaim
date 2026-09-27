"""Make one verified broken-link copy for the narrow v1 B-tree experiment.

The healthy file and this tool's manifest are benchmark materials. Neither is a
recovery input. The output is a disposable damaged copy, never an in-place edit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import H5File


MAX_FIXTURE_BYTES = 16 * 1024 * 1024
MAX_CHUNKS = 16_384


def _digest(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _check_targets(source: Path, output: Path, manifest: Path) -> None:
    paths = (source.resolve(), output.resolve(), manifest.resolve())
    if len(set(paths)) != 3:
        raise ValueError("source, damaged output, and manifest must be distinct paths")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"damaged output already exists: {output}")
    if manifest.exists() or manifest.is_symlink():
        raise FileExistsError(f"manifest already exists: {manifest}")


def _inspect_healthy(source: Path, dataset_path: str, child_index: int | None = None) -> dict:
    """Cross-check a selected object's actual tree with HDF5's healthy index."""
    with h5py.File(source, "r") as handle, H5File(source) as raw:
        dataset = handle[dataset_path]
        if not isinstance(dataset, h5py.Dataset):
            raise ValueError("selected path does not refer to a dataset")
        if dataset.ndim != 2 or dataset.dtype.str != "<u4":
            raise ValueError("requires a rank-two little-endian uint32 dataset")
        if dataset.chunks is None or dataset.maxshape != dataset.shape:
            raise ValueError("requires a fixed-size chunked dataset")
        if dataset.id.get_create_plist().get_nfilters() != 0:
            raise ValueError("requires an unfiltered dataset")
        shape, chunks = dataset.shape, dataset.chunks
        if any(s % c for s, c in zip(shape, chunks)):
            raise ValueError("requires dimensions divisible by chunk dimensions")
        expected_count = (shape[0] // chunks[0]) * (shape[1] // chunks[1])
        if expected_count < 3 or expected_count > MAX_CHUNKS:
            raise ValueError("chunk count outside supported fixture limit")
        if dataset.id.get_num_chunks() != expected_count:
            raise ValueError("requires every chunk to be allocated")

        # h5py identifies the selected dataset object, not an arbitrary TREE
        # signature found in the file. The raw layout message owns the root.
        object_address = h5py.h5o.get_info(dataset.id).addr
        layout = raw.read_dataset_layout(object_address)
        if layout.message_version != 3:
            raise ValueError("requires a version-three chunked layout message")
        if layout.chunk_shape != chunks or layout.element_size != 4:
            raise ValueError("layout message conflicts with the selected dataset")
        if raw.superblock.base_address != 0:
            raise ValueError("fixture tool requires a zero-base HDF5 file")
        root = raw.read_tree(layout.root_address)
        if root.level != 1 or len(root.entries) < 3:
            raise ValueError("requires a level-one root with at least three leaves")
        tree = raw.walk_tree(layout.root_address)
        if tree.broken_links or len(tree.nodes) != len(root.entries) + 1:
            raise ValueError("healthy index contains a broken link or unexpected node count")
        if any(entry.address is None for entry in root.entries):
            raise ValueError("healthy root contains an undefined child address")
        leaves = [raw.read_tree(entry.address) for entry in root.entries]
        if any(leaf.level != 0 for leaf in leaves):
            raise ValueError("root child is not a level-zero leaf")

        raw_chunks = {}
        for leaf in leaves:
            for entry in leaf.entries:
                key = entry.key
                coordinate = key.offsets[:2]
                if (
                    key.offsets[2] != 0
                    or entry.address is None
                    or key.filter_mask != 0
                    or key.stored_size != chunks[0] * chunks[1] * 4
                    or any(c < 0 or c >= s or c % size for c, s, size in zip(coordinate, shape, chunks))
                    or coordinate in raw_chunks
                ):
                    raise ValueError("invalid or duplicate raw chunk entry")
                raw_chunks[coordinate] = (raw.absolute(entry.address), key)
        if len(raw_chunks) != expected_count:
            raise ValueError("raw tree does not account for every allocated chunk")
        payloads = sorted(address for address, _ in raw_chunks.values())
        payload_size = chunks[0] * chunks[1] * 4
        if any(right - left < payload_size for left, right in zip(payloads, payloads[1:])):
            raise ValueError("two indexed chunks share overlapping payload bytes")

        library_chunks = {}
        for index in range(expected_count):
            info = dataset.id.get_chunk_info(index)
            if info.chunk_offset in library_chunks:
                raise ValueError("HDF5 reported a duplicate chunk")
            library_chunks[info.chunk_offset] = info
        if raw_chunks.keys() != library_chunks.keys():
            raise ValueError("raw tree and HDF5 disagree about chunk coordinates")
        for coordinate, (address, key) in raw_chunks.items():
            info = library_chunks[coordinate]
            if (
                info.byte_offset != address
                or info.size != key.stored_size
                or info.filter_mask != key.filter_mask
            ):
                raise ValueError(f"raw tree and HDF5 disagree about chunk {coordinate}")

        # Select an interior leaf with reciprocal siblings. Both adjacent
        # leaves remain reachable after this parent's pointer is broken.
        eligible = []
        for index in range(1, len(leaves) - 1):
            left, leaf, right = leaves[index - 1 : index + 2]
            if (
                leaf.entries
                and leaf.left_sibling == left.address
                and leaf.right_sibling == right.address
                and left.right_sibling == leaf.address
                and right.left_sibling == leaf.address
            ):
                eligible.append(index)
        if not eligible:
            raise ValueError("no interior leaf has reciprocal surviving siblings")
        if child_index is not None and child_index not in eligible:
            raise ValueError(
                f"child index {child_index} is not an eligible interior leaf; "
                f"eligible indices: {eligible}"
            )
        chosen = eligible[0] if child_index is None else child_index
        leaf = leaves[chosen]
        pointer = root.entries[chosen]
        return {
            "dataset": dataset_path,
            "shape": shape,
            "chunk_shape": chunks,
            "object_address": object_address,
            "root_address": root.address,
            "root_level": root.level,
            "leaf_address": leaf.address,
            "left_leaf_address": leaves[chosen - 1].address,
            "right_leaf_address": leaves[chosen + 1].address,
            "child_index": chosen,
            "pointer_offset": pointer.pointer_offset,
            "pointer_size": raw.superblock.offset_size,
            "pointer_value": pointer.address,
            "affected_offsets": [entry.key.offsets[:2] for entry in leaf.entries],
            "unaffected_offset": leaves[chosen - 1].entries[0].key.offsets[:2],
            "chunk_count": expected_count,
        }


def _observed_damage(clean: Path, damaged: Path, details: dict) -> dict:
    """Require broken affected reads and exact unaffected reads at every chunk."""
    row_size, col_size = details["chunk_shape"]

    def read_chunk(dataset, coordinates):
        row, col = coordinates
        return dataset[row : row + row_size, col : col + col_size]

    altered, unreadable, unchanged = 0, 0, 0
    affected = {tuple(coordinate) for coordinate in details["affected_offsets"]}
    with h5py.File(clean, "r") as good, h5py.File(damaged, "r") as bad:
        expected = good[details["dataset"]]
        actual = bad[details["dataset"]]
        for row in range(0, details["shape"][0], row_size):
            for col in range(0, details["shape"][1], col_size):
                coordinate = (row, col)
                if coordinate in affected:
                    try:
                        result = read_chunk(actual, coordinate)
                    except (OSError, RuntimeError, ValueError):
                        unreadable += 1
                    else:
                        if not np.array_equal(result, read_chunk(expected, coordinate)):
                            altered += 1
                        else:
                            unchanged += 1
                else:
                    np.testing.assert_array_equal(
                        read_chunk(actual, coordinate), read_chunk(expected, coordinate)
                    )
    if altered + unreadable == 0:
        raise ValueError("damaged copy still reads the affected region exactly")
    if unchanged:
        raise ValueError("some affected chunks remain fully readable after mutation")
    return {
        "affected_wrong_values": altered,
        "affected_read_errors": unreadable,
        "unaffected_exact_chunks": details["chunk_count"] - len(affected),
    }


def make_damage(
    source: Path, output: Path, manifest: Path, dataset_path: str,
    child_index: int | None = None,
) -> dict:
    """Validate, damage a temporary copy, observe failure, then publish it."""
    _check_targets(source, output, manifest)
    source_size = source.stat().st_size
    if source_size > MAX_FIXTURE_BYTES:
        raise ValueError("input exceeds 16 MiB controlled-fixture limit")
    before = source.read_bytes()
    details = _inspect_healthy(source, dataset_path, child_index=child_index)
    pointer_at = details["pointer_offset"]
    pointer_size = details["pointer_size"]
    original_pointer = details["pointer_value"].to_bytes(pointer_size, "little")
    if before[pointer_at : pointer_at + pointer_size] != original_pointer:
        raise ValueError("pointer bytes disagree with verified parent child entry")
    invalid_pointer = b"\xff" * pointer_size
    if original_pointer == invalid_pointer:
        raise ValueError("selected pointer was already undefined")
    changed = bytearray(before)
    changed[pointer_at : pointer_at + pointer_size] = invalid_pointer
    if len(changed) != len(before) or any(
        before[i] != changed[i]
        for i in range(len(before))
        if i < pointer_at or i >= pointer_at + pointer_size
    ):
        raise AssertionError("mutation escaped the selected pointer field")
    if source.read_bytes() != before:
        raise ValueError("source changed during validation")

    output.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".h5reclaim-damage-", suffix=".h5", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(changed)
        observation = _observed_damage(source, temporary, details)
        if source.read_bytes() != before:
            raise ValueError("source changed during read validation")
        result = {
            "experiment": "single internal child pointer made undefined",
            "source_sha256": _digest(before),
            "damaged_sha256": _digest(changed),
            "source_size": len(before),
            "dataset": details["dataset"],
            "shape": details["shape"],
            "chunk_shape": details["chunk_shape"],
            "chunk_count": details["chunk_count"],
            "dataset_object_address": details["object_address"],
            "btree_root_address": details["root_address"],
            "btree_root_level": details["root_level"],
            "parent_child_index": details["child_index"],
            "affected_leaf_address": details["leaf_address"],
            "left_leaf_address": details["left_leaf_address"],
            "right_leaf_address": details["right_leaf_address"],
            "pointer_absolute_byte_offset": pointer_at,
            "pointer_before_hex": original_pointer.hex(),
            "pointer_after_hex": invalid_pointer.hex(),
            "modified_byte_offsets": [
                i for i in range(pointer_at, pointer_at + pointer_size) if before[i] != changed[i]
            ],
            "affected_chunk_offsets": details["affected_offsets"],
            "unaffected_check_offset": details["unaffected_offset"],
            "standard_reader_observation": observation,
            "h5py_version": h5py.__version__,
            "hdf5_version": h5py.version.hdf5_version,
        }
        # link() will refuse an existing destination even if it appeared
        # since the initial path check. The temporary file is same-filesystem.
        os.link(temporary, output)
        manifest_created = False
        try:
            with manifest.open("x", encoding="utf-8") as target:
                manifest_created = True
                json.dump(result, target, indent=2)
                target.write("\n")
        except BaseException:
            output.unlink()
            if manifest_created:
                manifest.unlink()
            raise
        return result
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="pristine HDF5 fixture")
    parser.add_argument("--output", required=True, type=Path, help="new disposable damaged copy")
    parser.add_argument("--manifest", required=True, type=Path, help="benchmark truth kept outside recovery input")
    parser.add_argument("--dataset", default="/measurements", help="selected dataset path")
    parser.add_argument(
        "--child-index", type=int,
        help="verified interior root child slot to damage (default: first eligible slot)",
    )
    args = parser.parse_args()
    result = make_damage(
        args.input, args.output, args.manifest, args.dataset,
        child_index=args.child_index,
    )
    print(f"damaged copy: {args.output}")
    print(f"benchmark manifest: {args.manifest}")
    print(f"source SHA-256: {result['source_sha256']}")
    print(f"damaged SHA-256: {result['damaged_sha256']}")
    print(f"affected chunks: {len(result['affected_chunk_offsets'])}")
    print(f"standard reader: {result['standard_reader_observation']}")


if __name__ == "__main__":
    main()
