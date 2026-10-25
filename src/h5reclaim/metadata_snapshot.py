"""Restore captured, checked metadata bytes when everything else is unchanged.

This prospective full-file snapshot is different from a selected-dataset
recovery capsule: it stores the raw reachable metadata ranges of an entire
healthy HDF5 file, including the superblock, object messages and checked
indexes. An independently retained archive hash anchors the earlier capture.
Uncaptured bytes, including measurements and unsupported metadata, must still
match exactly. No missing historical bytes are guessed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from .large_streaming import LargeBudget, sparse_snapshot
from .metadata import UnsupportedCase
from .preflight import inspect_metadata_local
from .recovery import RecoveryError, VERSION, _validate_paths, _verify_source, sha256_file

MANIFEST = "manifest.json"
METADATA = "metadata.bin"
SCHEMA_VERSION = 1
MAX_MANIFEST = 8 << 20


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise RecoveryError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise RecoveryError(f"duplicate metadata snapshot key: {name}")
        result[name] = value
    return result


def _ranges(items: object, size: int, budget: LargeBudget) -> list[tuple[int, int, str]]:
    if not isinstance(items, list) or not 1 <= len(items) <= budget.max_objects:
        raise RecoveryError("metadata snapshot has an invalid range count")
    result = []
    for item in items:
        if (not isinstance(item, (list, tuple)) or len(item) != 3
                or type(item[0]) is not int or type(item[1]) is not int
                or not isinstance(item[2], str) or not 0 < len(item[2]) <= 120
                or not 0 <= item[0] < item[1] <= size):
            raise RecoveryError("metadata snapshot has an invalid range")
        result.append((item[0], item[1], item[2]))
    result.sort()
    if "superblock" not in result[0][2]:
        raise RecoveryError("metadata snapshot must cover the HDF5 signature and superblock")
    # Preflight can record an enclosing range and its checked child. One
    # canonical interval must never copy the same byte from the archive twice.
    merged: list[tuple[int, int, str]] = []
    for start, stop, name in result:
        if merged and start < merged[-1][1]:
            previous = merged[-1]
            merged[-1] = (previous[0], max(previous[1], stop), previous[2])
        else:
            merged.append((start, stop, name))
    if sum(stop - start for start, stop, _ in merged) > budget.max_metadata_bytes:
        raise UnsupportedCase("checked metadata exceeds the capture budget")
    if size - sum(stop - start for start, stop, _ in merged) > budget.max_scan_bytes:
        raise UnsupportedCase("uncaptured source bytes exceed the comparison budget")
    return merged


def _complement_sha256(path: Path, intervals: list[tuple[int, int, str]], size: int, block: int) -> str:
    digest = hashlib.sha256()
    cursor = 0
    with path.open("rb", buffering=0) as stream:
        for start, stop, _ in [*intervals, (size, size, "end")]:
            stream.seek(cursor)
            while cursor < start:
                data = stream.read(min(block, start - cursor))
                if not data:
                    raise RecoveryError("source ended while hashing uncaptured bytes")
                digest.update(data)
                cursor += len(data)
            cursor = stop
    return digest.hexdigest()


def _new_path(path: Path, label: str) -> None:
    if not path.parent.is_dir() or path.exists() or path.is_symlink():
        raise RecoveryError(f"{label} parent must exist and destination must be new")


def capture_metadata_snapshot(source: str | Path, archive: str | Path, *, budget: LargeBudget | None = None) -> dict:
    """Capture checked full-file metadata ranges from a healthy local file."""
    source, archive, budget = Path(source), Path(archive), budget or LargeBudget()
    _new_path(archive, "snapshot")
    if source.resolve(strict=True) == archive.resolve(strict=False):
        raise RecoveryError("metadata snapshot aliases its source")
    with sparse_snapshot(source, budget=budget) as (image, source_sha, identity, size, copied):
        result = inspect_metadata_local(image, budget=budget, profile="strict")
        if result["decision"] != "accept" or not result["analysis"]["complete"]:
            raise UnsupportedCase("healthy capture needs a complete, accepted strict raw preflight")
        intervals = _ranges(result["metadata_ranges"], size, budget)
        with image.open("rb", buffering=0) as handle:
            ranges_data = []
            for start, stop, name in intervals:
                handle.seek(start)
                raw = handle.read(stop - start)
                if len(raw) != stop - start:
                    raise RecoveryError("short checked metadata capture")
                ranges_data.append(raw)
        metadata = b"".join(ranges_data)
        document = {
            "schema_version": SCHEMA_VERSION, "kind": "checked_full_file_metadata_snapshot",
            "source_size": size, "source_sha256": source_sha,
            "complement_sha256": _complement_sha256(image, intervals, size, budget.block_bytes),
            "metadata_sha256": hashlib.sha256(metadata).hexdigest(),
            "metadata_ranges": [[start, stop, name] for start, stop, name in intervals],
            "covered_objects": result["analysis"]["objects_checked"],
            "coverage": "only independently checked reachable metadata ranges; every other byte must remain unchanged",
        }
        encoded = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if len(encoded) > MAX_MANIFEST:
            raise UnsupportedCase("metadata snapshot manifest exceeds its bound")
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-metadata-", dir=archive.parent) as directory:
            staged = Path(directory) / "metadata.zip"
            with zipfile.ZipFile(staged, "x", compression=zipfile.ZIP_STORED) as output:
                output.writestr(MANIFEST, encoded)
                output.writestr(METADATA, metadata)
            if staged.stat().st_size > MAX_MANIFEST + budget.max_metadata_bytes + 4096:
                raise UnsupportedCase("metadata snapshot archive exceeds its bound")
            if sha256_file(image) != source_sha:
                raise RecoveryError("healthy source snapshot changed during capture")
            _verify_source(source, identity, source_sha)
            _new_path(archive, "snapshot")
            os.link(staged, archive)
    return {**document, "archive_sha256": sha256_file(archive), "physical_source_bytes_copied": copied}


def _load(archive: Path, expected_sha256: str, budget: LargeBudget) -> tuple[dict, bytes]:
    expected = _digest(expected_sha256, "independently retained snapshot SHA-256")
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > MAX_MANIFEST + budget.max_metadata_bytes + 4096:
        raise RecoveryError("metadata snapshot is absent or exceeds the size quota")
    if sha256_file(archive) != expected:
        raise RecoveryError("metadata snapshot differs from the independently retained digest")
    try:
        with zipfile.ZipFile(archive) as reader:
            if reader.namelist() != [MANIFEST, METADATA]:
                raise RecoveryError("metadata snapshot needs exactly its manifest and metadata bytes")
            records = [reader.getinfo(name) for name in (MANIFEST, METADATA)]
            if (any(entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits & 1 for entry in records)
                    or records[0].file_size > MAX_MANIFEST
                    or records[1].file_size > budget.max_metadata_bytes):
                raise RecoveryError("metadata snapshot uses an unsupported encoding or size")
            document = json.loads(reader.read(MANIFEST).decode("utf-8"), object_pairs_hook=_unique)
            metadata = reader.read(METADATA)
    except RecoveryError:
        raise
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, RuntimeError) as exc:
        raise RecoveryError(f"metadata snapshot cannot be read: {exc}") from exc
    keys = {"schema_version", "kind", "source_size", "source_sha256", "complement_sha256",
            "metadata_sha256", "metadata_ranges", "covered_objects", "coverage"}
    if (not isinstance(document, dict) or set(document) != keys or
            type(document["schema_version"]) is not int or document["schema_version"] != SCHEMA_VERSION or
            document["kind"] != "checked_full_file_metadata_snapshot" or
            type(document["source_size"]) is not int or not 0 < document["source_size"] <= budget.max_source_bytes or
            type(document["covered_objects"]) is not int or document["covered_objects"] < 1 or
            document["coverage"] != "only independently checked reachable metadata ranges; every other byte must remain unchanged"):
        raise RecoveryError("metadata snapshot manifest schema is invalid")
    for field in ("source_sha256", "complement_sha256", "metadata_sha256"):
        _digest(document[field], field)
    intervals = _ranges(document["metadata_ranges"], document["source_size"], budget)
    if (document["metadata_ranges"] != [[start, stop, label] for start, stop, label in intervals]
            or len(metadata) != sum(stop - start for start, stop, _ in intervals)
            or hashlib.sha256(metadata).hexdigest() != document["metadata_sha256"]
            or sha256_file(archive) != expected):
        raise RecoveryError("metadata snapshot bytes, intervals or archive digest disagree")
    return document, metadata


def restore_metadata_snapshot(source: str | Path, archive: str | Path, expected_sha256: str,
                              output: str | Path, report_path: str | Path,
                              *, budget: LargeBudget | None = None) -> dict:
    """Restore only captured bytes to a new copy if uncaptured bytes match."""
    source, archive, output, report_path = map(Path, (source, archive, output, report_path))
    budget = budget or LargeBudget()
    _validate_paths(source, output, report_path)
    if (archive.resolve(strict=True) == source.resolve(strict=True)
            or archive.resolve(strict=True) in (output.resolve(strict=False), report_path.resolve(strict=False))
            or output.resolve(strict=False) == report_path.resolve(strict=False)):
        raise RecoveryError("snapshot, source and publication paths must be separate")
    document, metadata = _load(archive, expected_sha256, budget)
    with sparse_snapshot(source, budget=budget) as (image, damaged_sha, identity, size, copied):
        if size != document["source_size"]:
            raise RecoveryError("damaged file size differs from the captured source")
        intervals = _ranges(document["metadata_ranges"], size, budget)
        if _complement_sha256(image, intervals, size, budget.block_bytes) != document["complement_sha256"]:
            raise RecoveryError("uncaptured bytes changed; metadata snapshot cannot identify their history")
        if size > min(budget.max_output_bytes, budget.max_copied_bytes):
            raise UnsupportedCase("restored file exceeds its output or full-copy quota")
        if shutil.disk_usage(output.parent).free < size + budget.disk_reserve_bytes:
            raise UnsupportedCase("not enough free space for the staged restored file")
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-metadata-", dir=output.parent) as directory, \
             tempfile.TemporaryDirectory(prefix=".h5reclaim-metadata-", dir=report_path.parent) as report_directory:
            staged, staged_report = Path(directory) / "restored.h5", Path(report_directory) / "report.json"
            shutil.copyfile(image, staged)
            changes, cursor = [], 0
            with staged.open("r+b", buffering=0) as target:
                for start, stop, label in intervals:
                    replacement = metadata[cursor:cursor + stop - start]
                    target.seek(start)
                    prior = target.read(stop - start)
                    if len(prior) != len(replacement):
                        raise RecoveryError("short damaged metadata range")
                    if prior != replacement:
                        target.seek(start)
                        target.write(replacement)
                        changes.append({"offset": start, "length": stop - start, "kind": label,
                            "before_sha256": hashlib.sha256(prior).hexdigest(),
                            "after_sha256": hashlib.sha256(replacement).hexdigest(),
                            "changed_bytes": sum(a != b for a, b in zip(prior, replacement))})
                    cursor += stop - start
            if not changes:
                raise RecoveryError("captured metadata has no changed bytes to restore")
            if sha256_file(staged) != document["source_sha256"]:
                raise RecoveryError("restored file differs from the captured healthy source")
            checked = inspect_metadata_local(staged, budget=budget, profile="strict")
            if checked["decision"] != "accept" or not checked["analysis"]["complete"]:
                raise RecoveryError("restored file did not pass independent covered-metadata preflight")
            report = {"schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                "operation": "checked_metadata_snapshot_restore", "outcome": "metadata_restored",
                "source": {"path": str(source), "size_bytes": size, "sha256_before": damaged_sha},
                "capture": {"archive_sha256": expected_sha256, "source_sha256": document["source_sha256"],
                            "covered_objects": document["covered_objects"],
                            "captured_ranges": len(intervals), "captured_bytes": len(metadata)},
                "changed_ranges": changes, "output_path": str(output),
                "restored_sha256": document["source_sha256"], "physical_source_bytes_copied": copied,
                "scope": "restored captured checked metadata only; uncaptured bytes matched the earlier snapshot exactly"}
            serialized = json.dumps(report, sort_keys=True, indent=2) + "\n"
            if len(serialized.encode("utf-8")) > MAX_MANIFEST:
                raise UnsupportedCase("restoration report exceeds its bound")
            staged_report.write_text(serialized, encoding="utf-8")
            _verify_source(source, identity, damaged_sha)
            if sha256_file(archive) != expected_sha256:
                raise RecoveryError("metadata snapshot changed during restoration")
            _validate_paths(source, output, report_path)
            published = False
            try:
                os.link(staged, output)
                published = True
                os.link(staged_report, report_path)
            except Exception:
                if published:
                    output.unlink(missing_ok=True)
                raise
            return report
