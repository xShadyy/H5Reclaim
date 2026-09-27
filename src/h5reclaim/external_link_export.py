"""Bounded export through one explicitly pinned HDF5 external link.

An external link stores a target filename and object path, but neither is
resolved as a filesystem path here. The operator supplies a distinct file
and its SHA-256. Both containers are privately snapshotted and the target
path must traverse local hard links only. The selected target then passes
the native-readable allocation, sibling-ownership, and output checks.

HDF5 external-link semantics:
https://support.hdfgroup.org/documentation/hdf5/latest/group___h5_l.html
"""

from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import h5py

from .dependency_routes import inspect_dependencies
from .metadata import UnsupportedCase
from .readable_export import _export_readable_local, _selected_dataset
from .recovery import (
    RecoveryError, _validate_paths, _verify_source, sha256_file, source_snapshot,
)
from .vds_export import _manifest_entries


MAX_LINK_NAME = 512
MAX_RELATED_BYTES = 4 * 1024 * 1024 * 1024


def _linked_target(image: Path, dataset_path: str, inventory: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return declared filename, link object path, selected target path."""
    if (inventory.get("outcome") != "external_link" or inventory.get("omitted")
            or len(inventory.get("dependencies", ())) != 1):
        raise UnsupportedCase("selected path must cross exactly one declared external link")
    dependency = inventory["dependencies"][0]
    if dependency.get("kind") != "external_link" or not dependency.get("declared_name_exact"):
        raise UnsupportedCase("external link name is not exactly representable")
    parts = dataset_path[1:].split("/")
    if (not dataset_path.startswith("/") or len(parts) > 64
            or any(part in ("", ".", "..") for part in parts)):
        raise UnsupportedCase("selected dataset path must be canonical")
    with h5py.File(image, "r") as handle:
        current = handle["/"]
        for index, part in enumerate(parts):
            if not isinstance(current, h5py.Group):
                raise UnsupportedCase("external link parent is not a local group")
            link = current.get(part, getlink=True)
            if isinstance(link, h5py.ExternalLink):
                name, object_path = link.filename, link.path
                if (not isinstance(name, str) or not name or len(name) > MAX_LINK_NAME
                        or "\x00" in name or not isinstance(object_path, str)
                        or not object_path.startswith("/") or len(object_path) > 2048
                        or "\x00" in object_path or any(ord(c) < 32 or ord(c) == 127
                                                        for c in object_path)):
                    raise UnsupportedCase("external link name or target path is unsupported")
                try:
                    name.encode("utf-8", "strict")
                    object_path.encode("utf-8", "strict")
                except UnicodeError as exc:
                    raise UnsupportedCase("external link name or target is not exact UTF-8") from exc
                tail = object_path[1:].split("/") if object_path != "/" else []
                if any(component in ("", ".", "..") for component in tail):
                    raise UnsupportedCase("external link target path is not canonical")
                target_parts = tail + parts[index + 1:]
                if not target_parts:
                    raise UnsupportedCase("external link points to a group, not a dataset")
                target_path = "/" + "/".join(target_parts)
                if (len(target_path.encode("utf-8")) > 4096 or len(target_parts) > 64
                        or target_parts[0] == "_h5reclaim"):
                    raise UnsupportedCase("external target dataset path exceeds the local limits")
                if (dependency.get("file_name") != name
                        or dependency.get("object_path") != object_path):
                    raise UnsupportedCase("external link inventory differs from its declared target")
                return name, object_path, target_path
            if not isinstance(link, h5py.HardLink):
                raise UnsupportedCase("external link path includes a soft or missing local component")
            current = current[part]
    raise UnsupportedCase("selected path has no external link")


def export_external_link(
    source: str | Path, dataset_path: str,
    related_files: str | Path | Mapping[str, Any],
    output: str | Path, report_path: str | Path,
    *, published_output: str | Path | None = None,
) -> dict[str, Any]:
    """Materialize a locally stored target reached through a pinned link.

    Values are only asserted for the current pinned file. Neither the link's
    own filename nor any target-side external/soft link is followed.
    """
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    entries = _manifest_entries(related_files)
    if isinstance(related_files, (str, Path)) and Path(related_files).resolve(strict=True) in (
            output.resolve(strict=False), report_path.resolve(strict=False)):
        raise RecoveryError("destination aliases the related-file manifest")
    with ExitStack() as stack:
        image, main_hash, main_identity, main_size = stack.enter_context(source_snapshot(source))
        inventory = inspect_dependencies(image, dataset_path)
        declared_name, declared_object_path, target_path = _linked_target(
            image, dataset_path, inventory)
        if set(entries) != {declared_name}:
            raise UnsupportedCase("supply exactly the file declared by the selected external link")
        entry = entries[declared_name]
        related = Path(entry["path"])
        if not related.is_file():
            raise UnsupportedCase("declared external-link target file is unavailable")
        if related.samefile(source):
            raise UnsupportedCase("external-link target aliases the selected container")
        if related.resolve(strict=True) in (output.resolve(strict=False), report_path.resolve(strict=False)):
            raise RecoveryError("destination aliases the external-link target")
        snapshot, target_hash, target_identity, target_size = stack.enter_context(
            source_snapshot(related, max_source_bytes=max(1, MAX_RELATED_BYTES - main_size)))
        if target_hash != entry["sha256"]:
            raise RecoveryError("external-link target differs from the pinned SHA-256")
        if main_size + target_size > MAX_RELATED_BYTES:
            raise UnsupportedCase("external-link bundle exceeds 4 GiB")
        with h5py.File(snapshot, "r") as target_file:
            _selected_dataset(target_file, target_path)
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-link-", dir=output.parent) as outdir:
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-link-", dir=report_path.parent) as repdir:
                staged_output = Path(outdir) / "linked.h5"
                staged_report = Path(repdir) / "linked.json"
                report = _export_readable_local(
                    snapshot, target_path, staged_output, staged_report,
                    output_dataset_path=dataset_path,
                    published_output=Path(published_output) if published_output else output,
                )
                # The nested route has already checked allocation ownership,
                # copied bounded values, and read back the separate output.
                report["mode"] = "external_link_export"
                report["source"] = {
                    "path": str(source), "sha256_before": main_hash, "sha256_after": main_hash,
                    "size_bytes": main_size,
                }
                report["external_link"] = {
                    "declared_filename": declared_name,
                    "declared_object_path": declared_object_path,
                    "resolved_local_dataset_path": target_path,
                    "link_container_sha256": main_hash,
                    "target_path": str(related), "target_sha256": target_hash,
                    "target_size_bytes": target_size,
                    "target_snapshot_sha256": sha256_file(snapshot),
                }
                # Offsets and the sibling-ownership inventory in the nested
                # report refer to this target file, never the link container.
                report["dataset"]["storage_file"] = "external_link_target"
                report["dataset"]["storage_file_sha256"] = target_hash
                report["limits"] += (
                    " The external link was resolved only through an explicit pinned target. "
                    "The exported path is materialized locally; unrelated objects and the "
                    "original external-link relationship are not reproduced."
                )
                report_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
                if len(report_text.encode("utf-8")) > 32 * 1024 * 1024:
                    raise UnsupportedCase("external-link report exceeds the publication limit")
                with h5py.File(staged_output, "r+") as result:
                    result["/_h5reclaim/report_json"][()] = report_text
                    result["/_h5reclaim"].attrs["source_sha256"] = main_hash
                staged_report.write_text(report_text, encoding="utf-8")
                if sha256_file(image) != main_hash or sha256_file(snapshot) != target_hash:
                    raise RecoveryError("private external-link snapshot changed during export")
                _verify_source(source, main_identity, main_hash)
                _verify_source(related, target_identity, target_hash)
                _validate_paths(source, output, report_path)
                published = False
                try:
                    os.link(staged_output, output)
                    published = True
                    os.link(staged_report, report_path)
                except Exception:
                    if published:
                        output.unlink(missing_ok=True)
                    raise
                return report
