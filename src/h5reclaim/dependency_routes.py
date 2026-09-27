"""Read-only triage for file status and declared HDF5 dependencies.

The superblock status byte is an observation, not a corruption diagnosis. In
particular, old superblock versions do not assign write-lock meaning to their
consistency field, and an EOA/EOF mismatch does not establish what was lost.

External and virtual source names are read from metadata only. Referenced
paths are deliberately not opened or resolved, and no dataset values are read.
The optional h5clear experiment runs only on a second disposable copy; its
result is never published as a recovered HDF5 file.

Format reference: https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
h5clear reference: https://support.hdfgroup.org/documentation/hdf5/latest/_h5_t_o_o_l__c_r__u_g.html
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

import h5py

from .recovery import _verify_source, sha256_file, source_snapshot


SIGNATURE = b"\x89HDF\r\n\x1a\n"
MAX_DEPENDENCIES = 64
MAX_NAME_CHARS = 512
MAX_ERROR_CHARS = 240
PROBE_TIMEOUT_SECONDS = 30
MAX_MANIFEST_BYTES = 64 * 1024
MAX_BUNDLE_BYTES = 4 * 1024 * 1024 * 1024


class DependencyError(ValueError):
    """The explicitly supplied file manifest is invalid or contradictory."""


def _short(value: object, limit: int = MAX_ERROR_CHARS) -> str:
    return " ".join(str(value).split())[:limit]


def _name(value: bytes | str) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "backslashreplace")
    return value[:MAX_NAME_CHARS]


def _exact_name(value: bytes | str) -> bool:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "strict")
        except UnicodeError:
            return False
    return len(value) <= MAX_NAME_CHARS and "\x00" not in value


def inspect_superblock_status(
    image: str | Path, signature_offset: int | None, file_size: int,
) -> dict[str, Any]:
    """Read only bounded fields in a known superblock location.

    The v2/v3 checksum is not validated here. Values are labeled raw and must
    not be used as trusted structural anchors for chunk attribution.
    """
    result: dict[str, Any] = {
        "outcome": "unavailable", "superblock_version": None,
        "status_flags": {"interpretation": "unavailable", "raw": None},
        "end_of_address": {"raw": None, "physical_eof": file_size, "relation": "unknown"},
        "checksum_validated": False,
        "note": "Raw superblock observations do not prove a stale flag, a complete file, or recovered measurements.",
    }
    if signature_offset is None or signature_offset < 0 or file_size < signature_offset + 12:
        return result
    with Path(image).open("rb") as handle:
        handle.seek(signature_offset)
        prefix = handle.read(min(96, file_size - signature_offset))
    if len(prefix) < 12 or prefix[:8] != SIGNATURE:
        return result
    version = prefix[8]
    result["superblock_version"] = version
    if version in (0, 1):
        # v0/1 consistency bytes are unused, not evidence of an interrupted writer.
        if len(prefix) < (28 if version == 1 else 24):
            return result
        offset_size = prefix[13]
        header_size = 28 if version == 1 else 24
        flags = int.from_bytes(prefix[20:24], "little")
        interpretation = "unused_in_this_version"
    elif version in (2, 3):
        offset_size = prefix[9]
        header_size = 12
        flags = prefix[11]
        if version == 3:
            interpretation = (
                "write_flag_present" if flags & 0b101 else "no_write_flag_observed"
            )
        else:
            interpretation = "unused_in_this_version"
    else:
        result["status_flags"]["interpretation"] = "unknown_version"
        return result
    if offset_size not in (1, 2, 4, 8) or len(prefix) < header_size + 3 * offset_size:
        result["status_flags"]["interpretation"] = "truncated_or_unsupported_offset_size"
        return result
    base = int.from_bytes(prefix[header_size:header_size + offset_size], "little")
    eoa_start = header_size + 2 * offset_size
    eoa = int.from_bytes(prefix[eoa_start:eoa_start + offset_size], "little")
    result["outcome"] = "observed"
    result["base_address_raw"] = base
    result["offset_size"] = offset_size
    result["status_flags"] = {
        "raw": flags,
        "interpretation": interpretation,
        "write_access_bit": bool(flags & 1) if version == 3 else None,
        "swmr_write_bit": bool(flags & 4) if version == 3 else None,
        "reserved_bits_present": bool(flags & ~0b101) if version == 3 else None,
    }
    undefined = (1 << (offset_size * 8)) - 1
    relation = "unknown"
    if base == signature_offset and eoa != undefined:
        relation = "equal" if eoa == file_size else ("past_physical_eof" if eoa > file_size else "before_physical_eof")
    result["end_of_address"] = {
        "raw": eoa,
        "physical_eof": file_size,
        "relation": relation,
    }
    return result


def inspect_dependencies(image: str | Path, dataset_path: str | None) -> dict[str, Any]:
    """Inventory selected metadata without following soft/external links.

    Source names may be relative, absolute, or VDS patterns. They are reported
    as declared, not resolved against the temporary snapshot directory.
    """
    result: dict[str, Any] = {
        "outcome": "not_selected" if dataset_path is None else "unavailable",
        "dataset_path": dataset_path,
        "dependencies": [],
        "omitted": 0,
        "referenced_files_opened": False,
        "values_read": False,
        "note": "Declared paths are metadata claims. A missing source can be rendered as fill values by native HDF5 reads.",
    }
    if dataset_path is None:
        return result
    if not dataset_path.startswith("/") or any(part in ("", ".", "..") for part in dataset_path[1:].split("/")):
        result.update(outcome="invalid_path", error="select an absolute local HDF5 object path")
        return result
    if len(dataset_path) > MAX_NAME_CHARS * 4:
        result.update(outcome="invalid_path", error="selected object path exceeds inspection limit")
        return result
    try:
        with h5py.File(image, "r") as handle:
            obj: h5py.Group | h5py.Dataset = handle["/"]
            for part in dataset_path[1:].split("/"):
                if not isinstance(obj, h5py.Group):
                    result.update(outcome="invalid_path", error="a path component is not a group")
                    return result
                link = obj.get(part, getlink=True)
                if isinstance(link, h5py.ExternalLink):
                    result["dependencies"] = [{
                        "kind": "external_link", "file_name": _name(link.filename),
                        "object_path": _name(link.path), "requires_independent_file": True,
                        "declared_name_exact": _exact_name(link.filename),
                    }]
                    result["outcome"] = "external_link"
                    return result
                if isinstance(link, h5py.SoftLink):
                    result.update(outcome="soft_link", error="selected path crosses a soft link; it was not followed")
                    return result
                if not isinstance(link, h5py.HardLink):
                    result.update(outcome="missing_or_unknown_link", error="selected path does not have a local hard link")
                    return result
                obj = obj.get(part)
            if not isinstance(obj, h5py.Dataset):
                result.update(outcome="not_a_dataset", error="selected local object is not a dataset")
                return result
            creation = obj.id.get_create_plist()
            n_external = int(creation.get_external_count())
            n_virtual = int(creation.get_virtual_count()) if obj.is_virtual else 0
            dependencies = []
            for i in range(min(n_external, MAX_DEPENDENCIES)):
                filename, offset, size = creation.get_external(i)
                dependencies.append({
                    "kind": "external_raw_storage", "file_name": _name(filename),
                    "raw_offset": int(offset), "raw_size": int(size),
                    "requires_independent_file": True,
                    "declared_name_exact": _exact_name(filename),
                })
            remaining = MAX_DEPENDENCIES - len(dependencies)
            for i in range(min(n_virtual, remaining)):
                filename = creation.get_virtual_filename(i)
                dependencies.append({
                    "kind": "virtual_source",
                    "file_name": _name(filename),
                    "object_path": _name(creation.get_virtual_dsetname(i)),
                    "requires_independent_file": True,
                    "declared_name_exact": _exact_name(filename),
                })
            result["dependencies"] = dependencies
            result["omitted"] = max(0, n_external + n_virtual - len(dependencies))
            result["outcome"] = "limited" if result["omitted"] else "complete"
            return result
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, OverflowError) as exc:
        result.update(outcome="metadata_unreadable", error=_short(exc))
        return result


def load_dependency_manifest(path: str | Path) -> dict[str, Any]:
    """Load exact declared-name to absolute-path mappings with pinned hashes.

    The declared HDF5 filename is never used as a filesystem path. The path
    must be supplied independently, and relative paths are refused to avoid
    ambiguity after the main HDF5 file is copied to a temporary directory.
    """
    path = Path(path)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise DependencyError("related-file manifest exceeds 64 KiB")
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DependencyError(f"invalid related-file manifest: {_short(exc)}") from exc
    if not isinstance(document, dict) or set(document) != {"schema_version", "files"}:
        raise DependencyError("manifest must contain only schema_version and files")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise DependencyError("unsupported related-file manifest version")
    files = document["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_DEPENDENCIES:
        raise DependencyError(f"manifest must list between 1 and {MAX_DEPENDENCIES} files")
    seen = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"declared_name", "path", "sha256"}:
            raise DependencyError("each file needs exactly declared_name, path, and sha256")
        name, file_path, digest = item["declared_name"], item["path"], item["sha256"]
        if not isinstance(name, str) or not name or len(name) > MAX_NAME_CHARS or "\x00" in name:
            raise DependencyError("declared_name must be a bounded, nonempty string")
        if name in seen:
            raise DependencyError(f"duplicate declared_name: {_short(name)}")
        seen.add(name)
        if not isinstance(file_path, str) or not file_path or "\x00" in file_path or len(file_path) > 4096:
            raise DependencyError("path must be a bounded, nonempty string")
        if not Path(file_path).is_absolute():
            raise DependencyError("related-file paths must be absolute")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DependencyError("sha256 must be 64 lowercase hexadecimal digits")
    return document


def _hash_supplied_file(path: Path, expected: str, remaining_budget: int) -> dict[str, Any]:
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            return {"status": "not_regular_file"}
        if before.st_size > remaining_budget:
            return {"status": "bundle_size_limit", "size_bytes": before.st_size}
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if opened.st_size != before.st_size or (
                os.name != "nt" and (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                return {"status": "changed_during_open"}
            total = 0
            while block := handle.read(1024 * 1024):
                total += len(block)
                if total > remaining_budget:
                    return {"status": "bundle_size_limit", "size_bytes": total}
                digest.update(block)
            after_opened = os.fstat(handle.fileno())
        after = path.stat()
        fields = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if fields(before) != fields(after) or fields(opened) != fields(after_opened) or total != before.st_size:
            return {"status": "changed_during_hash"}
        found = digest.hexdigest()
        return {
            "status": "hash_matched" if found == expected else "hash_mismatch",
            "sha256_observed": found,
            "size_bytes": total,
        }
    except (OSError, ValueError) as exc:
        return {"status": "file_unavailable", "error": _short(exc)}


def _inspect_referenced_object(path: Path, object_path: Any, *, require_dataset: bool) -> str:
    """Check only an explicitly supplied HDF5 file's local object metadata."""
    if not isinstance(object_path, str) or not object_path.startswith("/"):
        return "unsupported_object_path"
    parts = object_path[1:].split("/")
    if len(object_path) > MAX_NAME_CHARS * 4 or any(part in ("", ".", "..") for part in parts):
        return "unsupported_object_path"
    try:
        with h5py.File(path, "r") as handle:
            obj: h5py.Group | h5py.Dataset = handle["/"]
            for part in parts:
                if not isinstance(obj, h5py.Group):
                    return "expected_object_missing"
                if not isinstance(obj.get(part, getlink=True), h5py.HardLink):
                    return "source_link_not_local_hard_link"
                obj = obj.get(part)
            if require_dataset and not isinstance(obj, h5py.Dataset):
                return "expected_dataset_missing"
            if not isinstance(obj, (h5py.Group, h5py.Dataset)):
                return "expected_object_missing"
            if isinstance(obj, h5py.Dataset):
                if obj.is_virtual or obj.id.get_create_plist().get_external_count():
                    return "transitive_dependency_unverified"
            return "local_object_metadata_present"
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        return "referenced_metadata_unreadable"


