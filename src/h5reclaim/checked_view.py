"""Repair uniquely checksum-justified superblock bytes in a disposable view."""

from contextlib import contextmanager
from pathlib import Path
import tempfile
import time

import h5py

from .format import FormatError, SIGNATURE
from .large_streaming import LargeBudget, _copy_sparse
from .modern_indexes import ModernH5File, lookup3
from .metadata import UnsupportedCase


@contextmanager
def checked_root_view(source, budget=None):
    from .metadata_correction import _superblock_raw, _unique_one_byte
    from .metadata_fallback import _messages, _unique
    source = Path(source)
    budget = budget or LargeBudget()
    try:
        position, raw, width, root_start = _superblock_raw(source)
    except UnsupportedCase:
        yield source, None
        return
    checksum = int.from_bytes(raw[-4:], 'little')
    if lookup3(raw[:-4]) == checksum:
        yield source, None
        return
    corrected = bytearray(raw)
    if raw[:8] != SIGNATURE:
        differences = [index for index in range(8) if raw[index] != SIGNATURE[index]]
        if len(differences) != 1 or lookup3(SIGNATURE + raw[8:-4]) != checksum:
            raise FormatError('signature restoration lacks a unique original-checksum match')
        offset = differences[0]
        value = SIGNATURE[offset]
        kind = 'unique_superblock_signature_checksum_correction'
    else:
        offset, value = _unique_one_byte(raw, range(root_start, root_start + width), checksum)
        kind = 'unique_root_pointer_checksum_correction'
    corrected[offset] = value
    root = int.from_bytes(corrected[root_start:root_start + width], 'little')
    with tempfile.TemporaryDirectory(prefix='h5reclaim-checked-root-') as directory:
        view = Path(directory) / 'view.h5'
        with source.open('rb') as original, view.open('w+b') as target:
            _copy_sparse(original, target, size=source.stat().st_size, parent=Path(directory),
                         budget=budget, deadline=time.monotonic() + budget.max_seconds)
            target.seek(position)
            target.write(corrected)
        with ModernH5File(view) as reader:
            messages = _messages(reader, root)
            if _unique(messages, 1) is not None or _unique(messages, 8) is not None:
                raise FormatError('corrected root is a dataset rather than a group')
            if _unique(messages, 2) is None or _unique(messages, 10) is None:
                raise FormatError('corrected root has no checked modern group metadata')
        with h5py.File(view, 'r') as handle:
            if int(h5py.h5o.get_info(handle['/'].id).addr) != root:
                raise FormatError('native root address contradicts the corrected pointer')
        yield view, {'kind': kind,
                     'physical_byte': position + offset, 'before': raw[offset], 'after': value,
                     'root_address': root, 'original_checksum': checksum,
                     'checksum_bytes_changed': 0}
