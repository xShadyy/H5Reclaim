"""One-byte trial for modern selected object-header chunk dimensions.

Only a direct checked compact-root hard link, a first-chunk v2 object header,
and the *dataset* chunk-dimension bytes of a v4/v5 layout are considered. The
recorded checksum is never changed. This is a disposable structural trial,
not an assertion that unchecksummed payload values match historical values.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .format import FormatError, UnsupportedFormat
from .metadata import UnsupportedCase, read_dataset_spec
from .metadata_correction import (
    MAX_TRIAL_BYTES, _rooted_compact_target, _superblock_raw, _unique_one_byte,
    _validate_trial,
)
from .modern_indexes import ModernH5File, lookup3
from .ownership_inventory import inventory_other_allocations, reject_sibling_overlap
from .recovery import RecoveryError, _verify_source, sha256_file, source_snapshot


MAX_DIMENSION_HEADER_BYTES = 16 << 10
MAX_DIMENSION_BYTES = 32


@dataclass(frozen=True)
class HeaderDimensionCorrection:
    kind: str
    physical_offset: int
    before_byte: int
    after_byte: int
    original_checksum: int
    dimension_index: int
    dimension_before: int
    dimension_after: int
    rooted_object_address: int
    source_sha256: str
    trial_sha256: str
    changed_bytes: int = 1
    checksum_bytes_changed: int = 0
    historical_payload_verified: bool = False


def _selected_dimension_field(
    reader: ModernH5File, address: int, *, require_mismatch: bool = True,
) -> tuple[int, bytes, range, int, int]:
    """Return physical header start, raw header, dimension bytes, rank, width.

    In particular the last raw layout dimension encodes the datatype size.
    Altering that dimension would change representation rather than repairing
    a dataset chunk extent; it is excluded from the trial.
    """
    absolute = reader.absolute(address)
    prefix = reader.read_at(address, 6)
    if prefix[:5] != b"OHDR\x02" or prefix[5] & 0xC4:
        raise UnsupportedFormat("selected object needs an ordinary first-chunk v2 header")
    flags = prefix[5]
    extra = (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0)
    size_width = 1 << (flags & 3)
    prefix_length = 6 + extra + size_width
    content_size = int.from_bytes(
        reader.read_at(address + prefix_length - size_width, size_width), "little"
    )
    if content_size < 4 or content_size > MAX_DIMENSION_HEADER_BYTES:
        raise UnsupportedFormat("selected first object-header chunk exceeds 16 KiB trial bound")
    raw = reader.read_at(address, prefix_length + content_size + 4)
    if require_mismatch and lookup3(raw[:-4]) == int.from_bytes(raw[-4:], "little"):
        raise UnsupportedCase("selected object header checksum is intact")

    contents = raw[prefix_length:-4]
    cursor = 0
    fields: list[tuple[range, int, int]] = []
    while len(contents) - cursor >= 4:
        kind = contents[cursor]
        length = int.from_bytes(contents[cursor + 1:cursor + 3], "little")
        flags = contents[cursor + 3]
        cursor += 4
        if length > len(contents) - cursor or flags & 0x22:
            raise FormatError("selected object header has malformed or shared messages")
        data = contents[cursor:cursor + length]
        if kind == 0x10:
            raise UnsupportedFormat("selected object-header continuation is outside dimension trial")
        if kind == 8:
            if len(data) < 6 or data[0] not in (4, 5) or data[1] != 2 or data[2] & ~3:
                raise UnsupportedFormat("selected header lacks supported v4/v5 chunk layout")
            ndim, width = data[3:5]
            if not 2 <= ndim <= 5 or width not in (1, 2, 4, 8):
                raise UnsupportedFormat("selected layout dimensions are outside trial bounds")
            extent_bytes = (ndim - 1) * width
            if extent_bytes > MAX_DIMENSION_BYTES or len(data) < 5 + ndim * width + 1:
                raise FormatError("selected layout chunk dimensions are truncated")
            if data[5 + ndim * width] not in (3, 4, 5):
                raise UnsupportedFormat("dimension trial requires fixed-array, extensible-array, or v2 B-tree index")
            start = prefix_length + cursor + 5
            fields.append((range(start, start + extent_bytes), ndim - 1, width))
        cursor += length
    if any(contents[cursor:]) or len(fields) != 1:
        raise FormatError("selected object needs exactly one first-chunk modern chunk layout")
    field, rank, width = fields[0]
    return absolute, raw, field, rank, width


def create_chunk_dimension_trial(
    source: str | Path, dataset_path: str, trial: str | Path,
) -> HeaderDimensionCorrection:
    """Create a new checked disposable trial without altering the source.

    Exactly one byte among selected dataset chunk dimensions must restore the
    original checksum. The resulting rooted graph, exact native schema,
    entire index, bounds and sibling allocation inventory must agree.
    """
    source, trial = Path(source), Path(trial)
    if source.resolve() == trial.resolve() or trial.exists() or not trial.parent.is_dir():
        raise RecoveryError("trial path must be new, distinct from source, and in an existing directory")
    if (not isinstance(dataset_path, str) or dataset_path.count("/") != 1
            or not dataset_path.startswith("/") or dataset_path == "/"):
        raise UnsupportedCase("dimension trial supports one direct checked root hard link")

    with source_snapshot(source, max_source_bytes=MAX_TRIAL_BYTES) as (snapshot, digest, identity, size):
        _, superblock, _, _ = _superblock_raw(snapshot)
        if lookup3(superblock[:-4]) != int.from_bytes(superblock[-4:], "little"):
            raise FormatError("dimension trial requires an intact modern superblock")
        with ModernH5File(snapshot) as reader:
            expected_object = _rooted_compact_target(reader, dataset_path[1:])
            start, raw, field, rank, width = _selected_dimension_field(reader, expected_object)
        original_checksum = int.from_bytes(raw[-4:], "little")
        relative_position, value = _unique_one_byte(raw, field, original_checksum)
        dimension_index = (relative_position - field.start) // width
        first = field.start + dimension_index * width
        dimension_before = int.from_bytes(raw[first:first + width], "little")
        corrected = bytearray(raw)
        corrected[relative_position] = value
        dimension_after = int.from_bytes(corrected[first:first + width], "little")
        if not 0 <= dimension_index < rank or dimension_before == dimension_after or dimension_after <= 0:
            raise FormatError("original-checksum correction has an invalid chunk dimension")
        physical = start + relative_position
        original_byte = raw[relative_position]

        with tempfile.TemporaryDirectory(prefix=".h5reclaim-header-dim-", dir=trial.parent) as directory:
            private = Path(directory) / "trial.h5"
            shutil.copyfile(snapshot, private)
            with private.open("r+b") as handle:
                handle.seek(physical)
                if handle.read(1) != bytes((original_byte,)):
                    raise RecoveryError("private trial disagrees with source at candidate byte")
                handle.seek(physical)
                handle.write(bytes((value,)))
            if private.stat().st_size != size:
                raise RecoveryError("private correction changed file length")
            object_address = _validate_trial(private, dataset_path, expected_object=expected_object)
            spec = read_dataset_spec(private, dataset_path)
            if spec.object_address != object_address or spec.chunks[dimension_index] != dimension_after:
                raise FormatError("corrected chunk dimension contradicts checked native schema")
            with ModernH5File(private) as reader:
                index = reader.read_index(
                    spec.object_address, spec.shape, spec.chunks, np.dtype(spec.dtype).itemsize,
                    maxshape=spec.maxshape, filters=spec.filters,
                )
                selected_ranges = [
                    (reader.absolute(chunk.address), reader.absolute(chunk.address) + chunk.size,
                     chunk.coordinate) for chunk in index.chunks
                ]
            inventory = inventory_other_allocations(private, spec.object_address)
            if not inventory.complete:
                raise UnsupportedCase("corrected trial lacks complete rooted ownership inventory")
            reject_sibling_overlap(selected_ranges, inventory)
            if sha256_file(snapshot) != digest:
                raise RecoveryError("source snapshot changed during metadata trial")
            _verify_source(source, identity, digest)
            trial_digest = sha256_file(private)
            os.link(private, trial)
            return HeaderDimensionCorrection(
                "selected_layout_chunk_dimension", physical, original_byte, value,
                original_checksum, dimension_index, dimension_before, dimension_after,
                object_address, digest, trial_digest,
            )
