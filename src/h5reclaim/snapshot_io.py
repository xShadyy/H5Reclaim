"""Bounded streaming copy of a source into a private analysis image.

This module knows nothing about HDF5. The caller establishes source identity
before copying and checks it again afterwards; the image is never published.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


MIB = 1024 * 1024
GIB = 1024 * MIB


class SnapshotBudgetError(ValueError):
    """An explicit copy, disk-space, or elapsed-time quota was exceeded."""


class SnapshotSourceChanged(ValueError):
    """The opened source grew during its private copy."""


@dataclass(frozen=True)
class SnapshotBudget:
    """Resource bounds for one full, consistent source image.

    The copy is intentionally local: neither this quota nor the size check
    establishes available memory, remote-file stability, or historical data
    authenticity. The caller must still verify input identity and digest.
    """

    max_source_bytes: int = 4 * GIB
    max_seconds: float = 1800.0
    block_bytes: int = MIB
    disk_reserve_bytes: int = 32 * MIB

    def __post_init__(self) -> None:
        if not isinstance(self.max_source_bytes, int) or self.max_source_bytes <= 0:
            raise ValueError("max_source_bytes must be a positive integer")
        if not 0 < self.max_seconds <= 24 * 60 * 60:
            raise ValueError("max_seconds must be between zero and 24 hours")
        if not 0 < self.block_bytes <= 4 * MIB:
            raise ValueError("block_bytes must be between one byte and 4 MiB")
        if self.disk_reserve_bytes < 0:
            raise ValueError("disk_reserve_bytes must be nonnegative")


def copy_and_hash(
    source: BinaryIO,
    target: BinaryIO,
    *,
    expected_size: int,
    target_parent: Path,
    budget: SnapshotBudget,
) -> tuple[str, int]:
    """Copy in small blocks under explicit byte, disk, and time bounds.

    A full image is necessary here because native HDF5 and the raw parser
    must inspect identical bytes. Sparse source holes are copied as zeros;
    disk space is reserved for the full logical length, not apparent usage.
    """
    if expected_size < 0 or expected_size > budget.max_source_bytes:
        raise SnapshotBudgetError(
            f"input exceeds the {budget.max_source_bytes}-byte snapshot limit"
        )
    available = shutil.disk_usage(target_parent).free
    if available < expected_size + budget.disk_reserve_bytes:
        raise SnapshotBudgetError(
            "insufficient free space for a private full-size source snapshot"
        )
    digest = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + budget.max_seconds
    while True:
        if time.monotonic() > deadline:
            raise SnapshotBudgetError("source snapshot exceeded the elapsed-time limit")
        block = source.read(budget.block_bytes)
        if not block:
            break
        total += len(block)
        if total > expected_size:
            raise SnapshotSourceChanged("input grew while making a read-only snapshot")
        if total > budget.max_source_bytes:
            raise SnapshotBudgetError("input grew beyond the source snapshot limit")
        target.write(block)
        digest.update(block)
    if time.monotonic() > deadline:
        raise SnapshotBudgetError("source snapshot exceeded the elapsed-time limit")
    return digest.hexdigest(), total
