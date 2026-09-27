"""Prospective per-element integrity evidence for rooted numeric storage.

Only capture made before damage can distinguish an unchanged measurement from
an unchecksummed altered payload. This route keeps matching complete elements
at their selected rooted layout coordinates, and marks mismatches unknown. It
never estimates or rewrites a missing measurement from neighboring values.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .format import FormatError, UnsupportedFormat
from .nonchunked_recovery import (
    MAX_ELEMENTS, NonchunkedAnalysis, STATUS_CODES, analyze_nonchunked_snapshot,
)
from .recovery import (
    RecoveryError, _identity, _validate_paths, _verify_source, sha256_file,
    source_snapshot,
)


MAX_BASELINE_BYTES = 32 * MAX_ELEMENTS + 8192
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_RUNS = 65536
_HASH_ENTRY = "element_sha256.bin"
_MANIFEST_ENTRY = "manifest.json"


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise RecoveryError("baseline digest must be a lowercase SHA-256 hex string")
    return value


def _schema(analysis: NonchunkedAnalysis) -> dict[str, Any]:
    spec = analysis.spec
    return {
        "path": spec.path, "shape": list(spec.shape), "dtype": spec.dtype,
        "layout": spec.layout, "element_size": spec.element_size,
        "elements": spec.elements,
    }


def _aliases(paths: list[Path], targets: list[Path]) -> None:
    resolved = [path.resolve(strict=True) for path in paths]
    for i, path in enumerate(resolved):
        if any(path.samefile(other) for other in resolved[i + 1:]):
            raise RecoveryError("source and baseline must be distinct regular files")
    for target in targets:
        if target.resolve(strict=False) in resolved:
            raise RecoveryError("destination aliases a source or baseline input")


def _hash_elements(data: bytes, width: int, count: int) -> bytes:
    if len(data) != width * count:
        raise RecoveryError("element hashing requires complete stored bytes")
    digest = bytearray(32 * count)
    for index in range(count):
        digest[index * 32:(index + 1) * 32] = hashlib.sha256(
            data[index * width:(index + 1) * width]
        ).digest()
    return bytes(digest)


def capture_element_baseline(
    source: str | Path, dataset_path: str, destination: str | Path,
) -> dict[str, Any]:
    """Capture hash evidence in a separate uncompressed, bounded ZIP sidecar.

    The operator must independently retain this file and its returned SHA-256
    before an incident. A later hash comparison does not authenticate the
    capture date or prove that the original scientific values were correct.
    """
    source, destination = Path(source), Path(destination)
    if not destination.parent.is_dir() or destination.exists() or destination.is_symlink():
        raise RecoveryError("baseline destination parent must exist and destination must be new")
    if source.resolve(strict=True) == destination.resolve(strict=False):
        raise RecoveryError("baseline destination aliases the source")
    with source_snapshot(source) as (snapshot, source_hash, identity, size):
        analysis = analyze_nonchunked_snapshot(
            snapshot, dataset_path, source, source_hash, identity, size,
        )
        if not analysis.report["complete"]:
            raise UnsupportedFormat("baseline capture requires a complete selected allocation")
        schema = _schema(analysis)
        hashes = _hash_elements(analysis.recovered_bytes, schema["element_size"], schema["elements"])
        manifest = {
            "schema_version": 1, "kind": "prospective_nonchunked_element_hashes",
            "source_sha256": source_hash, "dataset": schema,
            "digest_algorithm": "sha256-per-stored-element", "digest_entry": _HASH_ENTRY,
            "digest_sha256": hashlib.sha256(hashes).hexdigest(),
            "trust_note": (
                "This separately retained capture identifies bytes matching the observed source "
                "at capture time; it does not authenticate the capture date or scientific history."
            ),
        }
        encoded = _canonical_json(manifest)
        if len(encoded) > 8192 or len(hashes) + len(encoded) + 2048 > MAX_BASELINE_BYTES:
            raise UnsupportedFormat("element baseline exceeds its bounded sidecar limit")
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-elements-", dir=destination.parent) as tmp:
            staged = Path(tmp) / "element-baseline.zip"
            with zipfile.ZipFile(staged, "x", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr(_MANIFEST_ENTRY, encoded, compress_type=zipfile.ZIP_STORED)
                archive.writestr(_HASH_ENTRY, hashes, compress_type=zipfile.ZIP_STORED)
            _verify_source(source, identity, source_hash)
            if sha256_file(snapshot) != source_hash:
                raise RecoveryError("private source snapshot changed during baseline capture")
            if destination.exists() or destination.is_symlink():
                raise RecoveryError("element baseline destination already exists")
            os.link(staged, destination)
        return {**manifest, "archive_sha256": sha256_file(destination)}


def _load_baseline(path: Path, expected_digest: str, analysis: NonchunkedAnalysis) -> tuple[bytes, dict[str, Any], tuple[int, int, int, int, int]]:
    if not path.is_file():
        raise RecoveryError("element baseline must be a regular file")
    stat = path.stat()
    identity = _identity(stat)
    if stat.st_size > MAX_BASELINE_BYTES or sha256_file(path) != _digest(expected_digest):
        raise RecoveryError("element baseline is oversized or differs from the retained SHA-256")
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.namelist() != [_MANIFEST_ENTRY, _HASH_ENTRY]:
                raise RecoveryError("element baseline needs exactly two ordered entries")
            entries = [archive.getinfo(name) for name in (_MANIFEST_ENTRY, _HASH_ENTRY)]
            if any(entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits & 1 for entry in entries):
                raise RecoveryError("element baseline contains compressed or encrypted entries")
            if entries[0].file_size > 8192 or entries[1].file_size > 32 * MAX_ELEMENTS:
                raise RecoveryError("element baseline metadata or digest count is invalid")
            def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError(f"duplicate JSON key: {key}")
                    result[key] = value
                return result
            manifest = json.loads(archive.read(_MANIFEST_ENTRY).decode("utf-8"),
                                  object_pairs_hook=unique)
            hashes = archive.read(_HASH_ENTRY)
    except RecoveryError:
        raise
    except (zipfile.BadZipFile, UnicodeError, ValueError, RuntimeError) as exc:
        raise RecoveryError(f"element baseline is invalid: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "kind", "source_sha256", "dataset", "digest_algorithm",
        "digest_entry", "digest_sha256", "trust_note",
    } or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise RecoveryError("element baseline has an unsupported manifest schema")
    if manifest["kind"] != "prospective_nonchunked_element_hashes" \
       or manifest["digest_algorithm"] != "sha256-per-stored-element" \
       or manifest["digest_entry"] != _HASH_ENTRY or manifest["dataset"] != _schema(analysis):
        raise RecoveryError("element baseline dataset schema contradicts current rooted metadata")
    if len(hashes) != 32 * analysis.spec.elements:
        raise RecoveryError("element baseline digest count contradicts current dataset")
    _digest(manifest["source_sha256"])
    if hashlib.sha256(hashes).hexdigest() != _digest(manifest["digest_sha256"]):
        raise RecoveryError("element baseline digest entry disagrees with its manifest")
    if _identity(path.stat()) != identity or sha256_file(path) != expected_digest or _identity(path.stat()) != identity:
        raise RecoveryError("element baseline changed while it was read")
    return hashes, manifest, identity


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, is_present in enumerate(mask):
        if bool(is_present) and start is None:
            start = index
        elif not bool(is_present) and start is not None:
            spans.append((start, index))
            start = None
        if len(spans) > MAX_RUNS:
            raise UnsupportedFormat("element integrity alternates in too many separate regions")
    if start is not None:
        spans.append((start, len(mask)))
    if len(spans) > MAX_RUNS:
        raise UnsupportedFormat("element integrity alternates in too many separate regions")
    return spans


def _verified_analysis(analysis: NonchunkedAnalysis, hashes: bytes,
                       manifest: dict[str, Any], baseline_path: Path,
                       baseline_sha256: str) -> tuple[np.ndarray, bytes, dict[str, Any]]:
    spec = analysis.spec
    status = np.zeros(spec.elements, dtype="u1")
    raw = analysis.recovered_bytes
    width = spec.element_size
    physically_present = len(raw) // width
    accepted = bytearray(raw)
    for index in range(physically_present):
        current = hashlib.sha256(raw[index * width:(index + 1) * width]).digest()
        if current == hashes[index * 32:(index + 1) * 32]:
            status[index] = STATUS_CODES["recovered"]
        else:
            accepted[index * width:(index + 1) * width] = b"\0" * width
    present_runs = _runs(status == STATUS_CODES["recovered"])
    rejected_present_runs = _runs(status[:physically_present] == STATUS_CODES["unknown"])
    offset = spec.source_absolute_offset
    mappings = [{
        "first_linear_element": start, "element_count": stop - start,
        "source_absolute_offset": offset + start * width,
        "size_bytes": (stop - start) * width,
        "sha256_of_damaged_snapshot_bytes": hashlib.sha256(raw[start * width:stop * width]).hexdigest(),
        "coordinate_rule": "C row-major: offset + linear_element * element_size",
        "integrity": "matches_operator_supplied_prior_capture_sha256_per_element",
    } for start, stop in present_runs]
    unresolved = [{
        "first_linear_element": start, "element_count": stop - start,
        "reason": "current stored element differs from prior capture",
        "source_absolute_offset": offset + start * width,
    } for start, stop in rejected_present_runs]
    if physically_present < spec.elements:
        unresolved.append({
            "first_linear_element": physically_present,
            "element_count": spec.elements - physically_present,
            "reason": "physically missing allocation bytes",
            "source_absolute_offset": None,
        })
    report = analysis.report.copy()
    report["operation"] = "prospective_element_baseline_reconciliation"
    recovered = int(np.count_nonzero(status))
    report["complete"] = recovered == spec.elements
    report["outcome"] = "complete" if report["complete"] else "partial"
    report["counts"] = {"recovered": recovered, "unknown": spec.elements - recovered}
    report["mappings"] = mappings
    report["unresolved_elements"] = unresolved
    report["baseline"] = {
        "path": str(baseline_path), "sha256": baseline_sha256,
        "captured_source_sha256": manifest["source_sha256"],
        "comparison": "stored element bytes against independently retained capture hashes",
    }
    report["evidence_ledger"] = {
        **report["evidence_ledger"], "physical_ranges": mappings,
        "decision": "accept a complete rooted element only if its bytes match its prior captured hash",
    }
    report["integrity_note"] = (
        "Matching stored bytes agree with an operator supplied prior capture. This does not "
        "authenticate the capture date or establish that the measured values were correct "
        "before capture. Mismatches and missing bytes are unknown, never repaired from hashes."
    )
    return status.reshape(spec.shape), bytes(accepted), report


def export_verified_nonchunked(
    source: str | Path, dataset_path: str, baseline_path: str | Path,
    baseline_sha256: str, output: str | Path, report_path: str | Path,
) -> dict[str, Any]:
    """Export only currently present numeric elements matching prior hashes."""
    source, baseline_path = Path(source), Path(baseline_path)
    output, report_path = Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    _aliases([source, baseline_path], [output, report_path])
    with source_snapshot(source) as (snapshot, source_hash, identity, size):
        analysis = analyze_nonchunked_snapshot(snapshot, dataset_path, source, source_hash, identity, size)
        hashes, manifest, baseline_identity = _load_baseline(baseline_path, baseline_sha256, analysis)
        status, raw, report = _verified_analysis(
            analysis, hashes, manifest, baseline_path, baseline_sha256,
        )
        serialized = _canonical_json(report)
        if len(serialized) > MAX_REPORT_BYTES:
            raise UnsupportedFormat("element integrity report exceeds publication limit")
        if sha256_file(snapshot) != source_hash:
            raise RecoveryError("private source snapshot changed during analysis")
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-elements-", dir=output.parent) as out_tmp:
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-elements-", dir=report_path.parent) as rep_tmp:
                staged = Path(out_tmp) / "output.h5"
                staged_report = Path(rep_tmp) / "report.json"
                published = False
                try:
                    with h5py.File(staged, "x") as file:
                        spec = analysis.spec
                        values = np.zeros(spec.elements, dtype=spec.dtype)
                        if raw:
                            values[:len(raw) // spec.element_size] = np.frombuffer(raw, dtype=spec.dtype)
                        if spec.layout == "compact":
                            parent_name, name = spec.path.rsplit("/", 1)
                            parent = file.require_group(parent_name or "/")
                            space = (h5py.h5s.create(h5py.h5s.SCALAR) if not spec.shape else
                                     h5py.h5s.create_simple(spec.shape))
                            dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                            dcpl.set_layout(h5py.h5d.COMPACT)
                            dataset = h5py.Dataset(h5py.h5d.create(
                                parent.id, name.encode("utf-8"),
                                h5py.h5t.py_create(np.dtype(spec.dtype)), space, dcpl=dcpl,
                            ))
                        else:
                            dataset = file.create_dataset(spec.path, shape=spec.shape, dtype=spec.dtype)
                        dataset[...] = values.reshape(spec.shape)
                        meta = file.create_group("/_h5reclaim")
                        validity = meta.create_dataset("element_status", data=status, dtype="u1")
                        validity.attrs["codes_json"] = json.dumps(STATUS_CODES, sort_keys=True)
                        validity.attrs["axis_meaning"] = "one entry per selected dataset element"
                        meta.create_dataset("report_json", data=serialized.decode("utf-8"),
                                            dtype=h5py.string_dtype("utf-8"))
                        dataset.attrs["h5reclaim_element_status"] = "/_h5reclaim/element_status"
                        dataset.attrs["h5reclaim_complete"] = report["complete"]
                        dataset.attrs["h5reclaim_warning"] = (
                            "Unknown output elements read as zero but are not known measurements. "
                            "Check element_status and the prior capture evidence."
                        )
                        meta.attrs["source_sha256"] = source_hash
                        meta.attrs["report_schema_version"] = 1
                        file.flush()
                    with h5py.File(staged, "r") as file:
                        observed = file[dataset_path].astype(np.dtype(analysis.spec.dtype))[...]
                        if observed.tobytes(order="C")[:len(raw)] != raw:
                            raise RecoveryError("verified output bit patterns changed during export")
                    staged_report.write_bytes(serialized)
                    _verify_source(source, identity, source_hash)
                    _verify_source(baseline_path, baseline_identity, baseline_sha256)
                    _validate_paths(source, output, report_path)
                    _aliases([source, baseline_path], [output, report_path])
                    os.link(staged, output)
                    published = True
                    os.link(staged_report, report_path)
                except Exception:
                    if published:
                        output.unlink(missing_ok=True)
                    raise
        return report
