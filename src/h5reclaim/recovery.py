"""Conservative recovery for a single anchored v1 raw-data B-tree case."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .format import FormatError, H5File
from .metadata import DatasetSpec, UnsupportedCase, read_dataset_spec


VERSION = "0.1.0"
STATUS_CODES = {
    "recovered": 1,
    "allocation_unknown": 2,
    "ambiguous": 3,
    "unavailable": 4,
    "unsupported": 5,
    "decode_failed": 6,
}
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_NODES = 4096
MAX_CHUNKS = 4096


class RecoveryError(ValueError):
    """Recovery cannot safely produce the requested result."""


@dataclass(frozen=True)
class ChunkRecord:
    index: tuple[int, int]
    coordinate: tuple[int, int]
    file_address: int
    absolute_offset: int
    length: int
    leaf_address: int
    route: str
    evidence: dict[str, Any]
    payload: bytes


@dataclass
class Analysis:
    spec: DatasetSpec
    records: list[ChunkRecord]
    status: np.ndarray
    report: dict[str, Any]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _anchor_address(value: Any) -> int:
    return int(getattr(value, "address", value))


def analyze(source: Path, dataset_path: str) -> Analysis:
    """Analyze a damaged file without accessing fixture truth or writing to it."""

    source = Path(source)
    if not source.is_file():
        raise RecoveryError(f"input is not a regular file: {source}")
    size = source.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise UnsupportedCase(f"input exceeds the {MAX_SOURCE_BYTES}-byte limit")
    before_hash = sha256_file(source)
    spec = read_dataset_spec(source, dataset_path)
    grid = spec.chunk_grid
    if grid[0] * grid[1] > MAX_CHUNKS:
        raise UnsupportedCase(f"dataset exceeds the {MAX_CHUNKS}-chunk limit")

    records: list[ChunkRecord] = []
    unresolved: list[dict[str, Any]] = []
    with H5File(source) as reader:
        layout = reader.read_dataset_layout(spec.object_address)
        if layout.chunk_shape != spec.chunks or layout.element_size != 4:
            raise FormatError("raw layout disagrees with selected dataset metadata")
        root = reader.read_tree(layout.root_address)
        if root.level != 1:
            raise UnsupportedCase("this release requires a level-one v1 B-tree root")
        walk = reader.walk_tree(layout.root_address, max_nodes=MAX_NODES)
        if len(walk.broken_links) > 1:
            raise UnsupportedCase("this release handles at most one broken child link")
        for broken in walk.broken_links:
            if broken.parent_address != root.address:
                raise UnsupportedCase("only a broken root-to-leaf link is supported")

        root_entries = {entry.address: i for i, entry in enumerate(root.entries) if entry.address is not None}
        leaves: dict[int, tuple[Any, str, dict[str, Any]]] = {}
        for node in walk.nodes:
            if node.address == root.address:
                continue
            if node.level != 0 or node.address not in root_entries:
                raise FormatError("unexpected reachable B-tree node or level")
            if node.address in leaves:
                raise FormatError("duplicate reachable leaf")
            leaves[node.address] = (
                node,
                "intact_tree",
                {"parent_address": root.address, "parent_slot": root_entries[node.address]},
            )

        candidates = reader.find_missing_child_candidates(layout.root_address)
        for broken in walk.broken_links:
            matching = [
                candidate
                for candidate in candidates
                if candidate.parent_address == broken.parent_address
                and candidate.entry_index == broken.entry_index
            ]
            if len(matching) == 1:
                candidate = matching[0]
                node = candidate.node
                if node.address in leaves:
                    raise FormatError("candidate is already reachable from the root")
                leaves[node.address] = (
                    node,
                    "reconstructed_link",
                    {
                        "parent_address": broken.parent_address,
                        "parent_slot": broken.entry_index,
                        "left_anchor": _anchor_address(candidate.left_anchor),
                        "right_anchor": _anchor_address(candidate.right_anchor),
                        "rule": "parent interval and reciprocal links through both reachable siblings",
                    },
                )
            else:
                unresolved.append(
                    {
                        "parent_address": broken.parent_address,
                        "parent_slot": broken.entry_index,
                        "reason": "no unique two-sided anchored candidate",
                        "candidate_count": len(matching),
                    }
                )

        coordinates: set[tuple[int, int]] = set()
        ranges: list[tuple[int, int, tuple[int, int]]] = []
        for leaf, route, evidence in leaves.values():
            for entry in leaf.entries:
                key = entry.key
                row, col, element_offset = key.offsets
                if key.stored_size != spec.chunk_bytes or key.filter_mask != 0:
                    raise FormatError("chunk size or filter mask lies outside support")
                if element_offset != 0 or row % spec.chunks[0] or col % spec.chunks[1]:
                    raise FormatError("chunk key is not aligned to the dataset grid")
                if row >= spec.shape[0] or col >= spec.shape[1]:
                    raise FormatError("chunk key lies outside the selected dataset")
                coordinate = (row, col)
                if coordinate in coordinates:
                    raise FormatError("multiple accepted chunks claim the same coordinate")
                coordinates.add(coordinate)
                if entry.address is None:
                    raise FormatError("accepted leaf has an undefined payload address")
                payload = reader.read_at(entry.address, key.stored_size)
                absolute = reader.absolute(entry.address)
                ranges.append((absolute, absolute + len(payload), coordinate))
                records.append(
                    ChunkRecord(
                        index=(row // spec.chunks[0], col // spec.chunks[1]),
                        coordinate=coordinate,
                        file_address=entry.address,
                        absolute_offset=absolute,
                        length=len(payload),
                        leaf_address=leaf.address,
                        route=route,
                        evidence=evidence,
                        payload=payload,
                    )
                )

        ordered_ranges = sorted(ranges)
        for previous, current in zip(ordered_ranges, ordered_ranges[1:]):
            if previous[1] > current[0]:
                raise FormatError("accepted chunk payload ranges overlap")

        root_address = root.address
        reachable_leaves = sum(node.level == 0 for node in walk.nodes)

    after_hash = sha256_file(source)
    if after_hash != before_hash or source.stat().st_size != size:
        raise RecoveryError("input changed during analysis; no result can be trusted")

    status = np.full(grid, STATUS_CODES["allocation_unknown"], dtype="u1")
    for record in records:
        status[record.index] = STATUS_CODES["recovered"]
    counts = {name: int(np.count_nonzero(status == code)) for name, code in STATUS_CODES.items()}
    complete = bool(np.all(status == STATUS_CODES["recovered"]))
    mappings = [
        {
            "coordinate": list(record.coordinate),
            "chunk_index": list(record.index),
            "source_address": record.file_address,
            "source_absolute_offset": record.absolute_offset,
            "size_bytes": record.length,
            "leaf_address": record.leaf_address,
            "route": record.route,
            "evidence": record.evidence,
            "integrity": "not_checked_no_checksum",
        }
        for record in sorted(records, key=lambda item: item.index)
    ]
    report = {
        "schema_version": 1,
        "tool": "h5reclaim",
        "tool_version": VERSION,
        "execution_state": "finished",
        "outcome": "complete" if complete else "partial",
        "complete": complete,
        "source": {
            "path": str(source),
            "size_bytes": size,
            "sha256_before": before_hash,
            "sha256_after": after_hash,
        },
        "dataset": {
            "path": spec.path,
            "object_address": spec.object_address,
            "shape": list(spec.shape),
            "chunks": list(spec.chunks),
            "dtype": "<u4",
            "chunk_grid": list(grid),
        },
        "index": {
            "type": "v1_raw_data_btree",
            "root_address": root_address,
            "root_level": 1,
            "reachable_leaves": reachable_leaves,
            "broken_links": len(unresolved) + sum(
                leaf[1] == "reconstructed_link" for leaf in leaves.values()
            ),
        },
        "counts": counts,
        "reconstructed_chunks": sum(record.route == "reconstructed_link" for record in records),
        "unresolved_links": unresolved,
        "mappings": mappings,
        "assumptions": [
            "one selected dataset; fixed rank-two little-endian uint32; no filters",
            "v1 B-tree with level-one root; at most one broken root-to-leaf pointer",
            "detached leaf requires reciprocal sibling links and parent key range",
        ],
        "integrity_note": (
            "Structural placement is supported by the stated links and keys. "
            "Unfiltered payload bytes have no independent checksum here; "
            "historical measurement integrity is not established."
        ),
    }
    return Analysis(spec, records, status, report)


def _validate_paths(source: Path, output: Path, report: Path) -> None:
    source = source.resolve(strict=True)
    for target in (output, report):
        if target.exists() or target.is_symlink():
            raise RecoveryError(f"destination already exists: {target}")
        if target.resolve(strict=False) == source:
            raise RecoveryError("destination aliases the input")
        if not target.parent.is_dir():
            raise RecoveryError(f"destination directory does not exist: {target.parent}")
    if output.resolve(strict=False) == report.resolve(strict=False):
        raise RecoveryError("output and report paths must differ")


def recover(source: Path, dataset_path: str, output: Path, report_path: Path) -> dict[str, Any]:
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    analysis = analyze(source, dataset_path)
    report_text = json.dumps(analysis.report, indent=2, sort_keys=True) + "\n"
    # Private temporary directories keep HDF5's pathname reopen away from
    # other users of a shared output directory. The context managers clean up
    # the first directory even if creation of the second one fails.
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=output.parent) as out_dir:
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=report_path.parent) as rep_dir:
            output_temp = Path(out_dir) / "output.h5"
            report_temp = Path(rep_dir) / "report.json"
            published_output = False
            try:
                with h5py.File(output_temp, "x") as handle:
                    spec = analysis.spec
                    data = handle.create_dataset(
                        spec.path,
                        shape=spec.shape,
                        chunks=spec.chunks,
                        dtype="<u4",
                        fillvalue=0,
                    )
                    for record in analysis.records:
                        row, col = record.coordinate
                        tile = np.frombuffer(record.payload, dtype="<u4").reshape(spec.chunks)
                        data[row : row + spec.chunks[0], col : col + spec.chunks[1]] = tile
                    meta = handle.create_group("/_h5reclaim")
                    validity = meta.create_dataset("chunk_status", data=analysis.status, dtype="u1")
                    validity.attrs["codes_json"] = json.dumps(STATUS_CODES, sort_keys=True)
                    validity.attrs["axis_meaning"] = "chunk row, chunk column"
                    meta.create_dataset(
                        "report_json", data=report_text, dtype=h5py.string_dtype(encoding="utf-8")
                    )
                    data.attrs["h5reclaim_chunk_status"] = "/_h5reclaim/chunk_status"
                    data.attrs["h5reclaim_complete"] = analysis.report["complete"]
                    data.attrs["h5reclaim_execution_state"] = "finished"
                    data.attrs["h5reclaim_integrity"] = "not_checked_no_checksum"
                    data.attrs["h5reclaim_warning"] = (
                        "Check chunk_status before using values; unallocated output chunks "
                        "read as fill zero but are not known measurements."
                    )
                    meta.attrs["source_sha256"] = analysis.report["source"]["sha256_before"]
                    meta.attrs["report_schema_version"] = 1
                    handle.flush()

                report_temp.write_text(report_text, encoding="utf-8")
                if sha256_file(source) != analysis.report["source"]["sha256_before"]:
                    raise RecoveryError("input changed before output publication")
                _validate_paths(source, output, report_path)
                os.link(output_temp, output)
                published_output = True
                os.link(report_temp, report_path)
                return analysis.report
            except Exception:
                if published_output:
                    output.unlink(missing_ok=True)
                raise
