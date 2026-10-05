"""Conservative one-byte corrections of two checked modern metadata pointers.

The recorded checksum is *never* rewritten. A candidate must restore the
original checksum uniquely, and the resulting private trial must have a
rooted, independently checked object and a complete ownership inventory.
This cannot recover overwritten measurements or arbitrary object headers.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np

from .format import FormatError, SIGNATURE, UnsupportedFormat
from .metadata import UnsupportedCase, _selected_local_dataset, read_dataset_spec
from .modern_indexes import MAX_HEADER_BYTES, ModernH5File, lookup3
from .ownership_inventory import inventory_other_allocations
from .recovery import RecoveryError, _verify_source, sha256_file, source_snapshot


MAX_TRIAL_BYTES = 512 << 20
MAX_SIGNATURE_OFFSET = 1 << 20


@dataclass(frozen=True)
class MetadataCorrection:
    kind: str
    physical_offset: int
    before_byte: int
    after_byte: int
    original_checksum: int
    pointer_before: int
    pointer_after: int
    rooted_object_address: int
    source_sha256: str
    trial_sha256: str
    changed_bytes: int = 1
    checksum_bytes_changed: int = 0
    historical_payload_verified: bool = False


def _unique_one_byte(raw: bytes, positions: range, checksum: int) -> tuple[int, int]:
    """Return exactly one original-checksum-preserving substitution.

    Count all checksum matches, even structurally invalid pointers: otherwise
    selecting a plausible-looking child from multiple matches can invent an
    owner. The caller validates the sole match independently afterward.
    """
    matches: list[tuple[int, int]] = []
    for position in positions:
        for value in range(256):
            if value == raw[position]:
                continue
            changed = bytearray(raw)
            changed[position] = value
            if lookup3(changed[:-4]) == checksum:
                matches.append((position, value))
                if len(matches) > 1:
                    raise FormatError("ambiguous original-checksum correction candidates")
    if not matches:
        raise FormatError("no unique original-checksum correction in the bounded pointer")
    return matches[0]


def _superblock_raw(source: Path) -> tuple[int, bytes, int, int]:
    size = source.stat().st_size
    position = 0
    candidates = []
    with source.open("rb") as handle:
        while position <= MAX_SIGNATURE_OFFSET and position + 12 <= size:
            handle.seek(position)
            prefix = handle.read(12)
            if prefix[:8] == SIGNATURE:
                if candidates:
                    raise FormatError("ambiguous exact and checksum-justified modern signatures")
                break
            # A missing signature is recoverable only under an independently
            # retained modern superblock checksum, at a documented offset.
            # Recognizable magic or a plausible pointer alone is insufficient.
            if (sum(left != right for left, right in zip(prefix[:8], SIGNATURE)) == 1
                    and prefix[8] in (2, 3) and prefix[9] in (2, 4, 8)
                    and prefix[10] in (2, 4, 8) and prefix[11] == 0):
                length = 16 + 4 * prefix[9]
                if position + length <= size:
                    handle.seek(position)
                    candidate = handle.read(length)
                    corrected = SIGNATURE + candidate[8:]
                    if lookup3(corrected[:-4]) == int.from_bytes(candidate[-4:], "little"):
                        candidates.append((position, candidate))
            position = 512 if position == 0 else 2 * position
        else:
            if len(candidates) > 1:
                raise FormatError("ambiguous checksum-justified modern signature corrections")
            if not candidates:
                raise UnsupportedCase("no modern superblock at a bounded documented signature offset")
            position, candidate = candidates[0]
            prefix = candidate[:12]
        version, osize, lsize, flags = prefix[8:12]
        if version not in (2, 3) or osize not in (2, 4, 8) or lsize not in (2, 4, 8) or flags:
            raise UnsupportedCase("requires an ordinary modern v2/v3 superblock with known widths")
        length = 16 + 4 * osize
        if position + length > size:
            raise FormatError("truncated modern superblock")
        handle.seek(position)
        raw = handle.read(length)
    root_start = 12 + 3 * osize
    base = int.from_bytes(raw[12:12 + osize], "little")
    eof = int.from_bytes(raw[12 + 2 * osize:12 + 3 * osize], "little")
    if base != position or eof <= position + length or eof > size:
        raise FormatError("uncorrected modern superblock base or EOF is invalid")
    extension = int.from_bytes(raw[12 + osize:12 + 2 * osize], "little")
    if extension != (1 << (8 * osize)) - 1 and not 0 <= base + extension < eof:
        raise FormatError("uncorrected modern superblock extension is invalid")
    return position, raw, osize, root_start


def _read_checked_header(reader: ModernH5File, address: int) -> tuple[bytes, int]:
    prefix = reader.read_at(address, 6)
    if prefix[:5] != b"OHDR\x02" or prefix[5] & 0xC0:
        raise FormatError("rooted object is not an ordinary v2 object header")
    flags = prefix[5]
    extra = (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0)
    size_width = 1 << (flags & 3)
    prefix_length = 6 + extra + size_width
    content_size = int.from_bytes(reader.read_at(address + prefix_length - size_width,
                                                  size_width), "little")
    if content_size < 4 or content_size > MAX_HEADER_BYTES:
        raise UnsupportedFormat("rooted object header exceeds the bounded first chunk")
    raw = reader.read_at(address, prefix_length + content_size + 4)
    if lookup3(raw[:-4]) != int.from_bytes(raw[-4:], "little"):
        raise FormatError("rooted object header checksum mismatch")
    return raw, prefix_length


def _rooted_compact_target(reader: ModernH5File, name: str) -> int:
    """Resolve one direct local hard link from a checked compact root group."""
    root = reader.superblock.root_object_address
    raw, prefix_length = _read_checked_header(reader, root)
    if raw[5] & 0x04:
        raise UnsupportedFormat("compact root with message creation order is outside this correction")
    contents = raw[prefix_length:-4]
    cursor = 0
    matches: list[int] = []
    while len(contents) - cursor >= 4:
        kind, size, flags = contents[cursor], int.from_bytes(contents[cursor+1:cursor+3], "little"), contents[cursor+3]
        cursor += 4
        if size > len(contents) - cursor or flags & 0x22:
            raise FormatError("root object header has a malformed or shared message")
        data = contents[cursor:cursor+size]
        cursor += size
        if kind != 6:
            if kind == 0x10:
                raise UnsupportedFormat("root continuation is outside compact correction")
            continue
        if len(data) < 3 or data[0] != 1 or data[1] & ~3:
            raise UnsupportedFormat("root link message uses an unsupported link variant")
        width = 1 << (data[1] & 3)
        if len(data) < 2 + width:
            raise FormatError("root hard link name length is truncated")
        nlength = int.from_bytes(data[2:2+width], "little")
        start = 2 + width
        if not 1 <= nlength <= 2048 or len(data) != start+nlength+reader.superblock.offset_size:
            raise UnsupportedFormat("root link is not a bounded compact local hard link")
        try:
            observed = data[start:start+nlength].decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise FormatError("root link name is invalid UTF-8") from exc
        if observed == name:
            target = int.from_bytes(data[start+nlength:], "little")
            reader.absolute(target)
            matches.append(target)
    if any(contents[cursor:]) or len(matches) != 1:
        raise FormatError("selected name does not have one checked compact root hard link")
    return matches[0]


def _selected_layout_pointer(reader: ModernH5File, selected_address: int) -> tuple[int, bytes, int, int]:
    """Locate only the final index-address field of a first-chunk v4/v5 layout."""
    absolute = reader.absolute(selected_address)
    prefix = reader.read_at(selected_address, 6)
    if prefix[:5] != b"OHDR\x02" or prefix[5] & 0xC0 or prefix[5] & 0x04:
        raise UnsupportedFormat("selected header is not a bounded v2 header without creation-order fields")
    flags = prefix[5]
    extra = (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0)
    width = 1 << (flags & 3)
    prefix_length = 6+extra+width
    content_size = int.from_bytes(reader.read_at(selected_address+prefix_length-width, width), "little")
    if content_size < 4 or content_size > MAX_HEADER_BYTES:
        raise UnsupportedFormat("selected header length exceeds the bounded first chunk")
    raw = reader.read_at(selected_address, prefix_length+content_size+4)
    if lookup3(raw[:-4]) == int.from_bytes(raw[-4:], "little"):
        raise UnsupportedCase("selected object header checksum is intact")
    contents = raw[prefix_length:-4]
    cursor = 0
    pointers: list[int] = []
    while len(contents)-cursor >= 4:
        kind, size, msg_flags = contents[cursor], int.from_bytes(contents[cursor+1:cursor+3], "little"), contents[cursor+3]
        cursor += 4
        if size > len(contents)-cursor or msg_flags & 0x22:
            raise FormatError("selected object header has malformed or shared messages")
        data = contents[cursor:cursor+size]
        if kind == 8:
            if len(data) <= reader.superblock.offset_size + 5 or data[0] not in (4, 5) or data[1] != 2:
                raise UnsupportedFormat("selected header lacks a supported modern chunk layout")
            ndim, dimwidth = data[3:5]
            if not 2 <= ndim <= 5 or dimwidth not in (1, 2, 4, 8):
                raise UnsupportedFormat("selected chunk dimensions are out of scope")
            kind_position = 5+ndim*dimwidth
            if kind_position >= len(data) or data[kind_position] not in (3, 4, 5):
                raise UnsupportedFormat("only checked fixed-array, extensible-array, or v2 B-tree links can be corrected")
            pointer = prefix_length + cursor + size - reader.superblock.offset_size
            pointers.append(pointer)
        elif kind == 0x10:
            raise UnsupportedFormat("selected header continuation is outside this correction")
        cursor += size
    if any(contents[cursor:]) or len(pointers) != 1:
        raise FormatError("selected object needs exactly one first-chunk index pointer")
    return absolute, raw, pointers[0], selected_address


def _validate_trial(trial: Path, dataset_path: str, *, expected_object: int | None = None) -> int:
    with ModernH5File(trial) as reader:
        root = reader.superblock.root_object_address
        _read_checked_header(reader, root)
        if expected_object is not None:
            if dataset_path.count("/") != 1 or _rooted_compact_target(reader, dataset_path[1:]) != expected_object:
                raise FormatError("corrected dataset is not anchored by the checked root hard link")
    with h5py.File(trial, "r") as handle:
        if int(h5py.h5o.get_info(handle["/"].id).addr) != root:
            raise FormatError("native root contradicts the corrected superblock")
        selected = _selected_local_dataset(handle, dataset_path)
        object_address = int(h5py.h5o.get_info(selected.id).addr)
        if expected_object is not None and object_address != expected_object:
            raise FormatError("native selected address contradicts checked root link")
    spec = read_dataset_spec(trial, dataset_path)
    if object_address != spec.object_address:
        raise FormatError("selected object address changed across verification")
    with ModernH5File(trial) as reader:
        reader.read_index(spec.object_address, spec.shape, spec.chunks,
                          np.dtype(spec.dtype).itemsize,
                          maxshape=spec.maxshape, filters=spec.filters)
    inventory = inventory_other_allocations(trial, spec.object_address)
    if not inventory.complete:
        raise UnsupportedCase("corrected trial lacks complete rooted ownership inventory")
    return object_address


def create_root_address_trial(source: str | Path, dataset_path: str,
                              trial: str | Path) -> MetadataCorrection:
    """Build an exclusive disposable trial after unique root-pointer correction."""
    return _create_trial(Path(source), dataset_path, Path(trial), "superblock_root")


def create_layout_pointer_trial(source: str | Path, dataset_path: str,
                                trial: str | Path) -> MetadataCorrection:
    """Build a trial from one checked compact-root selected index pointer."""
    return _create_trial(Path(source), dataset_path, Path(trial), "selected_layout_pointer")


def _create_trial(source: Path, dataset_path: str, trial: Path,
                  kind: str) -> MetadataCorrection:
    if source.resolve() == trial.resolve() or trial.exists() or not trial.parent.is_dir():
        raise RecoveryError("trial path must be new, distinct from source, and in an existing directory")
    with source_snapshot(source, max_source_bytes=MAX_TRIAL_BYTES) as (snapshot, digest, identity, size):
        offset, superblock, osize, root_start = _superblock_raw(snapshot)
        original_super_checksum = int.from_bytes(superblock[-4:], "little")
        if kind == "superblock_root":
            if lookup3(superblock[:-4]) == original_super_checksum:
                raise UnsupportedCase("modern superblock checksum is intact")
            relative_position, value = _unique_one_byte(superblock, range(root_start, root_start+osize), original_super_checksum)
            physical = offset + relative_position
            pointer_before = int.from_bytes(superblock[root_start:root_start+osize], "little")
            changed = bytearray(superblock)
            changed[relative_position] = value
            pointer_after = int.from_bytes(changed[root_start:root_start+osize], "little")
            original_byte = superblock[relative_position]
            expected_object = None
        else:
            if lookup3(superblock[:-4]) != original_super_checksum:
                raise FormatError("selected layout correction requires an intact superblock")
            if dataset_path.count("/") != 1 or not dataset_path.startswith("/") or dataset_path == "/":
                raise UnsupportedCase("selected layout correction supports one direct root hard link")
            with ModernH5File(snapshot) as reader:
                selected_address = _rooted_compact_target(reader, dataset_path[1:])
                start, raw, position, expected_object = _selected_layout_pointer(reader, selected_address)
            checksum = int.from_bytes(raw[-4:], "little")
            relative_position, value = _unique_one_byte(raw, range(position, position+osize), checksum)
            physical = start + relative_position
            pointer_before = int.from_bytes(raw[position:position+osize], "little")
            changed = bytearray(raw)
            changed[relative_position] = value
            pointer_after = int.from_bytes(changed[position:position+osize], "little")
            original_byte = raw[relative_position]
            original_super_checksum = checksum
        if pointer_after == pointer_before or pointer_after == (1 << (8*osize))-1:
            raise FormatError("corrected pointer is invalid")
        # Keep an unpublished copy until every independent validation passes.
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-meta-", dir=trial.parent) as directory:
            private = Path(directory) / "trial.h5"
            shutil.copyfile(snapshot, private)
            with private.open("r+b") as handle:
                handle.seek(physical)
                if handle.read(1) != bytes((original_byte,)):
                    raise RecoveryError("private trial disagrees with source at candidate byte")
                handle.seek(physical)
                handle.write(bytes((value,)))
            if size != private.stat().st_size:
                raise RecoveryError("private correction changed file length")
            object_address = _validate_trial(private, dataset_path, expected_object=expected_object)
            if expected_object is None:
                with ModernH5File(private) as reader:
                    if reader.superblock.root_object_address != pointer_after:
                        raise FormatError("corrected pointer is not the rooted object")
            if sha256_file(snapshot) != digest:
                raise RecoveryError("source snapshot changed during metadata trial")
            _verify_source(source, identity, digest)
            trial_digest = sha256_file(private)
            os.link(private, trial)
            return MetadataCorrection(kind, physical, original_byte, value,
                                      original_super_checksum, pointer_before, pointer_after,
                                      object_address, digest, trial_digest)
