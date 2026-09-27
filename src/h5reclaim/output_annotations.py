"""Add tool convenience attributes without replacing source attributes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import h5py


def add_output_annotations(dataset: h5py.Dataset, values: Mapping[str, Any]) -> list[str]:
    """Write available names and return source-owned names left untouched.

    The validity datasets under ``/_h5reclaim`` remain the authoritative
    status even when a scientist already used one of these attribute names.
    Callers should include returned names in their recovery report.
    """
    collisions = sorted(name for name in values if name in dataset.attrs)
    for name, value in values.items():
        if name not in collisions:
            dataset.attrs[name] = value
    return collisions
