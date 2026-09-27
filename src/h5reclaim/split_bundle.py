"""Bounded current-value export from an explicitly pinned HDF5 Split pair.

The Split VFD is a particular two-member Multi configuration.  This route
verifies that configuration in the superblock's driver information before
letting HDF5 open private, renamed copies.  A generic Multi configuration,
another driver, an unavailable member, or an address outside the declared and
physically present raw member is refused.  The two SHA-256 pins identify the
supplied *current* bytes, not their historical correctness.

Format: https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
H5Pset_fapl_split: https://support.hdfgroup.org/documentation/hdf5/latest/group___f_a_p_l.html
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from math import prod
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .format import FormatError, UnsupportedFormat
from .metadata import UnsupportedCase
from .metadata_fallback import _messages, _unique
from .modern_indexes import ModernH5File
from .readable_export import (
    MAX_BLOCK_BYTES, MAX_CHUNKS, MAX_DATA_BYTES, MAX_STORED_CHUNK_BYTES,
    NATIVE_FILTERS, _blocks, _create_matching_dataset, _range_sha256,
    _safe_fixed_type, _selected_dataset,
)
from .recovery import RecoveryError, VERSION, _verify_source, sha256_file, source_snapshot


MAX_BUNDLE_BYTES = 4 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
SIGNATURE = b"\x89HDF\r\n\x1a\n"
DRIVER_ID = b"NCSAmult"
# H5FD_MEM_DEFAULT and the six file memory classes, then two reserved bytes.
# Metadata maps to SUPER (1), raw data to DRAW (3).
SPLIT_MAPPING = bytes((1, 1, 3, 3, 1, 1, 0, 0))


@dataclass(frozen=True)
class SplitMap:
    superblock_version: int
    raw_address: int
    metadata_eoa: int
    raw_eoa: int
    stored_member_names: tuple[str, str]
    checksum_validated: bool


def _load_manifest(value: str | Path | dict[str, Any]) -> tuple[Path, str, Path, str]:
    if isinstance(value, (str, Path)):
        with Path(value).open("rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise RecoveryError("Split manifest exceeds 64 KiB")
        value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "driver", "members"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["driver"] != "split"):
        raise RecoveryError("expected version-one Split manifest with exactly two named members")
    members = value["members"]
    if not isinstance(members, list) or len(members) != 2:
        raise RecoveryError("Split manifest requires metadata and raw members")
    verified: list[tuple[Path, str]] = []
    for role, entry in zip(("metadata", "raw"), members):
        if not isinstance(entry, dict) or set(entry) != {"role", "path", "sha256"} or entry["role"] != role:
            raise RecoveryError("Split members must be ordered metadata, then raw")
        path, digest = entry["path"], entry["sha256"]
        if (not isinstance(path, str) or not 1 <= len(path) <= 4096
                or "\x00" in path or not Path(path).is_absolute()
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise RecoveryError("Split members need explicit absolute paths and lowercase SHA-256 pins")
        verified.append((Path(path), digest))
    if verified[0][0].resolve(strict=True) == verified[1][0].resolve(strict=True):
        raise RecoveryError("Split metadata and raw members cannot be the same physical file")
    return verified[0][0], verified[0][1], verified[1][0], verified[1][1]


def _signature_position(handle, size: int) -> int:
    offset = 0
    while offset + 8 <= size:
        handle.seek(offset)
        if handle.read(8) == SIGNATURE:
            return offset
        offset = 512 if offset == 0 else offset * 2
    raise UnsupportedFormat("Split metadata member has no HDF5 superblock at a permitted offset")


def _driver_data(metadata: Path) -> tuple[int, int, bytes, bool]:
    """Read a rooted driver information block or checked extension message."""
    size = metadata.stat().st_size
    with metadata.open("rb") as handle:
        base = _signature_position(handle, size)
        handle.seek(base + 8)
        version = handle.read(1)[0]
        if version in (0, 1):
            handle.seek(base + 13)
            offset_size = handle.read(1)[0]
            if offset_size != 8:
                raise UnsupportedFormat("Split route requires eight-byte virtual addresses")
            header_length = 28 if version == 1 else 24
            handle.seek(base + header_length)
            fields = handle.read(4 * offset_size)
            if len(fields) != 4 * offset_size:
                raise FormatError("truncated old Split superblock")
            stored_base, _, eoa, driver = (int.from_bytes(fields[i:i + 8], "little")
                                            for i in range(0, 32, 8))
            if stored_base != base or eoa > size or driver >= eoa or driver < header_length + 32:
                raise FormatError("old Split superblock or driver address is inconsistent")
            address = base + driver
            if address + 16 > min(eoa, size):
                raise FormatError("Split driver block is truncated")
            handle.seek(address)
            prefix = handle.read(16)
            length = int.from_bytes(prefix[4:8], "little")
            if prefix[:4] != b"\x00" * 4 or prefix[8:16] != DRIVER_ID or length > 1024:
                raise UnsupportedFormat("old superblock does not declare bounded NCSAmult information")
            if address + 16 + length > min(eoa, size):
                raise FormatError("Split driver block crosses metadata EOA")
            payload = handle.read(length)
            if len(payload) != length:
                raise FormatError("Split driver block ended early")
            return version, 8, payload, False
        if version in (2, 3):
            # The modern reader validates the superblock checksum and bounds
            # the rooted extension; _messages validates its OHDR checksums.
            with ModernH5File(metadata) as reader:
                sb = reader.superblock
                if sb.offset_size != 8:
                    raise UnsupportedFormat("Split route requires eight-byte virtual addresses")
                header = reader._read_absolute(base, 16 + 4 * sb.offset_size)
                extension = int.from_bytes(header[12 + 8:12 + 16], "little")
                if extension == sb.undefined_address:
                    raise UnsupportedFormat("modern Split superblock has no driver information extension")
                driver_msg = _unique(_messages(reader, extension), 0x14)
                if driver_msg is None or driver_msg.flags & 2:
                    raise UnsupportedFormat("modern Split driver information is missing or shared")
                payload = driver_msg.data
                if (len(payload) < 11 or payload[0] != 0 or payload[1:9] != DRIVER_ID
                        or int.from_bytes(payload[9:11], "little") != len(payload) - 11):
                    raise UnsupportedFormat("modern extension does not declare bounded NCSAmult information")
                return version, sb.offset_size, payload[11:], True
        raise UnsupportedFormat("unsupported Split superblock version")


def inspect_split_map(metadata: str | Path, raw_size: int) -> SplitMap:
    """Validate the two-member physical-address map without opening HDF5 data."""
    metadata = Path(metadata)
    if not 0 <= raw_size <= MAX_BUNDLE_BYTES:
        raise UnsupportedCase("raw member size exceeds bounded Split route")
    version, offset_size, body, checksum = _driver_data(metadata)
    if len(body) < 8 + 4 * offset_size or body[:8] != SPLIT_MAPPING:
        raise UnsupportedFormat("generic Multi mapping is outside the two-member Split route")
    values = [int.from_bytes(body[8 + i * offset_size:8 + (i + 1) * offset_size], "little")
              for i in range(4)]
    meta_start, meta_eoa, raw_start, raw_member_eoa = values
    if (meta_start != 0 or not 0 < meta_eoa <= metadata.stat().st_size
            or raw_start <= meta_eoa or raw_member_eoa > raw_size
            or raw_start + raw_member_eoa >= (1 << 64)):
        raise FormatError("Split member virtual addresses contradict physical sizes")
    remaining = body[8 + 4 * offset_size:]
    names = []
    for _ in range(2):
        end = remaining.find(b"\x00")
        if end < 0 or end > 63:
            raise UnsupportedFormat("Split stored member name is missing or too long")
        padded = (end + 1 + 7) // 8 * 8
        if remaining[end:padded] != b"\x00" * (padded - end):
            raise FormatError("Split member-name padding is nonzero")
        try:
            name = remaining[:end].decode("ascii", "strict")
        except UnicodeError as exc:
            raise UnsupportedFormat("non-ASCII Multi member names are unsupported") from exc
        # The on-disk names are reported, never followed.  Reject names that
        # could address files outside the private snapshot directory.
        if re.fullmatch(r"%s[-._A-Za-z0-9]{1,61}", name) is None:
            raise UnsupportedFormat("Split stored member name is not a safe local generator")
        names.append(name)
        remaining = remaining[padded:]
    if remaining or names[0] == names[1]:
        raise FormatError("Split member-name region is contradictory")
    return SplitMap(version, raw_start, meta_eoa, raw_start + raw_member_eoa,
                    (names[0], names[1]), checksum)


def _raw_range(address: int, length: int, mapping: SplitMap, physical_size: int) -> tuple[int, int]:
    if (address < mapping.raw_address or length <= 0
            or address + length > mapping.raw_eoa
            or address + length - mapping.raw_address > physical_size):
        raise UnsupportedCase("Split payload range is not fully present in the pinned raw member")
    start = address - mapping.raw_address
    return start, start + length


def export_split(
    member_manifest: str | Path | dict[str, Any], dataset_path: str,
    output: str | Path, report_path: str | Path,
) -> dict[str, Any]:
    """Export current readable values from a complete, pinned Split VFD pair."""
    metadata, meta_pin, raw, raw_pin = _load_manifest(member_manifest)
    output, report_path = Path(output), Path(report_path)
    if not output.parent.is_dir() or not report_path.parent.is_dir():
        raise RecoveryError("output and report directories must exist")
    if (output.exists() or output.is_symlink() or report_path.exists() or report_path.is_symlink()
            or output.resolve(strict=False) == report_path.resolve(strict=False)
            or any(target.resolve(strict=False) == source.resolve(strict=True)
                   for target in (output, report_path) for source in (metadata, raw))):
        raise RecoveryError("destinations must be distinct new paths outside the Split evidence")
    with ExitStack() as stack:
        captures = []
        total = 0
        for role, source, pin in (("metadata", metadata, meta_pin), ("raw", raw, raw_pin)):
            snapshot, digest, identity, size = stack.enter_context(
                source_snapshot(source, max_source_bytes=MAX_BUNDLE_BYTES - total))
            if digest != pin:
                raise RecoveryError(f"Split {role} member does not match its pinned hash")
            captures.append((role, source, snapshot, digest, identity, size))
            total += size
        with tempfile.TemporaryDirectory(prefix="h5reclaim-split-") as directory:
            root = Path(directory)
            stem = root / "bundle"
            staged_meta, staged_raw = root / "bundle-m.h5", root / "bundle-r.h5"
            for (_, _, snapshot, digest, _, _), staged in zip(captures, (staged_meta, staged_raw)):
                shutil.copyfile(snapshot, staged)
                if sha256_file(staged) != digest:
                    raise RecoveryError("private Split member changed while being staged")
            mapping = inspect_split_map(staged_meta, captures[1][-1])
            with h5py.File(stem, "r", driver="split", meta_ext=b"-m.h5", raw_ext=b"-r.h5") as handle:
                if handle.id.get_access_plist().get_driver() != h5py.h5fd.MULTI:
                    raise UnsupportedCase("HDF5 did not open the private pair with the Multi driver")
                selected = _selected_dataset(handle, dataset_path)
                creation = selected.id.get_create_plist()
                if selected.is_virtual or creation.get_external_count():
                    raise UnsupportedCase("nested external or virtual dataset needs a separate dependency route")
                _safe_fixed_type(selected.id.get_type(), selected.dtype)
                shape = tuple(int(value) for value in selected.shape)
                if not 1 <= len(shape) <= 4 or any(extent <= 0 for extent in shape):
                    raise UnsupportedCase("Split export requires nonempty rank-one through rank-four storage")
                logical_size = prod(shape) * selected.id.get_type().get_size()
                if logical_size > MAX_DATA_BYTES:
                    raise UnsupportedCase("selected Split dataset exceeds 512 MiB logical limit")
                filters = [creation.get_filter(i)[0] for i in range(creation.get_nfilters())]
                if len(filters) > 8 or any(item not in NATIVE_FILTERS for item in filters):
                    raise UnsupportedCase("selected Split dataset needs an unapproved filter decoder")
                layout = creation.get_layout()
                accepted: list[dict[str, Any]] = []
                unknown: list[tuple[int, ...]] = []
                selections: list[tuple[slice, ...]] = []
                if layout == h5py.h5d.CHUNKED:
                    chunks = selected.chunks
                    if chunks is None or prod(chunks) * selected.dtype.itemsize > MAX_BLOCK_BYTES:
                        raise UnsupportedCase("Split chunk exceeds 1 MiB decoded limit")
                    origins = itertools.product(*(range(0, n, width) for n, width in zip(shape, chunks)))
                    for origin in origins:
                        if len(accepted) + len(unknown) >= MAX_CHUNKS:
                            raise UnsupportedCase("Split dataset exceeds 8192 logical chunks")
                        info = selected.id.get_chunk_info_by_coord(origin)
                        if info.byte_offset is None or not info.size:
                            unknown.append(origin)
                            continue
                        if (tuple(info.chunk_offset) != origin or info.size > MAX_STORED_CHUNK_BYTES
                                or info.filter_mask & ~((1 << len(filters)) - 1)):
                            raise RecoveryError("Split chunk coordinate, stored size or filter mask is inconsistent")
                        address = int(info.byte_offset)
                        start, end = _raw_range(address, int(info.size), mapping, captures[1][-1])
                        accepted.append({"coordinate": list(origin), "logical_address": address,
                                         "member_role": "raw", "physical_byte_range": [start, end],
                                         "stored_bytes": int(info.size), "filter_mask": int(info.filter_mask),
                                         "raw_sha256": _range_sha256(staged_raw, start, end - start)})
                        selections.append(tuple(slice(a, min(a + step, n)) for a, step, n in zip(origin, chunks, shape)))
                    intervals = sorted(tuple(item["physical_byte_range"]) for item in accepted)
                    if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
                        raise RecoveryError("Split chunk index claims overlapping raw-member bytes")
                    if selected.id.get_num_chunks() != len(accepted):
                        raise RecoveryError("Split allocated chunk count contradicts observed index")
                elif layout == h5py.h5d.CONTIGUOUS:
                    address = selected.id.get_offset()
                    if filters or address is None or selected.id.get_storage_size() != logical_size:
                        raise UnsupportedCase("Split contiguous dataset is unallocated or inconsistent")
                    start, end = _raw_range(int(address), logical_size, mapping, captures[1][-1])
                    accepted.append({"logical_address": int(address), "member_role": "raw",
                                     "physical_byte_range": [start, end], "stored_bytes": logical_size,
                                     "raw_sha256": _range_sha256(staged_raw, start, logical_size)})
                    selections = list(_blocks(shape, selected.dtype.itemsize))
                else:
                    raise UnsupportedCase("Split route supports chunked and contiguous raw storage only")
                for _, source, _, digest, identity, _ in captures:
                    _verify_source(source, identity, digest)
                with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=output.parent) as outdir:
                    with tempfile.TemporaryDirectory(prefix=".h5reclaim-", dir=report_path.parent) as repdir:
                        staged_output = Path(outdir) / "output.h5"
                        staged_report = Path(repdir) / "report.json"
                        source_hash = hashlib.sha256()
                        with h5py.File(staged_output, "x") as target:
                            result = _create_matching_dataset(target, selected, dataset_path)
                            if layout == h5py.h5d.CHUNKED:
                                validity = np.zeros(tuple((n + w - 1) // w for n, w in zip(shape, selected.chunks)), dtype="u1")
                                for item in accepted:
                                    validity[tuple(a // w for a, w in zip(item["coordinate"], selected.chunks))] = 1
                                target.create_dataset("/_h5reclaim/validity", data=validity, dtype="u1")
                            for selection in selections:
                                block = np.asarray(selected[selection])
                                if block.nbytes > MAX_BLOCK_BYTES or block.dtype != selected.dtype:
                                    raise RecoveryError("Split native read returned a mismatched bounded block")
                                source_hash.update(block.tobytes(order="C"))
                                result[selection] = block
                            target.flush()
                        output_hash = hashlib.sha256()
                        with h5py.File(staged_output, "r") as target:
                            for selection in selections:
                                output_hash.update(np.asarray(target[dataset_path][selection]).tobytes(order="C"))
                        if source_hash.digest() != output_hash.digest():
                            raise RecoveryError("Split output readback differs from accepted current values")
                        report = {
                            "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                            "mode": "split_native_export", "outcome": "partial" if unknown else "complete",
                            "dataset": {"path": dataset_path, "shape": list(shape), "dtype": selected.dtype.str,
                                        "layout": int(layout), "chunks": list(selected.chunks) if selected.chunks else None},
                            "members": [{"role": role, "path": str(source), "sha256": digest, "size_bytes": size}
                                        for role, source, _, digest, _, size in captures],
                            "address_map": {"metadata_start": 0, "metadata_eoa": mapping.metadata_eoa,
                                            "raw_start": mapping.raw_address, "raw_eoa": mapping.raw_eoa,
                                            "stored_member_names": list(mapping.stored_member_names),
                                            "superblock_version": mapping.superblock_version,
                                            "driver_anchor_checksum_validated": mapping.checksum_validated,
                                            "member_association": "explicit_operator_manifest"},
                            "accepted_ranges": accepted, "unknown_chunk_origins": [list(item) for item in unknown],
                            "accepted_elements": sum(prod(s.stop - s.start for s in selection) for selection in selections),
                            "validity_map": "/_h5reclaim/validity" if layout == h5py.h5d.CHUNKED else None,
                            "current_value_sha256": source_hash.hexdigest(),
                            "note": "Current-value native Split read. The manifest asserts which two members belong together; their hashes pin only the supplied bytes and do not authenticate that association or earlier measurements. Other Multi mappings are unsupported.",
                        }
                        staged_report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                        for _, source, _, digest, identity, _ in captures:
                            _verify_source(source, identity, digest)
                        if output.exists() or output.is_symlink() or report_path.exists() or report_path.is_symlink():
                            raise RecoveryError("destination appeared during Split export")
                        os.link(staged_output, output)
                        try:
                            os.link(staged_report, report_path)
                        except Exception:
                            output.unlink(missing_ok=True)
                            raise
                        return report
