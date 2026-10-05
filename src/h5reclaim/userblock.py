"""Retain an application header outside HDF5's address space."""

from pathlib import Path
import hashlib

from .format import SIGNATURE
from .recovery import RecoveryError


def userblock_size(source):
    source = Path(source)
    size, offset = source.stat().st_size, 0
    with source.open('rb') as stream:
        while offset + 8 <= size:
            stream.seek(offset)
            if stream.read(8) == SIGNATURE:
                return offset
            offset = 512 if offset == 0 else offset * 2
    # A checksum-justified disposable signature correction still identifies
    # the preceding application header in the unchanged physical source.
    from .metadata_correction import _superblock_raw
    from .format import FormatError
    from .metadata import UnsupportedCase
    try:
        position, _raw, _width, _root = _superblock_raw(source)
        return position
    except (FormatError, UnsupportedCase):
        pass
    return 0


def matlab_header(source):
    with Path(source).open('rb') as stream:
        return stream.read(20).startswith(b'MATLAB 7.3 MAT-file')


def copy_userblock(source, output, size, block_bytes=1024**2):
    if not size:
        return {'size_bytes': 0, 'sha256': None}
    digest = hashlib.sha256()
    with Path(source).open('rb') as original, Path(output).open('r+b') as target:
        remaining = size
        while remaining:
            block = original.read(min(remaining, block_bytes))
            if not block:
                raise RecoveryError('application user block ended unexpectedly')
            # A newly created HDF5 user block is zero-filled. Leave empty
            # blocks sparse while still hashing and verifying every byte.
            if block.count(0) != len(block):
                target.write(block)
            else:
                target.seek(len(block), 1)
            digest.update(block)
            remaining -= len(block)
        target.flush()
    checked = hashlib.sha256()
    with Path(output).open('rb') as target:
        remaining = size
        while remaining:
            block = target.read(min(remaining, block_bytes))
            if not block:
                raise RecoveryError('output application user block is truncated')
            checked.update(block)
            remaining -= len(block)
    if checked.digest() != digest.digest():
        raise RecoveryError('application user block readback differs')
    return {'size_bytes': size, 'sha256': digest.hexdigest()}
