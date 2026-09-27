"""Keep placement/current readability separate from prior-capture equality.

This module operates on the private *staged* output of a bounded route, before
the route worker publishes either destination. It never infers an old value
from a present HDF5 chunk, its checksum, a plausible physical address, or an
operator hint. The only accepted prior comparisons are made by routes which
already verify every exported unit against a separately retained capture.

An SHA-256 pin authenticates which operator-supplied capture was used. Neither
the pin nor this status map establishes when the capture was made or whether
the measurement was scientifically correct at capture time.
"""

from __future__ import annotations

import json
from math import prod
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .metadata import UnsupportedCase
from .recovery import RecoveryError


MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_STATUS_UNITS = 64 * 1024 * 1024
STATUS_PATH = "/_h5reclaim/historical_status"
STATUS_CODES = {
    "unknown": 0,
    "matches_operator_supplied_prior_capture": 1,
}

# These routes compare *each* accepted unit with an independent captured hash.
# Other routes may check a current HDF5 checksum or input/output equality, but
# those checks do not establish equality to bytes retained before the incident.
_CAPTURE_ROUTES: dict[str, tuple[str, str]] = {
    "prospective_chunk_baseline_reconciliation": ("baseline", "coordinate_decoded_chunk_sha256"),
    "prospective_element_baseline_reconciliation": ("baseline", "stored_element_sha256"),
    "prospective_recovery_capsule": ("capsule", "captured_physical_chunk_or_block_sha256"),
    "hash_pinned_replica_reconciliation": ("baseline", "coordinate_decoded_chunk_sha256"),
    "prospective_xor_parity_recovery": ("baseline", "coordinate_decoded_chunk_sha256"),
    "prospective_multi_erasure_recovery": ("baseline", "coordinate_decoded_chunk_sha256"),
}


def _capture(report: dict[str, Any]) -> tuple[str | None, str | None]:
    operation = report.get("operation")
    if operation not in _CAPTURE_ROUTES:
        return None, None
    artifact_name, comparison = _CAPTURE_ROUTES[operation]
    artifact = report.get(artifact_name)
    if not isinstance(artifact, dict):
        raise RecoveryError("prior-capture route omitted its capture evidence")
    digest = artifact.get("sha256")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)):
        raise RecoveryError("prior-capture route omitted its pinned SHA-256")
    return digest, comparison


def _source_map(report: dict[str, Any], handle: h5py.File,
                selected: h5py.Dataset) -> tuple[str | None, str | None]:
    validity = report.get("validity")
    candidates: list[str] = []
    if isinstance(validity, dict):
        for key in ("element_status", "dataset", "chunk_status"):
            value = validity.get(key)
            if isinstance(value, str) and value.startswith("/_h5reclaim/"):
                candidates.append(value)
    if isinstance(report.get("validity_map"), str):
        candidates.append(report["validity_map"])
    candidates.extend(("/_h5reclaim/element_status", "/_h5reclaim/chunk_status",
                       "/_h5reclaim/validity"))
    for path in candidates:
        if path in handle and isinstance(handle[path], h5py.Dataset):
            if path.endswith("element_status"):
                return path, "element"
            if path.endswith("validity") and handle[path].shape == selected.shape:
                return path, "element"
            return path, "chunk"
    return None, None


def _validate_map(source: h5py.Dataset, selected: h5py.Dataset,
                  granularity: str) -> None:
    if source.dtype != np.dtype("u1") or source.ndim > 4:
        raise RecoveryError("status map has an unsupported dtype or rank")
    if prod(source.shape) > MAX_STATUS_UNITS:
        raise UnsupportedCase("historical status map exceeds the unit limit")
    if granularity == "element":
        if source.shape != selected.shape:
            raise RecoveryError("element status shape contradicts selected dataset")
    elif selected.chunks is not None:
        expected = tuple((size + chunk - 1) // chunk
                         for size, chunk in zip(selected.shape, selected.chunks))
        if source.shape != expected:
            raise RecoveryError("chunk status grid contradicts selected dataset")
    elif source.shape != (1,):
        raise RecoveryError("nonchunked status map is not a bounded single unit")


