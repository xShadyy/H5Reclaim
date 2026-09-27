"""Conservative recovery for a single anchored v1 raw-data B-tree case."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import h5py
import numpy as np

from .format import FormatError, H5File
from .metadata import DatasetSpec, UnsupportedCase, read_dataset_spec


VERSION = "0.3.2"
_WINDOWS_STAT = os.name == "nt"
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


class ChunkDecodeError(RecoveryError):
    """An indexed chunk could not be decoded or failed its own checksum."""


@dataclass(frozen=True)
class ChunkRecord:
    index: tuple[int, ...]
    coordinate: tuple[int, ...]
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
    source_identity: tuple[int, int, int, int, int]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _handle_matches_path(handle_info: os.stat_result, path_info: os.stat_result) -> bool:
    """Check fields comparable across descriptor and pathname stat APIs.

    Windows filesystems and Python versions can expose different identifiers
    or timestamp values through fstat and stat for the same file. The caller
    also compares each API with itself before and after copying, then hashes
    the pathname against the snapshot before accepting the result.
    """
    if handle_info.st_size != path_info.st_size:
        return False
    if not _WINDOWS_STAT and (
        handle_info.st_dev, handle_info.st_ino
    ) != (path_info.st_dev, path_info.st_ino):
        return False
    return True


def _verify_source(source: Path, identity: tuple[int, int, int, int, int], digest: str) -> None:
    """Reject a replaced or changed source, including changes during verification."""
    if _identity(source.stat()) != identity:
        raise RecoveryError("input identity or metadata changed during recovery")
    if sha256_file(source) != digest or _identity(source.stat()) != identity:
        raise RecoveryError("input bytes changed during recovery")


def _anchor_address(value: Any) -> int:
    return int(getattr(value, "address", value))


def _fletcher32(data: bytes) -> int:
    """HDF5 Fletcher32 over big-endian 16-bit words, including odd final byte.

    Fold at most 360 pairs at a time, matching HDF5's checksum implementation.
    The format used here has full, even-length float64 chunks. This helper
    also handles odd lengths so its behavior can be tested independently.
    """
    sum1 = sum2 = 0
    even_length = len(data) & ~1
    for group_start in range(0, even_length, 720):
        for position in range(group_start, min(group_start + 720, even_length), 2):
            sum1 += (data[position] << 8) | data[position + 1]
            sum2 += sum1
        sum1 = (sum1 & 0xFFFF) + (sum1 >> 16)
        sum2 = (sum2 & 0xFFFF) + (sum2 >> 16)
    if len(data) & 1:
        sum1 += data[-1] << 8
        sum2 += sum1
        sum1 = (sum1 & 0xFFFF) + (sum1 >> 16)
        sum2 = (sum2 & 0xFFFF) + (sum2 >> 16)
    sum1 = (sum1 & 0xFFFF) + (sum1 >> 16)
    sum2 = (sum2 & 0xFFFF) + (sum2 >> 16)
    return (sum2 << 16) | sum1


def _decode_chunk(raw: bytes, spec: DatasetSpec, filter_mask: int) -> bytes:
    """Reverse only the two declared built-in filters with an exact size cap."""
    if not spec.filters:
        if filter_mask != 0 or len(raw) != spec.chunk_bytes:
            raise ChunkDecodeError("unfiltered chunk has an unexpected size or filter mask")
        return raw
    if spec.filters != (3, 1) or filter_mask & ~0b10 or filter_mask & 0b01:
        raise ChunkDecodeError("unsupported filter mask or filter pipeline")
    expected = spec.chunk_bytes + 4  # Fletcher32 appends one checksum word.
    if filter_mask & 0b10:
        # The optional deflate filter was skipped for this chunk. The mandatory
        # Fletcher32 filter must still be present, even when compression fails.
        if len(raw) != expected:
            raise ChunkDecodeError("uncompressed filtered chunk has wrong size")
        decoded = raw
    else:
        # zlib.decompress() has no bounded output argument. Stop at one byte
        # beyond the one possible decoded size, and reject trailing streams.
        try:
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(raw, expected + 1)
            if len(decoded) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ChunkDecodeError("deflate output length or stream boundary is invalid")
        except zlib.error as exc:
            raise ChunkDecodeError("deflate stream is corrupt") from exc
    actual = _fletcher32(decoded[:-4])
    stored = int.from_bytes(decoded[-4:], "little")
    if actual != stored:
        raise ChunkDecodeError("Fletcher32 checksum mismatch")
    return decoded[:-4]


@contextmanager
def source_snapshot(
    source: Path,
) -> Iterator[tuple[Path, str, tuple[int, int, int, int, int], int]]:
    """Make one bounded private image for consistent HDF5 and raw-parser reads."""
    source = Path(source)
    if not source.is_file():
        raise RecoveryError(f"input is not a regular file: {source}")
    path_info = source.stat()
    with source.open("rb") as original:
        file_info = os.fstat(original.fileno())
        # Keep the exported identity in the pathname stat domain. Comparing
        # fstat and stat tuples directly rejects unchanged files on some
        # Windows installations because the APIs report different metadata.
        identity = _identity(path_info)
        if (
            not stat.S_ISREG(file_info.st_mode)
            or _identity(source.stat()) != identity
            or not _handle_matches_path(file_info, path_info)
        ):
            raise RecoveryError("input changed while it was being opened")
        if file_info.st_size > MAX_SOURCE_BYTES:
            raise UnsupportedCase(f"input exceeds the {MAX_SOURCE_BYTES}-byte limit")
        with tempfile.TemporaryDirectory(prefix="h5reclaim-source-") as directory:
            snapshot = Path(directory) / "source.h5"
            digest = hashlib.sha256()
            total = 0
            with snapshot.open("xb") as target:
                while block := original.read(1024 * 1024):
                    total += len(block)
                    if total > MAX_SOURCE_BYTES:
                        raise UnsupportedCase(f"input exceeds the {MAX_SOURCE_BYTES}-byte limit")
                    target.write(block)
                    digest.update(block)
            if total != file_info.st_size:
                raise RecoveryError("input size changed while making a read-only snapshot")
            if _identity(os.fstat(original.fileno())) != _identity(file_info):
                raise RecoveryError("opened input changed while making a read-only snapshot")
            if _identity(source.stat()) != identity:
                raise RecoveryError("input path changed while making a read-only snapshot")
            source_hash = digest.hexdigest()
            yield snapshot, source_hash, identity, total


def analyze(source: Path, dataset_path: str) -> Analysis:
    """Parse one stable private copy; never mix metadata and payload from path reopens."""
    source = Path(source)
    with source_snapshot(source) as (snapshot, source_hash, identity, size):
        analysis = _analyze_snapshot(
            snapshot, dataset_path, source, source_hash, identity, size
        )
        if sha256_file(snapshot) != source_hash:
            raise RecoveryError("private source snapshot changed during analysis")
        _verify_source(source, identity, source_hash)
        return analysis


def _analyze_snapshot(
    snapshot: Path,
    dataset_path: str,
    source: Path,
    before_hash: str,
    identity: tuple[int, int, int, int, int],
    size: int,
) -> Analysis:
    spec = read_dataset_spec(snapshot, dataset_path)
    grid = spec.chunk_grid
    if int(np.prod(grid)) > MAX_CHUNKS:
        raise UnsupportedCase(f"dataset exceeds the {MAX_CHUNKS}-chunk limit")

    records: list[ChunkRecord] = []
    unresolved: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    rank = len(spec.shape)
    element_size = np.dtype(spec.dtype).itemsize
    with H5File(snapshot) as reader:
        layout = reader.read_dataset_layout(spec.object_address, rank=rank)
        if layout.chunk_shape != spec.chunks or layout.element_size != element_size:
            raise FormatError("raw layout disagrees with selected dataset metadata")
        root = reader.read_tree(layout.root_address, rank=rank, element_size=element_size)
        if root.level != 1:
            raise UnsupportedCase("this release requires a level-one v1 B-tree root")
        walk = reader.walk_tree(
            layout.root_address, rank=rank, element_size=element_size, max_nodes=MAX_NODES
        )
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

        candidates = reader.find_missing_child_candidates(
            layout.root_address, rank=rank, element_size=element_size
        )
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

        coordinates: set[tuple[int, ...]] = set()
        ranges: list[tuple[int, int, tuple[int, ...]]] = []
        for leaf, route, evidence in leaves.values():
            for entry in leaf.entries:
                key = entry.key
                *coordinate, element_offset = key.offsets
                coordinate = tuple(coordinate)
                if (not spec.filters and (key.stored_size != spec.chunk_bytes or key.filter_mask != 0)) or (
                    spec.filters and (key.stored_size < 4 or key.stored_size > 1_048_576
                                      or key.filter_mask & ~0b10 or key.filter_mask & 0b01)
                ):
                    raise FormatError("chunk size or filter mask lies outside support")
                if element_offset != 0 or any(
                    start % chunk for start, chunk in zip(coordinate, spec.chunks)
                ):
                    raise FormatError("chunk key is not aligned to the dataset grid")
                if any(start >= length for start, length in zip(coordinate, spec.shape)):
                    raise FormatError("chunk key lies outside the selected dataset")
                if coordinate in coordinates:
                    raise FormatError("multiple accepted chunks claim the same coordinate")
                coordinates.add(coordinate)
                if entry.address is None:
                    raise FormatError("accepted leaf has an undefined payload address")
                raw = reader.read_at(entry.address, key.stored_size)
                absolute = reader.absolute(entry.address)
                ranges.append((absolute, absolute + len(raw), coordinate))
                try:
                    payload = _decode_chunk(raw, spec, key.filter_mask)
                except ChunkDecodeError as exc:
                    failed.append({
                        "coordinate": list(coordinate),
                        "chunk_index": [start // chunk for start, chunk in zip(coordinate, spec.chunks)],
                        "source_address": entry.address,
                        "reason": str(exc),
                    })
                    continue
                records.append(
                    ChunkRecord(
                        index=tuple(start // chunk for start, chunk in zip(coordinate, spec.chunks)),
                        coordinate=coordinate,
                        file_address=entry.address,
                        absolute_offset=absolute,
                        length=len(raw),
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
        for start, end, coordinate in ordered_ranges:
            for meta_start, meta_end, kind in reader.metadata_ranges:
                if start < meta_end and meta_start < end:
                    raise FormatError(
                        f"chunk {coordinate} overlaps parsed {kind} at byte {meta_start}"
                    )

        root_address = root.address
        reachable_leaves = sum(node.level == 0 for node in walk.nodes)

    status = np.full(grid, STATUS_CODES["allocation_unknown"], dtype="u1")
    for chunk in failed:
        status[tuple(chunk["chunk_index"])] = STATUS_CODES["decode_failed"]
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
            "integrity": "fletcher32_verified" if spec.filters else "not_checked_no_checksum",
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
            "sha256_after": before_hash,
        },
        "dataset": {
            "path": spec.path,
            "object_address": spec.object_address,
            "shape": list(spec.shape),
            "chunks": list(spec.chunks),
            "dtype": spec.dtype,
            "chunk_grid": list(grid),
            "filters": list(spec.filters),
            "attributes_copied": [name for name, _value in spec.attributes],
            "attributes_omitted": list(spec.omitted_attributes),
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
        "failed_chunks": failed,
        "mappings": mappings,
        "assumptions": [
            (
                "one selected dataset; fixed rank-one little-endian IEEE binary64; "
                "Fletcher32 then deflate"
                if spec.filters else
                "one selected dataset; fixed rank-two little-endian uint32; no filters"
            ),
            "v1 B-tree with level-one root; at most one broken root-to-leaf pointer",
            "detached leaf requires reciprocal sibling links and parent key range",
        ],
        "integrity_note": (
            "Structural placement is supported by the stated links and keys. "
            "Each exported chunk passed its stored Fletcher32 check; this detects some "
            "byte errors but does not establish historical authenticity or ownership."
            if spec.filters else
            "Structural placement is supported by the stated links and keys. "
            "Unfiltered payload bytes have no independent checksum here; "
            "historical measurement integrity is not established."
        ),
        "metadata_note": (
            "Only the selected dataset's values, shape, chunking, datatype, and listed "
            "primitive scalar attributes are exported. Other attributes, dimension scales, "
            "links, sibling objects, and scientific context are not preserved. This is not "
            "a replacement GWOSC file."
            if spec.filters else
            "Only the selected dataset's values, shape, chunking, and datatype are exported. "
            "Original attributes, dimension scales, links, sibling objects, and scientific "
            "context are not preserved."
        ),
    }
    return Analysis(spec, records, status, report, identity)


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
                        dtype=spec.dtype,
                        fillvalue=0,
                    )
                    for record in analysis.records:
                        # The supported chunks have no edge padding and the
                        # output dataset has no filters. Writing direct raw
                        # bytes preserves float NaN payloads and signed zero.
                        data.id.write_direct_chunk(record.coordinate, record.payload, filter_mask=0)
                    for name, value in spec.attributes:
                        data.attrs[name] = value
                    meta = handle.create_group("/_h5reclaim")
                    validity = meta.create_dataset("chunk_status", data=analysis.status, dtype="u1")
                    validity.attrs["codes_json"] = json.dumps(STATUS_CODES, sort_keys=True)
                    validity.attrs["axis_meaning"] = (
                        "chunk sample" if len(spec.shape) == 1 else "chunk row, chunk column"
                    )
                    meta.create_dataset(
                        "report_json", data=report_text, dtype=h5py.string_dtype(encoding="utf-8")
                    )
                    data.attrs["h5reclaim_chunk_status"] = "/_h5reclaim/chunk_status"
                    data.attrs["h5reclaim_complete"] = analysis.report["complete"]
                    data.attrs["h5reclaim_execution_state"] = "finished"
                    data.attrs["h5reclaim_integrity"] = (
                        "fletcher32_verified_per_recovered_chunk" if spec.filters
                        else "not_checked_no_checksum"
                    )
                    data.attrs["h5reclaim_warning"] = (
                        "Check chunk_status before using values; unallocated output chunks "
                        "read as fill zero but are not known measurements. The report lists copied "
                        "attributes; other scientific metadata and sibling objects are not preserved."
                        if spec.filters else
                        "Check chunk_status before using values; unallocated output chunks "
                        "read as fill zero but are not known measurements. Original scientific "
                        "metadata and sibling objects are not preserved."
                    )
                    meta.attrs["source_sha256"] = analysis.report["source"]["sha256_before"]
                    meta.attrs["report_schema_version"] = 1
                    handle.flush()

                report_temp.write_text(report_text, encoding="utf-8")
                _verify_source(
                    source,
                    analysis.source_identity,
                    analysis.report["source"]["sha256_before"],
                )
                _validate_paths(source, output, report_path)
                os.link(output_temp, output)
                published_output = True
                os.link(report_temp, report_path)
                return analysis.report
            except Exception:
                if published_output:
                    output.unlink(missing_ok=True)
                raise
