"""Rooted, read-only recovery of compact and contiguous numeric datasets.

Only a selected local hard-link path can assign a raw byte range to a dataset.
The rooted group path, dataspace, canonical datatype, and storage-layout message
must survive; hints and file-wide byte signatures never substitute for them.
For a physically truncated contiguous allocation, only complete surviving
elements at their declared row-major positions are exported as measurements.

Format reference: HDF5 File Format Specification, Version 4.0, sections II.A,
IV.A.1, IV.A.3.b-d and IV.A.3.q (compact/contiguous layout classes).
https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from math import prod
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .format import DEFAULT_ISTORE_K, SIGNATURE, FormatError, H5File, Superblock, UnsupportedFormat
from .metadata_fallback import (
    MAX_DEPTH, MAX_PATH_BYTES, _compact_links, _messages, _numeric_dtype,
    _old_group, _old_messages, _old_root_address, _unique,
    _validate_metadata_ranges,
)
from .modern_indexes import ModernH5File, ModernSuperblock, lookup3
from .hints import DatasetHints, HintsError, compare_hints, require_no_conflicts


MAX_ELEMENTS = 1_048_576
MAX_DATA_BYTES = 8 * MAX_ELEMENTS
MAX_OWNERSHIP_OBJECTS = 8192
MAX_OWNERSHIP_LINKS = 32768
STATUS_CODES = {"unknown": 0, "recovered": 1}


def _uint(data: bytes) -> int:
    return int.from_bytes(data, "little")


class _TruncatedOldReader(H5File):
    """Keep declared EOF for validation while bounding all reads by physical size.

    The regular parser rejects a physical truncation at open time. The only
    difference here is accepting a declared EOF past the last physical byte.
    Every inherited metadata read is still bounded by the actual file size.
    """

    def absolute(self, address: int) -> int:
        if (not isinstance(address, int) or address < 0
            or address == self.superblock.undefined_address):
            raise FormatError("undefined or invalid older relative address")
        offset = self.superblock.base_address + address
        if offset >= self.superblock.eof_address:
            raise FormatError("older relative address exceeds declared EOF")
        # A link to an unrelated object beyond physical EOF is still a
        # syntactically valid pointer. read_at() enforces physical bounds if
        # this particular object is needed by the selected hard-link path.
        return offset

    def _parse_superblock(self) -> Superblock:
        position = 0
        while position < self.size:
            if self.size - position >= 8 and self._read_absolute(position, 8) == SIGNATURE:
                break
            position = 512 if position == 0 else position * 2
        else:
            raise FormatError("HDF5 signature not found at a permitted offset")
        prefix = self._read_absolute(position, 24)
        version = prefix[8]
        if version not in (0, 1):
            raise UnsupportedFormat("older rooted parser requires a v0/v1 superblock")
        osize, lsize = prefix[13], prefix[14]
        if osize not in (2, 4, 8) or lsize not in (2, 4, 8):
            raise UnsupportedFormat("unsupported older superblock address/length width")
        if any(prefix[index] for index in (9, 10, 11, 12, 15)):
            raise UnsupportedFormat("unexpected older superblock versions or reserved bits")
        if not _uint(prefix[16:18]) or not _uint(prefix[18:20]):
            raise FormatError("invalid older group B-tree K")
        width = 28 if version == 1 else 24
        raw = self._read_absolute(position, width + 4 * osize)
        istore_k = _uint(raw[24:26]) if version == 1 else DEFAULT_ISTORE_K
        if not 0 < istore_k <= 32767 or (version == 1 and raw[26:28] != b"\x00\x00"):
            raise FormatError("invalid indexed-storage B-tree K")
        base = _uint(raw[width:width + osize])
        eof = _uint(raw[width + 2 * osize:width + 3 * osize])
        driver = _uint(raw[width + 3 * osize:width + 4 * osize])
        if base != position or driver != (1 << (8 * osize)) - 1:
            raise UnsupportedFormat("relocated base or non-default file driver")
        if eof <= position + width + 4 * osize:
            raise FormatError("older superblock end-of-file is inconsistent with source")
        return Superblock(version, position, base, osize, lsize, eof, istore_k)


class _TruncatedModernReader(ModernH5File):
    """Validate the original superblock checksum and allow a missing physical tail."""

    def absolute(self, address: int) -> int:
        if (not isinstance(address, int) or address < 0
            or address == self.superblock.undefined_address):
            raise FormatError("undefined or invalid modern relative address")
        offset = self.superblock.base_address + address
        if offset >= self.superblock.eof_address:
            raise FormatError("modern relative address exceeds declared EOF")
        return offset

    def _parse_superblock(self) -> ModernSuperblock:
        position = 0
        while position < self.size:
            if self.size - position >= 8 and self._read_absolute(position, 8) == SIGNATURE:
                break
            position = 512 if position == 0 else position * 2
        else:
            raise FormatError("HDF5 signature not found at a permitted offset")
        prefix = self._read_absolute(position, 12)
        version, osize, lsize, flags = prefix[8:12]
        if version not in (2, 3) or osize not in (2, 4, 8) or lsize not in (2, 4, 8):
            raise UnsupportedFormat("unsupported modern superblock version or width")
        if version == 3 and flags:
            raise UnsupportedFormat("write-access/SWMR or unknown consistency flags")
        width = 16 + 4 * osize
        raw = self._read_absolute(position, width)
        if lookup3(raw[:-4]) != _uint(raw[-4:]):
            raise FormatError("modern superblock checksum mismatch")
        base = _uint(raw[12:12 + osize])
        extension = _uint(raw[12 + osize:12 + 2 * osize])
        eof = _uint(raw[12 + 2 * osize:12 + 3 * osize])
        root = _uint(raw[12 + 3 * osize:12 + 4 * osize])
        undefined = (1 << (8 * osize)) - 1
        if base != position:
            raise UnsupportedFormat("relocated modern superblock base")
        if eof <= position + width:
            raise FormatError("modern superblock end-of-file is inconsistent with source")
        if root == undefined or base + root >= eof:
            raise FormatError("invalid modern root group address")
        if extension != undefined and base + extension >= eof:
            raise FormatError("invalid modern superblock extension address")
        return ModernSuperblock(version, position, base, osize, lsize, eof, root)


@dataclass(frozen=True)
class NonchunkedSpec:
    path: str
    object_address: int
    shape: tuple[int, ...]
    dtype: str
    layout: str
    source_address: int | None
    source_absolute_offset: int | None
    stored_size: int
    metadata_route: str
    root_address: int
    link_chain: tuple[dict[str, Any], ...]
    metadata_ranges: tuple[tuple[int, int, str], ...]
    omitted_auxiliary_metadata: tuple[str, ...]
    truncated_source: bool
    uninspected_rooted_objects: int

    @property
    def element_size(self) -> int:
        return np.dtype(self.dtype).itemsize

    @property
    def elements(self) -> int:
        return prod(self.shape)


@dataclass(frozen=True)
class NonchunkedAnalysis:
    spec: NonchunkedSpec
    status: np.ndarray
    recovered_bytes: bytes
    report: dict[str, Any]
    source_identity: tuple[int, int, int, int, int]


def _dataspace(raw: bytes, lsize: int, *, older: bool) -> tuple[int, ...]:
    if len(raw) < 4:
        raise FormatError("truncated dataspace message")
    version, rank, flags, kind = raw[:4]
    if rank > 4 or flags & ~1:
        raise UnsupportedFormat("nonchunked route requires rank zero through four simple dataspace")
    if version == 1:
        if len(raw) < 8 or any(raw[3:8]):
            raise FormatError("invalid older dataspace prefix")
        pos = 8
    elif version == 2:
        if kind != (0 if rank == 0 else 1):
            raise UnsupportedFormat("null or contradictory dataspace")
        pos = 4
    else:
        raise UnsupportedFormat("unsupported dataspace message version")
    length = pos + rank * lsize * (2 if flags & 1 else 1)
    if len(raw) != length and not (older and len(raw) == (length + 7) // 8 * 8
                                    and not any(raw[length:])):
        raise FormatError("dataspace message length or padding disagrees with rank")
    shape = tuple(_uint(raw[pos + i*lsize:pos + (i+1)*lsize]) for i in range(rank))
    pos += rank*lsize
    maximum = (tuple(_uint(raw[pos + i*lsize:pos + (i+1)*lsize]) for i in range(rank))
               if flags & 1 else shape)
    if (any(dim <= 0 for dim in shape) or prod(shape) > MAX_ELEMENTS
        or any(limit < dim for limit, dim in zip(maximum, shape))):
        raise UnsupportedFormat("nonchunked extents exceed bounded positive range")
    # Compact/contiguous storage cannot grow independently of the recorded
    # allocated shape. Reject a different maximum rather than infer contents.
    if maximum != shape:
        raise UnsupportedFormat("growing compact/contiguous dataset is unsupported")
    return shape


def _datatype(raw: bytes, *, older: bool) -> str:
    if older:
        candidates = [n for n in (12, 20) if len(raw) == (n + 7) // 8 * 8
                      and not any(raw[n:])]
        if len(candidates) != 1:
            raise UnsupportedFormat("noncanonical or unbounded older datatype message")
        raw = raw[:candidates[0]]
    return _numeric_dtype(raw)


def _layout(raw: bytes, *, older: bool, osize: int, lsize: int,
            expected_size: int, base: int, declared_eof: int,
            message_offset: int) -> tuple[str, int | None, int | None, int]:
    if len(raw) < 2 or raw[0] not in (3, 4, 5):
        raise UnsupportedFormat("nonchunked route requires a version 3/4/5 layout")
    if raw[1] == 0:
        if len(raw) < 4:
            raise FormatError("truncated compact layout")
        stored = _uint(raw[2:4])
        actual = 4 + stored
        if len(raw) != actual and not (older and len(raw) == (actual + 7) // 8 * 8
                                       and not any(raw[actual:])):
            raise FormatError("compact layout length or padding is contradictory")
        if stored != expected_size:
            raise FormatError("compact payload length contradicts dataspace and datatype")
        absolute = message_offset + 4
        if absolute + stored > declared_eof:
            raise FormatError("compact payload crosses declared EOF")
        return "compact", None, absolute, stored
    if raw[1] == 1:
        actual = 2 + osize + lsize
        if len(raw) != actual and not (older and len(raw) == (actual + 7) // 8 * 8
                                       and not any(raw[actual:])):
            raise FormatError("contiguous layout length or padding is contradictory")
        address = _uint(raw[2:2 + osize])
        stored = _uint(raw[2 + osize:actual])
        if stored != expected_size:
            raise FormatError("contiguous allocation length contradicts dataspace and datatype")
        if address == (1 << (8 * osize)) - 1:
            return "contiguous", None, None, stored
        absolute = base + address
        if absolute >= declared_eof or absolute + stored > declared_eof:
            raise FormatError("contiguous allocation crosses declared EOF")
        return "contiguous", address, absolute, stored
    raise UnsupportedFormat("chunked or virtual layout is not a nonchunked numeric dataset")


def _check_path(dataset_path: str) -> list[str]:
    if (not isinstance(dataset_path, str) or not dataset_path.startswith("/")
        or dataset_path in ("/", "/_h5reclaim")
        or dataset_path.startswith("/_h5reclaim/")):
        raise UnsupportedFormat("select an absolute local dataset path")
    try:
        encoded = dataset_path.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise UnsupportedFormat("selected path is not valid UTF-8") from exc
    parts = dataset_path.split("/")[1:]
    if (len(encoded) > MAX_PATH_BYTES or len(parts) > MAX_DEPTH
        or any(not part or part in (".", "..") or any(ord(c) < 32 or ord(c) == 127
                                                      for c in part) for part in parts)):
        raise UnsupportedFormat("selected dataset path is not canonical or exceeds limits")
    return parts


def _other_contiguous_extent(reader: H5File | ModernH5File, layout: bytes,
                             *, older: bool) -> tuple[int, int] | None:
    """Read an unrelated dataset's allocation without interpreting its type.

    The physical range comes from its own rooted layout message. The selected
    payload cannot be assigned while another rooted object also claims any of
    those bytes. An unknown layout is refused, never assumed nonoverlapping.
    """
    osize, lsize = reader.superblock.offset_size, reader.superblock.length_size
    if len(layout) < 2 or layout[0] not in (3, 4, 5):
        raise UnsupportedFormat("other rooted dataset has an unsupported storage layout")
    if layout[1] == 0:  # Compact bytes live inside its parsed object header.
        return None
    if layout[1] == 1:
        length = 2 + osize + lsize
        if len(layout) != length and not (older and len(layout) == (length + 7) // 8 * 8
                                         and not any(layout[length:])):
            raise FormatError("other rooted contiguous layout length is contradictory")
        address = _uint(layout[2:2 + osize])
        stored = _uint(layout[2 + osize:length])
        if address == reader.superblock.undefined_address:
            return None
        absolute = reader.superblock.base_address + address
        if absolute >= reader.superblock.eof_address or stored > reader.superblock.eof_address - absolute:
            raise FormatError("other rooted contiguous allocation exceeds declared EOF")
        return absolute, absolute + stored
    if layout[1] == 2:
        raise UnsupportedFormat("other rooted chunked dataset needs independent allocation inventory")
    if layout[1] == 3:  # Virtual storage has no payload in this file.
        return None
    raise UnsupportedFormat("other rooted dataset has an unknown storage layout")


def _refuse_competing_rooted_allocations(reader: H5File | ModernH5File,
                                         root: int, root_cached: tuple[int, int] | None,
                                         selected_address: int, selected_start: int,
                                         selected_end: int, *, older: bool) -> int:
    """Boundedly follow the local namespace, refusing competing allocations.

    The selected path alone cannot distinguish a redirect to a sibling's data.
    A partial or unsupported namespace inventory is not evidence of exclusive
    ownership, so it fails closed rather than trusting a solitary pointer.
    """
    pending = [(root, root_cached, 0)]
    visited: dict[int, tuple[int, int] | None] = {}
    links_seen = 0
    beyond_physical_eof = 0
    chunked_sibling_seen = False
    while pending:
        address, cached, depth = pending.pop()
        if address in visited:
            if older and cached is not None and visited[address] not in (None, cached):
                raise FormatError("rooted hard links disagree about a cached group")
            continue
        if len(visited) >= MAX_OWNERSHIP_OBJECTS or depth > MAX_DEPTH:
            raise UnsupportedFormat("rooted ownership inventory exceeds object or depth limit")
        visited[address] = cached
        if address != selected_address and reader.absolute(address) >= reader.size:
            # A physically truncated tail may remove an unrelated sibling's
            # object header. Its allocation claims cannot be inspected, and
            # this limitation must be disclosed with the partial result.
            beyond_physical_eof += 1
            continue
        messages = (_old_messages(reader, address) if older else _messages(reader, address))
        layout = _unique(messages, 8)
        group = (_unique(messages, 0x11) is not None if older else
                 _unique(messages, 2) is not None or _unique(messages, 10) is not None)
        if group and layout is not None:
            raise FormatError("rooted object declares both group links and dataset storage")
        if group:
            children = (_old_group(reader, address, cached) if older else
                        _compact_links(reader, address))
            links_seen += len(children)
            if links_seen > MAX_OWNERSHIP_LINKS:
                raise UnsupportedFormat("rooted ownership inventory exceeds link limit")
            for step in children.values():
                if step is not None:
                    pending.append((step.object_address, step.cached_group, depth + 1))
        elif layout is not None and address != selected_address:
            if len(layout.data) >= 2 and layout.data[0] in (3, 4, 5) and layout.data[1] == 2:
                chunked_sibling_seen = True
                continue
            extent = _other_contiguous_extent(reader, layout.data, older=older)
            if extent is not None and selected_start < extent[1] and extent[0] < selected_end:
                raise FormatError("selected payload overlaps another rooted dataset allocation")
    if chunked_sibling_seen:
        from .ownership_inventory import inventory_other_allocations, reject_sibling_overlap
        inventory = inventory_other_allocations(reader.path, selected_address)
        reject_sibling_overlap([(selected_start, selected_end, ())], inventory)
        if not inventory.complete:
            raise UnsupportedFormat("chunked sibling allocations cannot be completely inventoried")
    return beyond_physical_eof


def read_nonchunked_spec(snapshot: Path, dataset_path: str) -> NonchunkedSpec:
    """Resolve a selected dataset through surviving local hard links only."""
    parts = _check_path(dataset_path)
    snapshot = Path(snapshot)
    if snapshot.stat().st_size > 4 * (1 << 30):
        raise UnsupportedFormat("nonchunked source exceeds 4 GiB limit")
    with snapshot.open("rb") as stream:
        position = 0
        size = snapshot.stat().st_size
        while position + 9 <= size:
            stream.seek(position)
            if stream.read(8) == SIGNATURE:
                version = stream.read(1)[0]
                break
            position = 512 if position == 0 else position * 2
        else:
            raise FormatError("HDF5 signature not found at a permitted offset")
    if version in (0, 1):
        reader_type: type[H5File | ModernH5File] = _TruncatedOldReader
    elif version in (2, 3):
        reader_type = _TruncatedModernReader
    else:
        raise UnsupportedFormat(f"unsupported superblock version {version}")
    older = version in (0, 1)
    with reader_type(snapshot) as reader:
        if older:
            root, cached = _old_root_address(reader)
        else:
            root, cached = reader.superblock.root_object_address, None
        root_cached = cached
        address = root
        chain = []
        for part in parts:
            links = (_old_group(reader, address, cached) if older else
                     _compact_links(reader, address))
            if part not in links:
                raise UnsupportedFormat(f"selected component {part!r} has no rooted hard link")
            step = links[part]
            if step is None:
                raise UnsupportedFormat(f"selected component {part!r} is not a local hard link")
            chain.append({
                "group_address": step.group_address, "name": step.name,
                "object_address": step.object_address,
                "link_message_offset": step.link_message_offset,
                "index_record_offset": step.index_record_offset,
            })
            address, cached = step.object_address, step.cached_group
        messages = _old_messages(reader, address) if older else _messages(reader, address)
        if _unique(messages, 7) is not None:
            raise UnsupportedFormat("external raw storage requires its own dependency route")
        space, datatype, layout = (_unique(messages, kind) for kind in (1, 3, 8))
        if space is None or datatype is None or layout is None:
            raise UnsupportedFormat("selected object lacks required dataset metadata")
        shape = _dataspace(space.data, reader.superblock.length_size, older=older)
        dtype = _datatype(datatype.data, older=older)
        expected = prod(shape) * np.dtype(dtype).itemsize
        if expected <= 0 or expected > MAX_DATA_BYTES:
            raise UnsupportedFormat("dataset bytes exceed bounded nonchunked route")
        if _unique(messages, 11) is not None:
            raise UnsupportedFormat("unexpected filter pipeline on compact/contiguous dataset")
        storage, data_address, absolute, stored = _layout(
            layout.data, older=older, osize=reader.superblock.offset_size,
            lsize=reader.superblock.length_size, expected_size=expected,
            base=reader.superblock.base_address, declared_eof=reader.superblock.eof_address,
            message_offset=layout.absolute_offset,
        )
        uninspected = (_refuse_competing_rooted_allocations(
            reader, root, root_cached, address, absolute, absolute + stored,
            older=older,
        ) if absolute is not None else 0)
        _validate_metadata_ranges(reader.metadata_ranges)
        if absolute is not None:
            end = absolute + stored
            overlapping = [kind for start, stop, kind in reader.metadata_ranges
                           if absolute < stop and start < end]
            if storage == "compact":
                # This is the only intended payload/metadata overlap. A full
                # checked object header encloses the compact layout message.
                if not any(start <= absolute and end <= stop and "object header" in kind
                           for start, stop, kind in reader.metadata_ranges):
                    raise FormatError("compact bytes are not inside the selected object header")
                if any("object header" not in kind for kind in overlapping):
                    raise FormatError("compact bytes overlap unrelated rooted metadata")
            elif overlapping:
                raise FormatError("contiguous payload overlaps rooted metadata: " + overlapping[0])
        omitted = tuple(
            f"auxiliary message type {message.kind} at byte {message.absolute_offset}: omitted"
            for message in messages if message.kind in (5, 12, 13, 17, 21)
        )
        return NonchunkedSpec(
            path=dataset_path, object_address=address, shape=shape, dtype=dtype,
            layout=storage, source_address=data_address,
            source_absolute_offset=absolute, stored_size=stored,
            metadata_route=("unchecksummed_older_symbol_table_hard_links" if older else
                            "checksummed_modern_local_hard_links"),
            root_address=root, link_chain=tuple(chain),
            metadata_ranges=tuple(reader.metadata_ranges),
            omitted_auxiliary_metadata=omitted,
            truncated_source=(reader.superblock.eof_address > reader.size),
            uninspected_rooted_objects=uninspected,
        )


def analyze_nonchunked_snapshot(
    snapshot: Path, dataset_path: str, source: Path, before_hash: str,
    identity: tuple[int, int, int, int, int], size: int,
) -> NonchunkedAnalysis:
    """Inspect immutable snapshot bytes and mark every unobserved element unknown."""
    from .recovery import VERSION

    spec = read_nonchunked_spec(snapshot, dataset_path)
    offset = spec.source_absolute_offset
    available = (0 if offset is None else
                 min(spec.stored_size, max(0, size - offset)))
    recovered = available // spec.element_size
    accepted_bytes = recovered * spec.element_size
    if accepted_bytes > MAX_DATA_BYTES:
        raise UnsupportedFormat("nonchunked accepted byte count exceeds limit")
    with Path(snapshot).open("rb") as stream:
        if offset is not None:
            stream.seek(offset)
        raw = stream.read(accepted_bytes) if accepted_bytes else b""
        if len(raw) != accepted_bytes:
            raise FormatError("short read while copying justified contiguous extent")
        remainder = stream.read(available - accepted_bytes) if offset is not None else b""
    status = np.zeros(spec.shape, dtype="u1")
    status.flat[:recovered] = STATUS_CODES["recovered"]
    mapping = ([{
        "first_linear_element": 0,
        "element_count": recovered,
        "source_absolute_offset": offset,
        "size_bytes": accepted_bytes,
        "sha256_of_damaged_snapshot_bytes": hashlib.sha256(raw).hexdigest(),
        "coordinate_rule": "C row-major: offset + linear_element * element_size",
        "integrity": "not_independently_verified",
        "on_disk_checksum": (
            "v2 object-header checksum covers current compact bytes"
            if spec.layout == "compact" and "checksummed" in spec.metadata_route
            and "unchecksummed" not in spec.metadata_route else "none"
        ),
    }] if recovered else [])
    fragments = ([{
        "reason": "incomplete final element in physically truncated source",
        "source_absolute_offset": offset + accepted_bytes,
        "size_bytes": len(remainder),
        "sha256_of_damaged_snapshot_bytes": hashlib.sha256(remainder).hexdigest(),
    }] if remainder else [])
    report: dict[str, Any] = {
        "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
        "execution_state": "finished",
        "operation": "structural_nonchunked_export", "structural_repair": False,
        "outcome": "complete" if recovered == spec.elements else "partial",
        "complete": recovered == spec.elements,
        "source": {"path": str(source), "size_bytes": size,
                   "sha256_before": before_hash, "sha256_after": before_hash},
        "dataset": {"path": spec.path, "object_address": spec.object_address,
                    "shape": list(spec.shape), "dtype": spec.dtype,
                    "layout": spec.layout, "element_size": spec.element_size,
                    "attributes_copied": [], "attributes_omitted": list(spec.omitted_auxiliary_metadata)},
        "metadata_resolution": {
            "route": spec.metadata_route, "superblock_root_address": spec.root_address,
            "selected_hard_link_chain": list(spec.link_chain),
            "checksum_status": ("traversed v2/3 superblock and v2 object headers checked"
                                if "unchecksummed" not in spec.metadata_route else
                                "older graph has no structural checksums"),
            "warning": (
                "Older group and object-header metadata lacks checksums; internal consistency "
                "does not prove historical ownership."
                if "unchecksummed" in spec.metadata_route else
                "Checksummed modern metadata establishes an internally consistent selected "
                "path, not historical authenticity."
            ),
            "parsed_metadata_ranges": [
                {"start": start, "end": stop, "kind": kind}
                for start, stop, kind in spec.metadata_ranges
            ],
            "allocation_conflict_check": {
                "scope": "bounded rooted local hard-link graph and observed sibling allocations",
                "complete": spec.uninspected_rooted_objects == 0,
                "uninspected_objects_beyond_physical_eof": spec.uninspected_rooted_objects,
                "note": (
                    "Headers lost beyond physical EOF may have had allocation claims; "
                    "this inventory cannot exclude those historical claims."
                    if spec.uninspected_rooted_objects else
                    "No competing rooted allocation was observed within the bounded supported graph."
                ),
            },
        },
        "allocation": {
            "source_address": spec.source_address,
            "source_absolute_offset": offset,
            "declared_size_bytes": spec.stored_size,
            "physically_available_size_bytes": available,
            "declared_eof_past_physical_file": spec.truncated_source,
            "rooted_objects_beyond_physical_eof": spec.uninspected_rooted_objects,
        },
        "counts": {"recovered": recovered, "unknown": spec.elements - recovered},
        "validity": {"dataset": "/_h5reclaim/element_status", "codes": STATUS_CODES,
                     "granularity": "one code per selected dataset element"},
        "mappings": mapping, "unassigned_fragments": fragments,
        "evidence_ledger": {
            "selected_dataset_anchor": spec.object_address,
            "hard_link_chain": list(spec.link_chain),
            "physical_ranges": mapping,
            "contradictions": [],
            "decision": "accept complete elements only at the selected layout address",
        },
        "integrity_note": (
            "Metadata establishes internally consistent ownership and row-major position. "
            "It does not establish that unchecksummed payload values equal their pre-damage values."
        ),
        "metadata_note": (
            "Only values, selected shape, datatype, storage class, and element validity "
            "are exported. "
            "Original attributes, links, scales, fill rules, and sibling objects are not reconstructed."
        ),
    }
    return NonchunkedAnalysis(spec, status, raw, report, identity)


def export_nonchunked(
    source: Path, dataset_path: str, output: Path, report_path: Path, *,
    hints: DatasetHints | None = None,
) -> dict[str, Any]:
    """Publish a separate HDF5/JSON pair only after source identity rechecks."""
    from .recovery import (_validate_paths, _verify_source, sha256_file,
                           source_snapshot, RecoveryError)

    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    with source_snapshot(source) as (snapshot, source_hash, identity, size):
        analysis = analyze_nonchunked_snapshot(
            snapshot, dataset_path, source, source_hash, identity, size,
        )
        if hints is not None:
            if hints.chunks is not None:
                raise HintsError("operator hints assert chunks for observed compact/contiguous storage")
            comparisons = compare_hints(
                hints, observed_fields={
                    "path": analysis.spec.path, "shape": analysis.spec.shape,
                    "dtype": analysis.spec.dtype, "filters": (),
                }, input_sha256=source_hash,
            )
            require_no_conflicts(comparisons)
            analysis.report["operator_hints"] = {
                "trust_level": hints.trust_level,
                "comparisons": [
                    {"field": item.field, "asserted": item.asserted,
                     "observed": item.observed, "status": item.status}
                    for item in comparisons
                ],
                "note": hints.note,
                "warning": "Matching hints do not authenticate current or pre-damage measurements.",
            }
        if sha256_file(snapshot) != source_hash:
            raise RecoveryError("private source snapshot changed during analysis")
        report_text = json.dumps(analysis.report, indent=2, sort_keys=True) + "\n"
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=output.parent) as out_dir:
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=report_path.parent) as rep_dir:
                output_temp = Path(out_dir) / "output.h5"
                report_temp = Path(rep_dir) / "report.json"
                published = False
                try:
                    with h5py.File(output_temp, "x") as handle:
                        spec = analysis.spec
                        values = np.zeros(spec.elements, dtype=np.dtype(spec.dtype))
                        count = analysis.report["counts"]["recovered"]
                        if count:
                            values[:count] = np.frombuffer(analysis.recovered_bytes, dtype=spec.dtype)
                        if spec.layout == "compact":
                            parent_name, name = spec.path.rsplit("/", 1)
                            parent = handle.require_group(parent_name or "/")
                            space = (h5py.h5s.create(h5py.h5s.SCALAR) if not spec.shape else
                                     h5py.h5s.create_simple(spec.shape))
                            creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                            creation.set_layout(h5py.h5d.COMPACT)
                            dataset = h5py.Dataset(h5py.h5d.create(
                                parent.id, name.encode("utf-8"),
                                h5py.h5t.py_create(np.dtype(spec.dtype)),
                                space, dcpl=creation,
                            ))
                        else:
                            dataset = handle.create_dataset(spec.path, shape=spec.shape,
                                                            dtype=spec.dtype)
                        dataset[...] = values.reshape(spec.shape)
                        meta = handle.create_group("/_h5reclaim")
                        validity = meta.create_dataset("element_status", data=analysis.status,
                                                       dtype="u1")
                        validity.attrs["codes_json"] = json.dumps(STATUS_CODES, sort_keys=True)
                        validity.attrs["axis_meaning"] = "one entry per selected dataset element"
                        meta.create_dataset("report_json", data=report_text,
                                            dtype=h5py.string_dtype(encoding="utf-8"))
                        dataset.attrs["h5reclaim_element_status"] = "/_h5reclaim/element_status"
                        dataset.attrs["h5reclaim_complete"] = analysis.report["complete"]
                        dataset.attrs["h5reclaim_warning"] = (
                            "Check element_status before using values. Unknown output elements "
                            "read as zero but are not known measurements."
                        )
                        meta.attrs["source_sha256"] = source_hash
                        meta.attrs["report_schema_version"] = 1
                        handle.flush()
                    with h5py.File(output_temp, "r") as handle:
                        observed = handle[dataset_path].astype(np.dtype(analysis.spec.dtype))[...]
                        if observed.tobytes(order="C")[:len(analysis.recovered_bytes)] != analysis.recovered_bytes:
                            raise RecoveryError("output numeric bit patterns changed during export")
                    report_temp.write_text(report_text, encoding="utf-8")
                    _verify_source(source, identity, source_hash)
                    _validate_paths(source, output, report_path)
                    os.link(output_temp, output)
                    published = True
                    os.link(report_temp, report_path)
                    return analysis.report
                except Exception:
                    if published:
                        output.unlink(missing_ok=True)
                    raise