def _copy_verified(source: h5py.Dataset, destination: h5py.Dataset) -> int:
    """Copy one-byte decisions in bounded strips; no full element-map read."""
    if source.ndim == 0:
        value = int(source[()] == 1)
        destination[()] = value
        return value
    stride = max(1, (1024 * 1024) // max(1, prod(source.shape[1:])))
    accepted = 0
    for first in range(0, source.shape[0], stride):
        selection = (slice(first, min(first + stride, source.shape[0])),)
        selection += (slice(None),) * (source.ndim - 1)
        chunk = np.asarray(source[selection], dtype="u1")
        if np.any(chunk > 7):
            raise RecoveryError("current status map contains an unknown code")
        accepted_chunk = np.asarray(chunk == 1, dtype="u1")
        destination[selection] = accepted_chunk
        accepted += int(np.count_nonzero(accepted_chunk))
    return accepted


def finalize_staged_history(output: str | Path, report_path: str | Path, *,
                            strict: bool = False) -> dict[str, Any]:
    """Annotate staged output, refusing unsupported evidence under strict mode.

    Strict mode does not publish an output when no prior-capture comparison
    route was selected. With a comparison route, all values it has not matched
    to its capture already remain unknown in that route's output. The caller
    must invoke this before its atomic publication of the staged files.
    """
    output, report_path = Path(output), Path(report_path)
    if output.is_symlink() or report_path.is_symlink() or not output.is_file() or not report_path.is_file():
        raise RecoveryError("historical status requires regular staged output and report files")
    if report_path.stat().st_size > MAX_REPORT_BYTES:
        raise UnsupportedCase("staged report exceeds historical status report limit")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise RecoveryError("staged report is not valid JSON") from exc
    if not isinstance(report, dict):
        raise RecoveryError("staged report must be a JSON object")
    digest, comparison = _capture(report)
    if strict and digest is None:
        raise UnsupportedCase(
            "strict historical integrity requires a separately retained prior capture; "
            "use a pinned chunk/element baseline, recovery capsule, replica, or parity route"
        )
    dataset_path = report.get("dataset", {}).get("path")
    if not isinstance(dataset_path, str) or not dataset_path.startswith("/"):
        raise RecoveryError("staged report omits the selected output dataset path")
    with h5py.File(output, "r+") as handle:
        if dataset_path not in handle or not isinstance(handle[dataset_path], h5py.Dataset):
            raise RecoveryError("staged output does not contain the reported dataset")
        if STATUS_PATH in handle:
            raise RecoveryError("staged output already has a historical status map")
        handle.require_group("/_h5reclaim")
        selected = handle[dataset_path]
        status_path, granularity = _source_map(report, handle, selected)
        current_status_path = status_path
        accepted = 0
        total: int | None = None
        map_omission: str | None = None
        if status_path is not None:
            source = handle[status_path]
            assert granularity is not None
            total = int(prod(source.shape))
            # A read-only classification of a very large current-value map
            # must not allocate another huge dataset merely to say unknown.
            if total > MAX_STATUS_UNITS and digest is None:
                status_path = None
                map_omission = "unprotected current-value map exceeds 64 million units; historical equality is unknown"
            else:
                _validate_map(source, selected, granularity)
                target = handle.create_dataset(STATUS_PATH, shape=source.shape, dtype="u1",
                                               chunks=source.chunks if source.chunks else None,
                                               fillvalue=0)
                if digest is not None:
                    accepted = _copy_verified(source, target)
                target.attrs["codes_json"] = json.dumps(STATUS_CODES, sort_keys=True)
                target.attrs["granularity"] = granularity
        elif strict:
            raise RecoveryError("prior-capture route has no selected-unit status map")
        else:
            total = int(prod(selected.shape))
            granularity = "element"
            map_omission = "this output has no per-unit current-value map; every element lacks prior-capture verification"
        if digest is not None:
            if status_path is None:
                raise RecoveryError("prior-capture route has no selected-unit status map")
            expected = report.get("accepted_elements") if granularity == "element" else (
                report.get("counts", {}).get("recovered")
            )
            if isinstance(expected, int) and accepted != expected:
                raise RecoveryError("prior-capture count contradicts historical status map")
        report["current_value_evidence"] = {
            "status_dataset": current_status_path,
            "meaning": (
                "Current HDF5 placement, decoding, or native readability; "
                "does not establish prior measurement bytes"
            ),
        }
        report["historical_integrity"] = {
            "policy": "require_prior_capture_match" if strict else "report_only",
            "prior_capture_match_status_dataset": STATUS_PATH if status_path is not None else None,
            "status_map_omission": map_omission,
            "codes": STATUS_CODES,
            "granularity": granularity,
            "matching_units": accepted,
            "unknown_units": total - accepted if total is not None else None,
            "capture_sha256": digest,
            "comparison": comparison,
            "capture_time_authenticated": False,
            "scientific_correctness_established": False,
            "note": (
                "Code 1 means equality to an operator-supplied, independently pinned capture, "
                "not proof of its date or scientific correctness. Code 0 means unknown "
                "where a historical status map exists; a missing map means no prior equality was established."
            ),
        }
        serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if len(serialized.encode("utf-8")) > MAX_REPORT_BYTES:
            raise UnsupportedCase("historical status report exceeds publication limit")
        if "/_h5reclaim/report_json" in handle:
            handle["/_h5reclaim/report_json"][()] = serialized
        else:
            handle["/_h5reclaim"].create_dataset(
                "report_json", data=serialized, dtype=h5py.string_dtype("utf-8"))
        handle.flush()
    report_path.write_text(serialized, encoding="utf-8")
    return report