def validate_dependency_manifest(
    dependency_report: Mapping[str, Any], manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Match all selected declared names to separately pinned local files.

    Matching only establishes the identity and availability of supplied
    files. No HDF5 external/VDS values are read, and no recursive discovery,
    globbing, environment expansion, or implicit path resolution occurs.
    """
    if dependency_report.get("outcome") not in ("complete", "external_link"):
        return {
            "outcome": "dependency_inventory_incomplete", "references": [],
            "all_declared_present_and_hash_matched": False,
            "values_read": False,
        }
    # Validate even direct API calls, not only values returned by the loader.
    if not isinstance(manifest, Mapping) or type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1:
        raise DependencyError("invalid or unsupported related-file manifest")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > MAX_DEPENDENCIES:
        raise DependencyError("invalid related-file manifest files")
    by_name: dict[str, dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"declared_name", "path", "sha256"}:
            raise DependencyError("invalid related-file manifest entry")
        name, path, digest = item["declared_name"], item["path"], item["sha256"]
        if not isinstance(name, str) or not name or len(name) > MAX_NAME_CHARS or "\x00" in name or name in by_name:
            raise DependencyError("invalid or duplicate declared_name")
        if not isinstance(path, str) or not Path(path).is_absolute() or len(path) > 4096 or "\x00" in path:
            raise DependencyError("related-file path must be absolute")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DependencyError("invalid related-file SHA-256")
        by_name[name] = item

    dependencies = dependency_report.get("dependencies")
    if not isinstance(dependencies, list) or len(dependencies) > MAX_DEPENDENCIES:
        raise DependencyError("invalid dependency inventory")
    references = []
    used: set[str] = set()
    budget = MAX_BUNDLE_BYTES
    checked: dict[str, dict[str, Any]] = {}
    for dependency in dependencies:
        name = dependency.get("file_name")
        if not isinstance(name, str):
            raise DependencyError("dependency inventory contains invalid file name")
        reference = {"kind": dependency.get("kind"), "declared_name": name}
        if not dependency.get("declared_name_exact", True):
            reference["status"] = "name_not_exactly_representable"
        elif "%" in name and dependency.get("kind") == "virtual_source":
            reference["status"] = "dynamic_filename_pattern_unresolved"
        elif name not in by_name:
            reference["status"] = "not_supplied"
        else:
            used.add(name)
            if name not in checked:
                item = by_name[name]
                checked[name] = _hash_supplied_file(Path(item["path"]), item["sha256"], budget)
                if checked[name]["status"] in ("hash_matched", "hash_mismatch"):
                    budget -= checked[name]["size_bytes"]
            reference.update(checked[name])
            reference["supplied_path"] = by_name[name]["path"]
            if reference["status"] == "hash_matched" and reference["kind"] == "external_raw_storage":
                offset, length = dependency.get("raw_offset"), dependency.get("raw_size")
                if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length < 0:
                    reference["status"] = "invalid_declared_raw_range"
                elif length == (1 << 64) - 1:
                    # HDF5 unlimited external segment: its full logical
                    # extent needs the selected dataset's shape/type and all
                    # preceding segments, which are not established here.
                    reference["status"] = "unlimited_raw_range_unverified"
                elif offset + length > reference["size_bytes"]:
                    reference["status"] = "declared_raw_range_missing"
            if reference["status"] == "hash_matched" and reference["kind"] in ("virtual_source", "external_link"):
                target = _inspect_referenced_object(
                    Path(by_name[name]["path"]), dependency.get("object_path"),
                    require_dataset=reference["kind"] == "virtual_source",
                )
                reference["target_metadata"] = target
                if target != "local_object_metadata_present":
                    reference["status"] = target
        references.append(reference)
    unused = sorted(set(by_name) - used)
    complete = bool(references) and all(item["status"] == "hash_matched" for item in references)
    return {
        "outcome": "complete" if complete else "limited",
        "references": references,
        "unused_manifest_names": unused,
        "all_declared_present_and_hash_matched": complete,
        "values_read": False,
        "historical_values_verified": False,
        "note": "Matching a supplied file hash does not prove that it is the historically correct scientific source.",
    }


def discover_h5clear() -> dict[str, Any]:
    """Discover only; do not run h5clear or change any file."""
    executable = shutil.which("h5clear")
    return {
        "available": executable is not None,
        "executable": executable,
        "allowed_trial": "--status on a disposable copy only",
        "warning": "h5clear is not a general corruption repair tool and cannot restore overwritten data.",
    }


def _changed_bytes(before: Path, after: Path, allowed: set[int]) -> tuple[int, list[int]]:
    """Stream both bounded images and retain just the first unexpected offsets."""
    count, unexpected = 0, []
    with before.open("rb") as original, after.open("rb") as trial:
        offset = 0
        while chunk := original.read(1024 * 1024):
            altered = trial.read(len(chunk))
            if len(altered) != len(chunk):
                return count + 1, [offset + len(altered)]
            if chunk != altered:
                for i, (left, right) in enumerate(zip(chunk, altered)):
                    if left != right:
                        count += 1
                        if offset + i not in allowed and len(unexpected) < 8:
                            unexpected.append(offset + i)
            offset += len(chunk)
        if trial.read(1):
            return count + 1, [offset]
    return count, unexpected


def probe_status_copy(source: str | Path, *, executable: str | None = None) -> dict[str, Any]:
    """Optionally test h5clear --status on a throwaway copy, with no export.

    Only a v3 superblock with a write bit and no reserved bits is eligible.
    EOA beyond physical EOF is excluded, as it may signal missing bytes.
    The result is metadata-openability evidence, never a recovery success.
    """
    source = Path(source)
    with source_snapshot(source) as (image, digest, identity, size):
        sig = 0
        with image.open("rb") as handle:
            while sig + len(SIGNATURE) <= size:
                handle.seek(sig)
                if handle.read(len(SIGNATURE)) == SIGNATURE:
                    break
                sig = 512 if sig == 0 else sig * 2
            else:
                sig = None
        observed = inspect_superblock_status(image, sig, size)
        result: dict[str, Any] = {
            "outcome": "not_applicable", "source": {"path": str(source), "sha256": digest},
            "observation": observed,
            "trial_was_disposable": True, "source_modified": False,
            "recovery_attempted": False, "recovered_values": None,
        }
        flags = observed["status_flags"]
        if not (observed["outcome"] == "observed" and observed["superblock_version"] == 3
                and flags["interpretation"] == "write_flag_present"
                and not flags["reserved_bits_present"]
                and observed["end_of_address"]["relation"] in ("equal", "before_physical_eof")):
            result["detail"] = "Only a v3 write flag with a bounded EOA is eligible for this status-only trial."
        else:
            command = executable if executable is not None else shutil.which("h5clear")
            if command is None:
                result.update(outcome="unavailable", detail="h5clear was not found on PATH; no trial was run.")
            else:
                with tempfile.TemporaryDirectory(prefix="h5reclaim-status-probe-") as directory:
                    trial = Path(directory) / "disposable-copy.h5"
                    shutil.copyfile(image, trial)
                    try:
                        completed = subprocess.run(
                            [command, "--status", str(trial)], capture_output=True,
                            text=True, errors="replace", timeout=PROBE_TIMEOUT_SECONDS, check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        result.update(outcome="tool_failed", detail=_short(exc))
                    else:
                        result["tool_exit_code"] = completed.returncode
                        result["tool_stderr"] = _short(completed.stderr)
                        offset = int(sig)
                        checksum_start = offset + 12 + 4 * observed["offset_size"]
                        allowed = {offset + 11, *range(checksum_start, checksum_start + 4)}
                        changed, unexpected = _changed_bytes(image, trial, allowed)
                        result["changed_bytes_in_trial"] = changed
                        result["unexpected_changed_offsets"] = unexpected
                        if unexpected:
                            result.update(outcome="unexpected_tool_change", detail="The status trial changed bytes outside the v3 flag and superblock checksum; its result is disregarded.")
                        elif completed.returncode != 0:
                            result.update(outcome="tool_failed", detail="h5clear failed on the disposable copy.")
                        else:
                            trial_status = inspect_superblock_status(trial, sig, size)
                            if trial_status["status_flags"]["raw"] & 0b101:
                                result.update(outcome="ineffective", detail="The write flag remains in the disposable copy.")
                            else:
                                try:
                                    with h5py.File(trial, "r") as handle:
                                        handle["/"]  # Metadata open only; no values read.
                                except (OSError, RuntimeError, ValueError) as exc:
                                    result.update(outcome="metadata_still_unreadable", detail=_short(exc))
                                else:
                                    result.update(outcome="copy_metadata_opened", detail="Native HDF5 opens the throwaway status-cleared copy. This does not verify any measurement or repair the original.")
        if sha256_file(image) != digest:
            raise ValueError("private source snapshot changed during status probe")
        _verify_source(source, identity, digest)
        return result
