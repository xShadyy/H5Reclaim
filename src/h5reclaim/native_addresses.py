"""Normalize native chunk offsets across installed HDF5 API versions."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import secrets
import tempfile

import h5py
import numpy as np

from .metadata import UnsupportedCase


@lru_cache(maxsize=1)
def _relative_chunk_offsets():
    """Observe whether this runtime includes the file user block in offsets.

    Older HDF5 returns base-relative chunk offsets; newer releases return
    physical offsets. A disposable, known-payload fixture determines the
    installed API's behavior without guessing from its version number.
    """
    marker = secrets.token_bytes(64)
    with tempfile.TemporaryDirectory(prefix="h5reclaim-address-api-") as directory:
        path = Path(directory) / "probe.h5"
        with h5py.File(path, "w", userblock_size=512) as handle:
            dataset = handle.create_dataset("probe", data=np.frombuffer(marker, dtype="u1"), chunks=(64,))
            handle.flush()
            offset = int(dataset.id.get_chunk_info_by_coord((0,)).byte_offset)
        raw = path.read_bytes()
        physical = raw[offset:offset + 64] == marker
        relative = raw[offset + 512:offset + 576] == marker
        if physical == relative:
            raise UnsupportedCase("installed native chunk-address semantics could not be determined")
        return relative


def chunk_address(dataset, reported):
    if reported is None:
        return None
    userblock = dataset.file.id.get_create_plist().get_userblock()
    return int(reported) + (int(userblock) if userblock and _relative_chunk_offsets() else 0)
