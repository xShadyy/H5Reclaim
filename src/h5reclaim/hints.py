"""Bounded, untrusted operator hints for a damaged HDF5 dataset.

Hints describe what a scientist expects. They are never evidence that a
particular payload belongs to that dataset or coordinate, and this module
does not read measurements or alter recovery decisions by itself.

The version-one JSON shape is::

    {
      "schema_version": 1,
      "dataset": {
        "path": "/measurements",
        "shape": [512, 512],
        "chunks": [16, 16],
        "dtype": "<u4",
        "filters": []
      },
      "source_sha256": "<SHA-256 of the damaged input, if known>",
      "note": "Copied from the acquisition log"
    }

Only ``dataset.path`` is required in ``dataset``. The SHA-256 is of the
*damaged* input file and can prevent applying notes to the wrong file. It is
not the checksum of its pre-damage contents. ``note`` is informational only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .metadata import DatasetSpec


MAX_HINT_FILE_BYTES = 65_536
MAX_RANK = 32
MAX_FILTERS = 32
MAX_DIMENSION = (1 << 64) - 1


class HintsError(ValueError):
    """An operator hint is malformed, contradictory, or conflicts with the file."""


@dataclass(frozen=True)
class DatasetHints:
    path: str
    shape: tuple[int, ...] | None = None
    chunks: tuple[int, ...] | None = None
    dtype: str | None = None
    filters: tuple[int, ...] | None = None
    source_sha256: str | None = None
    note: str | None = None

    @property
    def trust_level(self) -> str:
        """Claimed origin never elevates a hint to observed structural evidence."""
        return "unverified_operator_assertion"


@dataclass(frozen=True)
class HintComparison:
    field: str
    asserted: Any
    observed: Any | None
    status: str  # "matches", "conflicts", or "unobserved"


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HintsError(f"duplicate JSON field: {key[:96]!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise HintsError(f"nonstandard JSON constant: {value}")


def _field_names(data: Mapping[str, Any], allowed: set[str], context: str) -> None:
    extras = data.keys() - allowed
    if extras:
        raise HintsError(f"unknown {context} field: {sorted(extras)[0][:96]!r}")


def _dimensions(raw: Any, name: str, *, positive: bool) -> tuple[int, ...]:
    if not isinstance(raw, list) or not (1 <= len(raw) <= MAX_RANK):
        raise HintsError(f"dataset.{name} must be a list of 1 to {MAX_RANK} dimensions")
    minimum = 1 if positive else 0
    if any(type(item) is not int or not minimum <= item <= MAX_DIMENSION for item in raw):
        raise HintsError(f"dataset.{name} has an invalid dimension")
    return tuple(raw)


def parse_hints(content: bytes) -> DatasetHints:
    """Parse a strict hints file without interpreting it as recovery evidence."""
    if len(content) > MAX_HINT_FILE_BYTES:
        raise HintsError(f"hints file exceeds {MAX_HINT_FILE_BYTES} bytes")
    try:
        obj = json.loads(
            content.decode("utf-8"), object_pairs_hook=_unique_keys,
            parse_constant=_reject_constant,
        )
    except HintsError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise HintsError(f"hints file is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise HintsError("hints root must be a JSON object")
    _field_names(obj, {"schema_version", "dataset", "source_sha256", "note"}, "top-level")
    if type(obj.get("schema_version")) is not int or obj["schema_version"] != 1:
        raise HintsError("unsupported hints schema_version (expected 1)")
    dataset = obj.get("dataset")
    if not isinstance(dataset, dict):
        raise HintsError("dataset must be a JSON object")
    _field_names(dataset, {"path", "shape", "chunks", "dtype", "filters"}, "dataset")
    path = dataset.get("path")
    if (not isinstance(path, str) or not path.startswith("/") or path == "/"
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or len(path.encode("utf-8")) > 4096):
        raise HintsError("dataset.path must be an absolute HDF5 dataset path")
    if path == "/_h5reclaim" or path.startswith("/_h5reclaim/"):
        raise HintsError("the /_h5reclaim namespace is reserved")
    shape = _dimensions(dataset["shape"], "shape", positive=False) if "shape" in dataset else None
    chunks = _dimensions(dataset["chunks"], "chunks", positive=True) if "chunks" in dataset else None
    if shape is not None and chunks is not None and len(shape) != len(chunks):
        raise HintsError("dataset.shape and dataset.chunks must have the same rank")
    dtype = dataset.get("dtype")
    if "dtype" in dataset and (
        not isinstance(dtype, str) or not 1 <= len(dtype) <= 128
        or any(ord(char) < 32 or ord(char) == 127 for char in dtype)
    ):
        raise HintsError("dataset.dtype must be a short printable datatype descriptor")
    filters: tuple[int, ...] | None = None
    if "filters" in dataset:
        raw_filters = dataset["filters"]
        if (not isinstance(raw_filters, list) or len(raw_filters) > MAX_FILTERS or
                any(type(item) is not int or not 0 <= item <= 65535 for item in raw_filters)):
            raise HintsError("dataset.filters must be a list of up to 32 HDF5 filter IDs")
        filters = tuple(raw_filters)
    source_sha256 = obj.get("source_sha256")
    if "source_sha256" in obj and (
        not isinstance(source_sha256, str) or
        re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256) is None
    ):
        raise HintsError("source_sha256 must be 64 hexadecimal digits")
    note = obj.get("note")
    if "note" in obj and (
        not isinstance(note, str) or len(note.encode("utf-8")) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in note)
    ):
        raise HintsError("note must be UTF-8 text of at most 4096 bytes")
    return DatasetHints(
        path=path, shape=shape, chunks=chunks, dtype=dtype,
        filters=filters, source_sha256=source_sha256.lower() if source_sha256 else None,
        note=note,
    )


def load_hints(path: Path) -> DatasetHints:
    """Read no more than the maximum allowed bytes from a local hints file."""
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_HINT_FILE_BYTES + 1)
    return parse_hints(data)


def compare_hints(
    hints: DatasetHints,
    *,
    observed_dataset: DatasetSpec | None = None,
    observed_fields: Mapping[str, Any] | None = None,
    input_sha256: str | None = None,
) -> tuple[HintComparison, ...]:
    """Compare independent observations, without letting hints override them.

    An ``unobserved`` field is not a match, and matching metadata does not
    verify the identity or integrity of any data chunk. Callers must refuse
    conflicting hints before exporting; missing evidence stays unresolved.
    """
    if observed_dataset is not None and observed_fields is not None:
        raise HintsError("provide observed_dataset or observed_fields, not both")
    observations: dict[str, Any] = {}
    if observed_dataset is not None:
        observations.update(
            path=observed_dataset.path,
            shape=observed_dataset.shape,
            chunks=observed_dataset.chunks,
            dtype=observed_dataset.dtype,
            filters=observed_dataset.filters,
        )
    if observed_fields is not None:
        _field_names(observed_fields, {"path", "shape", "chunks", "dtype", "filters"},
                     "observed dataset")
        for field, value in observed_fields.items():
            if field in ("shape", "chunks", "filters") and value is not None:
                if not isinstance(value, (tuple, list)) or any(type(item) is not int for item in value):
                    raise HintsError(f"observed {field} must be an integer sequence")
                value = tuple(value)
            if field in ("path", "dtype") and value is not None and not isinstance(value, str):
                raise HintsError(f"observed {field} must be a string")
            observations[field] = value
    if input_sha256 is not None:
        observations["source_sha256"] = input_sha256.lower()
    comparisons = []
    for field in ("path", "shape", "chunks", "dtype", "filters", "source_sha256"):
        asserted = getattr(hints, field)
        if asserted is None:
            continue
        observed = observations.get(field)
        status = "unobserved" if observed is None else (
            "matches" if asserted == observed else "conflicts"
        )
        comparisons.append(HintComparison(field, asserted, observed, status))
    return tuple(comparisons)


def require_no_conflicts(comparisons: tuple[HintComparison, ...]) -> None:
    """Fail closed on mismatches; unobserved values remain mere assertions."""
    conflicts = [item.field for item in comparisons if item.status == "conflicts"]
    if conflicts:
        raise HintsError("operator hints conflict with observed " + ", ".join(conflicts))
