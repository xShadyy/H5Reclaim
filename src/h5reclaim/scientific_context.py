"""Bounded audit of scientific metadata around one selected local dataset.

This is descriptive evidence, not a repair route.  A fresh process contains
native HDF5 errors.  It never reads measurement values, follows external or
soft links, or dereferences dimension-scale reference arrays.  The recovered
file remains a selected-dataset export; observed sibling context is omitted.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .worker_limits import run_worker

MAX_AUDIT_SOURCE_BYTES = 4 * 1024**3
MAX_AUDIT_RESPONSE_BYTES = 256 * 1024
MAX_GROUP_DEPTH = 32
MAX_GROUP_LINKS = 64
MAX_ATTRIBUTES = 64
MAX_NAME_BYTES = 128
AUDIT_TIMEOUT_SECONDS = 90


def _omitted(reason: str, selected_path: str, copied: Sequence[str],
             omitted: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "uninspected",
        "selected_path": selected_path,
        "attributes_reported_copied": list(copied),
        "attributes_reported_omitted": list(omitted),
        "selected_attribute_names_not_copied": None,
        "copied_name_contradictions": [],
        "source_metadata": None,
        "omissions": [
            f"source context could not be inspected: {reason}",
            "ancestor attributes and other objects are not copied to the output",
            "dimension scale targets and links are not copied to the output",
        ],
        "historical_integrity": "not established by this audit",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _bounded_names(attributes: Any) -> dict[str, Any]:
    count = len(attributes)
    if count > MAX_ATTRIBUTES:
        return {"count": count, "names": None, "omission": "attribute count exceeds 64"}
    names = sorted(attributes.keys())
    if any(not isinstance(name, str) or len(name.encode("utf-8")) > MAX_NAME_BYTES
           for name in names):
        return {"count": count, "names": None,
                "omission": "attribute name exceeds 128 UTF-8 bytes"}
    return {"count": count, "names": names, "omission": None}


def _group_inventory(group: Any, path: str) -> dict[str, Any]:
    import h5py

    attributes = _bounded_names(group.attrs)
    link_count = len(group)
    if link_count > MAX_GROUP_LINKS:
        links = None
        link_omission = "group has more than 64 links; names were not enumerated"
    else:
        links = []
        link_omission = None
        for name in sorted(group.keys()):
            if not isinstance(name, str) or len(name.encode("utf-8")) > MAX_NAME_BYTES:
                link_omission = "at least one link name exceeds 128 UTF-8 bytes"
                links = None
                break
            link = group.get(name, getlink=True)
            kind = ("hard" if isinstance(link, h5py.HardLink)
                    else "soft" if isinstance(link, h5py.SoftLink)
                    else "external" if isinstance(link, h5py.ExternalLink) else "other")
            links.append({"name": name, "kind": kind})
    return {
        "path": path, "attributes": attributes, "link_count": link_count,
        "links": links, "link_omission": link_omission,
        "note": "only local link names and kinds were inspected; sibling targets were not opened",
    }


def _unit_value(dataset: Any, name: str) -> dict[str, Any]:
    """Inspect a tiny inline scalar string; leave heap-backed values unread."""
    import h5py

    try:
        attr = dataset.attrs.get_id(name)
        data_type, space = attr.get_type(), attr.get_space()
        if (space.get_simple_extent_ndims() != 0 or space.get_simple_extent_npoints() != 1
                or data_type.get_class() != h5py.h5t.STRING
                or data_type.is_variable_str() or data_type.get_size() > 128
                or attr.get_storage_size() > 128):
            return {"status": "not_read", "reason": "not a bounded fixed scalar string"}
        value = dataset.attrs[name]
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="strict")
        if not isinstance(value, str) or len(value.encode("utf-8")) > 128:
            return {"status": "not_read", "reason": "unit value exceeds the bounded string limit"}
        return {"status": "observed", "value": value}
    except (OSError, RuntimeError, TypeError, ValueError, UnicodeError):
        return {"status": "not_read", "reason": "unit value is unreadable or not valid UTF-8"}


def _inspect(source: Path, selected_path: str) -> dict[str, Any]:
    import h5py
    from .metadata import _selected_local_dataset

    with h5py.File(source, "r") as handle:
        selected = _selected_local_dataset(handle, selected_path)
        parts = selected_path[1:].split("/")
        if len(parts) > MAX_GROUP_DEPTH:
            raise ValueError("selected path exceeds the 32-group context audit limit")
        group = handle["/"]
        ancestors = [_group_inventory(group, "/")]
        for index, part in enumerate(parts[:-1], start=1):
            group = group[part]  # previously checked local hard link
            ancestors.append(_group_inventory(group, "/" + "/".join(parts[:index])))
        selected_attrs = _bounded_names(selected.attrs)
        attr_names = selected_attrs["names"]
        markers = None if attr_names is None else {
            "dimension_list": "DIMENSION_LIST" in attr_names,
            "dimension_labels": "DIMENSION_LABELS" in attr_names,
            "reference_list": "REFERENCE_LIST" in attr_names,
            "dimension_scale_class": "CLASS" in attr_names,
        }
        units = None if attr_names is None else [
            name for name in attr_names if "unit" in name.casefold()
        ]
        unit_values = None if units is None else {
            name: _unit_value(selected, name) for name in units
        }
        return {
            "selected_dataset": {
                "path": selected_path, "shape": list(selected.shape),
                "attribute_names": selected_attrs, "unit_attribute_names": units,
                "unit_values": unit_values,
                "dimension_scale_markers": markers,
                "dimension_scale_targets": "not dereferenced or copied",
            },
            "ancestor_groups": ancestors,
        }


def _child(request: Path, response: Path) -> int:
    try:
        if request.stat().st_size > 16384:
            raise ValueError("context audit request exceeds 16 KiB")
        data = json.loads(request.read_text(encoding="utf-8"))
        os.environ["HDF5_PLUGIN_PRELOAD"] = "::"
        os.environ.pop("HDF5_PLUGIN_PATH", None)
        os.environ.pop("HDF5_EXTFILE_PREFIX", None)
        from .native_worker import _apply_memory_limit
        _apply_memory_limit(1024**3)
        source = Path(data["source"])
        expected = data["expected_sha256"]
        if (not isinstance(expected, str) or len(expected) != 64
                or any(character not in "0123456789abcdef" for character in expected)):
            raise ValueError("expected SHA-256 must be 64 lowercase hexadecimal digits")
        before = source.stat()
        if not source.is_file() or before.st_size > MAX_AUDIT_SOURCE_BYTES:
            raise ValueError("source is not a regular file or exceeds the 4 GiB context limit")
        if _sha256(source) != expected or source.stat() != before:
            raise ValueError("source differs from the selected recovery snapshot")
        result = _inspect(source, data["dataset"])
        if _sha256(source) != expected or source.stat() != before:
            raise ValueError("source changed during the context audit")
        encoded = json.dumps({"status": "ok", "source_metadata": result}, sort_keys=True).encode()
        if len(encoded) > MAX_AUDIT_RESPONSE_BYTES:
            raise ValueError("context audit response exceeds 256 KiB")
        response.write_bytes(encoded)
        return 0
    except Exception as exc:
        response.write_text(json.dumps({"status": "error", "reason":
                                        f"{type(exc).__name__}: {str(exc)[:200]}"}), encoding="utf-8")
        return 2


def audit_context(source: str | Path, selected_path: str, expected_sha256: str,
                  copied_attributes: Sequence[str] = (),
                  omitted_attributes: Sequence[str] = ()) -> dict[str, Any]:
    """Return a report-only audit tied to the hash of the recovery source.

    Failures, unsupported metadata and child crashes produce explicit unknown
    context. No source or recovery output is modified. The supplied copied and
    omitted names are existing recovery-report claims, not established by the
    audit itself.
    """
    copied, omitted = tuple(copied_attributes), tuple(omitted_attributes)
    if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)):
        return _omitted("missing valid source SHA-256", selected_path, copied, omitted)
    with tempfile.TemporaryDirectory(prefix="h5reclaim-context-") as directory:
        request, response = Path(directory) / "request.json", Path(directory) / "response.json"
        encoded = json.dumps({"source": str(source), "dataset": selected_path,
                              "expected_sha256": expected_sha256}).encode()
        if len(encoded) > 16384:
            return _omitted("source path or dataset path exceeds audit request limit",
                            selected_path, copied, omitted)
        request.write_bytes(encoded)
        environment = os.environ.copy()
        environment["HDF5_PLUGIN_PRELOAD"] = "::"
        environment.pop("HDF5_PLUGIN_PATH", None)
        environment.pop("HDF5_EXTFILE_PREFIX", None)
        try:
            result = run_worker(
                [sys.executable, "-m", "h5reclaim.scientific_context", str(request), str(response)],
                env=environment, timeout_seconds=AUDIT_TIMEOUT_SECONDS, memory_bytes=1024**3,
            )
            if not response.is_file() or response.stat().st_size > MAX_AUDIT_RESPONSE_BYTES:
                raise ValueError("audit worker produced no bounded response")
            message = json.loads(response.read_text(encoding="utf-8"))
            if result.returncode or message.get("status") != "ok":
                raise ValueError(str(message.get("reason", "audit worker failed"))[:220])
            observed = message["source_metadata"]
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            return _omitted(str(exc)[:240], selected_path, copied, omitted)
    try:
        observed_names = observed["selected_dataset"]["attribute_names"]["names"]
        ancestors = observed["ancestor_groups"]
    except (TypeError, KeyError, IndexError):
        return _omitted("audit worker response has an invalid schema",
                        selected_path, copied, omitted)
    contradictions = [] if observed_names is None else sorted(set(copied) - set(observed_names))
    selected_omitted_names = None if observed_names is None else sorted(set(observed_names) - set(copied))
    omissions = [
        "ancestor group attributes and other objects are not copied to the output",
        "dimension scale targets, labels, and links are not copied to the output",
        "soft and external link targets were not followed",
        "aliases outside the selected hard-link path were not inventoried",
    ]
    if contradictions:
        omissions.append("reported copied attribute names absent in the audited source metadata")
    if observed_names is None:
        omissions.append("selected attribute names exceed the bounded context audit")
    if any(item["attributes"]["names"] is None or item["links"] is None
           for item in ancestors):
        omissions.append("some ancestor group attributes or links exceed audit limits")
    return {
        "schema_version": 1,
        "status": "bounded_observation" if not contradictions else "contradiction",
        "selected_path": selected_path,
        "source_sha256_bound": expected_sha256,
        "attributes_reported_copied": list(copied),
        "attributes_reported_omitted": list(omitted),
        "selected_attribute_names_not_copied": selected_omitted_names,
        "copied_name_contradictions": contradictions,
        "source_metadata": observed,
        "omissions": omissions,
        "historical_integrity": "not established by this audit",
    }


if __name__ == "__main__":
    raise SystemExit(_child(Path(sys.argv[1]), Path(sys.argv[2])) if len(sys.argv) == 3 else 2)
