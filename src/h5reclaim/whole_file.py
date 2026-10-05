"""Recover discovered datasets independently into one derived HDF5 file."""

from __future__ import annotations

import base64
import json
import hashlib
import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from collections import deque
from itertools import islice
from pathlib import Path
from typing import Any

from .worker_limits import run_worker

def _metadata_budget(obj=None):
    from .large_streaming import LargeBudget
    from .source_session import reused_image
    shared = reused_image(Path(obj.file.filename)) if obj is not None else None
    return LargeBudget(**shared["budget"]) if shared else LargeBudget()


def _attributes(obj, *, omit: set[str] | None = None, budget=None) -> tuple[list[dict], list[str]]:
    import h5py
    import numpy as np
    from .logical_types import contains_pointers, encode_value, token_bytes

    budget = budget or _metadata_budget(obj)
    copied, omitted, used = [], [], 0
    for number, name in enumerate(obj.attrs):
        if name in (omit or set()):
            continue
        if number >= budget.max_links:
            omitted.append("attribute count exceeds the configured metadata budget")
            break
        try:
            attr = obj.attrs.get_id(name)
            typ, space = attr.get_type(), attr.get_space()
            encoding = typ.encode()
            record = {"name": name, "type": encoding.hex()}
            shape = space.get_simple_extent_dims()
            if shape is None:
                record["null_space"] = True
            elif contains_pointers(typ):
                if space.get_simple_extent_npoints() > budget.max_grid:
                    raise ValueError("logical attribute exceeds the configured element budget")
                array = np.asarray(obj.attrs[name], dtype=typ.dtype)
                values = [encode_value(array[index], typ, obj.file, max_bytes=budget.max_metadata_bytes)
                          for index in np.ndindex(shape)]
                record.update(shape=list(shape), logical_values=values)
            else:
                if space.get_simple_extent_npoints() * typ.get_size() > budget.max_metadata_bytes:
                    raise ValueError("attribute exceeds the configured metadata byte budget")
                data = np.empty(shape, dtype=f"V{typ.get_size()}")
                attr.read(data, mtype=typ)
                record.update(shape=list(shape), value=base64.b64encode(data.tobytes()).decode("ascii"))
            size = len(json.dumps(record).encode("utf-8"))
            if size > budget.max_metadata_bytes - used:
                raise ValueError("attributes exceed the configured metadata byte budget")
            copied.append(record)
            used += size
        except (OSError, RuntimeError, ValueError, TypeError, UnicodeError, KeyError):
            omitted.append(name)
    return copied, omitted


def _inventory_local(image: Path, root="/", opened_file=None, budget=None) -> dict[str, Any]:
    import h5py
    from .rescue import file_condition
    from .source_session import reused_image
    from .large_streaming import LargeBudget
    shared = reused_image(image)
    budget = budget or (LargeBudget(**shared["budget"]) if shared else LargeBudget())
    from functools import partial
    read_attributes = partial(_attributes, budget=budget)

    condition = file_condition(image) if opened_file is None else {"end_of_address": {"relation": "equal"}, "status_flags": {"interpretation": "clear"}}
    groups, datasets, skipped, issues, group_aliases = [], [], [], [], []
    soft_links, external_links, named_types = [], [], []
    by_address: dict[int, dict] = {}
    seen_groups: set[int] = set()
    group_paths: dict[int, str] = {}
    link_count = 0
    with tempfile.TemporaryDirectory(prefix="h5reclaim-inventory-view-") as directory:
        inspection_image = image
        view = "source"
        if condition["end_of_address"].get("relation") == "past_physical_eof":
            import shutil
            from .chunk_truncation import MAX_MISSING_TAIL_BYTES, _declared_end
            declared, _ = _declared_end(image, image.stat().st_size, MAX_MISSING_TAIL_BYTES)
            inspection_image = Path(directory) / "tail-view.h5"
            shutil.copyfile(image, inspection_image)
            with inspection_image.open("r+b") as stream:
                stream.truncate(declared)
            view = "tail_names_only"
        elif condition["status_flags"].get("interpretation") == "write_flag_present":
            from .status_trial_export import _status_only_trial
            inspection_image = Path(directory) / "status-view.h5"
            _status_only_trial(image, inspection_image, image.stat().st_size, budget=budget)
            view = "checked_status_trial"
        from contextlib import nullcontext
        from .checked_view import checked_root_view
        with (nullcontext((inspection_image, None)) if opened_file is not None else checked_root_view(inspection_image, budget)) as (native_view, root_correction), (nullcontext(opened_file) if opened_file is not None else h5py.File(native_view, "r")) as handle:
            # Resolve the requested root through local links only.
            obj = handle["/"]
            for part in root.strip('/').split('/') if root != '/' else []:
                if not isinstance(obj, h5py.Group) or not isinstance(obj.get(part, getlink=True), h5py.HardLink):
                    raise ValueError("inventory root requires local hard links")
                obj = obj[part]
            if not isinstance(obj, h5py.Group):
                if not isinstance(obj, (h5py.Dataset, h5py.Datatype)):
                    raise ValueError("inventory root is not a recoverable object")
                attrs, omitted = read_attributes(obj)
                record = {"path": root, "address": int(h5py.h5o.get_info(obj.id).addr),
                          "attributes": attrs, "attributes_omitted": omitted}
                if isinstance(obj, h5py.Dataset):
                    record.update(aliases=[], dimensions=[], is_scale=False,
                                  committed_type=obj.id.get_type().committed(),
                                  committed_type_address=int(h5py.h5o.get_info(obj.id.get_type()).addr) if obj.id.get_type().committed() else None)
                    datasets.append(record)
                else:
                    record['type'] = obj.id.encode().hex()
                    named_types.append(record)
                queue = deque()
            else:
                queue = deque([(obj, root, 0)])
            while queue:
                group, path, depth = queue.popleft()
                address = int(h5py.h5o.get_info(group.id).addr)
                if address in seen_groups:
                    group_aliases.append({"path": path, "target": group_paths[address]})
                    continue
                if len(seen_groups) >= budget.max_objects:
                    issues.append({"path": path, "reason": "group inventory is incomplete"})
                    break
                seen_groups.add(address)
                group_paths[address] = path
                attributes, omitted = (read_attributes(group) if view != "tail_names_only"
                                       else ([], ["attributes were not read from the padded inventory view"]))
                groups.append({"path": path, "address": address,
                               "attributes": attributes, "attributes_omitted": omitted})
                try:
                    names = list(islice(group.keys(), max(0, budget.max_links - link_count) + 1))
                except (OSError, RuntimeError, ValueError, KeyError) as exc:
                    issues.append({"path": path, "reason": str(exc)[:300]})
                    continue
                for name in names:
                    link_count += 1
                    child_path = path.rstrip("/") + "/" + name
                    if link_count > budget.max_links:
                        issues.append({"path": path, "reason": "link inventory is incomplete"})
                        queue.clear()
                        break
                    if len(child_path.encode("utf-8")) > budget.max_metadata_bytes:
                        skipped.append({"path": child_path, "reason": "path exceeds the configured metadata budget"})
                        continue
                    try:
                        link = group.get(name, getlink=True)
                        if isinstance(link, h5py.SoftLink):
                            soft_links.append({"path": child_path, "target": link.path})
                            continue
                        if isinstance(link, h5py.ExternalLink):
                            external_links.append({"path": child_path, "filename": link.filename,
                                                   "target": link.path})
                            continue
                        if not isinstance(link, h5py.HardLink):
                            skipped.append({"path": child_path, "reason": type(link).__name__})
                            continue
                        object_address = int(group.id.links.get_info(name.encode("utf-8")).u)
                        obj = group[name]
                        if isinstance(obj, h5py.Group):
                            queue.append((obj, child_path, depth + 1))
                        elif isinstance(obj, h5py.Dataset):
                            if object_address in by_address:
                                by_address[object_address]["aliases"].append(child_path)
                                continue
                            if len(datasets) >= budget.max_objects:
                                issues.append({"path": child_path, "reason": "dataset inventory is incomplete"})
                                queue.clear()
                                break
                            is_scale = bool(obj.is_scale) if view != "tail_names_only" else False
                            attrs, omitted = (read_attributes(obj, omit={"DIMENSION_LIST", "REFERENCE_LIST", "DIMENSION_LABELS"}
                                                          | ({"CLASS", "NAME"} if is_scale else set()))
                                              if view != "tail_names_only" else ([], ["attributes not inspected"]))
                            scale_name = obj.attrs.get("NAME", b"") if is_scale else b""
                            if isinstance(scale_name, bytes):
                                scale_name = scale_name.decode("utf-8", "replace")
                            entry = {"path": child_path, "address": object_address,
                                     "committed_type": obj.id.get_type().committed(),
                                     "committed_type_address": (int(h5py.h5o.get_info(obj.id.get_type()).addr)
                                                                if obj.id.get_type().committed() else None),
                                     "aliases": [], "attributes": attrs,
                                     "attributes_omitted": omitted, "is_scale": is_scale,
                                     "scale_name": str(scale_name), "dimensions": []}
                            datasets.append(entry)
                            by_address[object_address] = entry
                        elif isinstance(obj, h5py.Datatype):
                            attrs, omitted = read_attributes(obj)
                            named_types.append({"path": child_path, "address": object_address,
                                                "type": obj.id.encode().hex(),
                                                "attributes": attrs, "attributes_omitted": omitted})
                        else:
                            skipped.append({"path": child_path, "reason": "unsupported object class"})
                    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                        issues.append({"path": child_path, "reason": str(exc)[:300]})
            if view != "tail_names_only":
                for entry in datasets:
                    try:
                        obj = handle[entry["path"]]
                        for axis in range(obj.ndim):
                            dimension = obj.dims[axis]
                            scales = []
                            for scale in dimension.values():
                                anchor = by_address.get(int(h5py.h5o.get_info(scale.id).addr))
                                if anchor is not None:
                                    scales.append(anchor["path"])
                                else:
                                    issues.append({"path": entry["path"],
                                                   "reason": "dimension scale could not be inventoried"})
                            entry["dimensions"].append({"axis": axis, "label": dimension.label,
                                                         "scales": scales})
                    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                        entry["context_issue"] = str(exc)[:300]
                        issues.append({"path": entry["path"], "reason": "dimension context: " + str(exc)[:300]})
    return {"groups": groups, "group_aliases": group_aliases,
            "soft_links": soft_links, "external_links": external_links,
            "datasets": datasets, "named_types": named_types, "skipped": skipped, "issues": issues,
            "complete": not issues, "inspection_view": root_correction or view}


def _inventory(image: Path, directory: Path, *, root="/", budget=None) -> dict[str, Any]:
    from .recovery import RecoveryError

    response = directory / "inventory.json"
    budget = budget or _metadata_budget()
    from dataclasses import asdict
    from .source_session import worker_environment
    environment = worker_environment()
    environment["HDF5_PLUGIN_PRELOAD"] = "::"
    environment.pop("HDF5_PLUGIN_PATH", None)
    environment.pop("HDF5_EXTFILE_PREFIX", None)
    try:
        result = run_worker([sys.executable, "-m", "h5reclaim.whole_file", str(image), str(response), root, json.dumps(asdict(budget))],
                            env=environment, timeout_seconds=budget.max_seconds, memory_bytes=budget.worker_memory_bytes)
    except subprocess.TimeoutExpired as exc:
        raise RecoveryError("whole-file inventory exceeded its deadline") from exc
    if not response.is_file() or response.stat().st_size > budget.max_metadata_bytes * 4:
        raise RecoveryError("whole-file inventory worker did not finish; select a dataset explicitly")
    message = json.loads(response.read_text(encoding="utf-8"))
    if result.returncode or message.get("status") != "ok":
        raise RecoveryError("whole-file inventory failed: " + message.get("detail", "unknown failure"))
    return message["inventory"]


def _restore_attributes(obj, records: list[dict], *, reference_paths=None) -> list[str]:
    import h5py
    import numpy as np
    from .logical_types import decode_value, encode_value, token_bytes

    omitted = []
    for record in records:
        name = record["name"]
        try:
            typ = h5py.h5t.decode(bytes.fromhex(record["type"]))
            if name in obj.attrs:
                del obj.attrs[name]
            if record.get("null_space"):
                attr = h5py.h5a.create(obj.id, name.encode("utf-8"), typ, h5py.h5s.create(h5py.h5s.NULL))
                attr.close()
            elif "logical_values" in record:
                shape = tuple(record["shape"])
                block = np.empty(shape, dtype=typ.dtype)
                for index, token in zip(np.ndindex(shape), record["logical_values"]):
                    block[index] = decode_value(token, typ, obj.file, reference_paths or {})
                obj.attrs.create(name, block, dtype=typ.dtype)
                if not obj.attrs.get_id(name).get_type().equal(typ):
                    raise ValueError("logical attribute type changed")
                reverse = {int(h5py.h5o.get_info(obj.file[path].id).addr): address
                           for address, path in (reference_paths or {}).items()}
                checked = np.asarray(obj.attrs[name], dtype=typ.dtype)
                for index, token in zip(np.ndindex(shape), record["logical_values"]):
                    observed = encode_value(checked[index], typ, obj.file, address_map=reverse, max_bytes=max(65536, len(token_bytes(token)) * 2))
                    if token_bytes(observed) != token_bytes(token):
                        raise ValueError("logical attribute readback differs")
            elif record.get("variable_string"):
                payload = base64.b64decode(record["value"], validate=True)
                obj.attrs.create(name, payload, dtype=typ.dtype)
                if not obj.attrs.get_id(name).get_type().equal(typ):
                    raise ValueError("attribute type changed")
            else:
                payload = base64.b64decode(record["value"], validate=True)
                shape = tuple(record["shape"])
                space = h5py.h5s.create_simple(shape) if shape else h5py.h5s.create(h5py.h5s.SCALAR)
                attr = h5py.h5a.create(obj.id, name.encode("utf-8"), typ, space)
                block = np.frombuffer(payload, dtype=f"V{typ.get_size()}").copy().reshape(shape)
                attr.write(block, mtype=typ)
                checked = np.empty(shape, dtype=block.dtype)
                attr.read(checked, mtype=typ)
                if checked.tobytes() != payload:
                    raise ValueError("attribute readback differs")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            if name in obj.attrs:
                del obj.attrs[name]
            omitted.append(name)
    return omitted


def _rebase(value: Any, replacements: dict[str, str], prefix: str, unit_metadata: str = "/_h5reclaim") -> Any:
    if isinstance(value, dict):
        return {key: _rebase(item, replacements, prefix, unit_metadata) for key, item in value.items()}
    if isinstance(value, list):
        return [_rebase(item, replacements, prefix, unit_metadata) for item in value]
    if isinstance(value, str):
        if value in replacements:
            return replacements[value]
        if value.startswith(unit_metadata + "/"):
            return prefix + value[len(unit_metadata):]
    return value


def _augment_detached_inventory(image, inventory, error=None):
    """Retain known names and add surviving headers whose names were lost."""
    from .object_discovery import scan_local, _validate_candidates
    from .large_streaming import LargeBudget
    from .source_session import reused_image
    shared = reused_image(image)
    budget = LargeBudget(**shared["budget"]) if shared else LargeBudget()
    discovery = _validate_candidates(image, scan_local(image, budget), budget)
    if inventory is None:
        inventory = {"groups": [{"path": "/", "attributes": [], "attributes_omitted": []}],
            "datasets": [], "group_aliases": [], "soft_links": [], "external_links": [], "skipped": [],
            "issues": [{"path": "/", "reason": "original namespace unreadable: " + str(error)[:300]}],
            "complete": False, "inspection_view": "detached checksummed dataset headers"}
    known = {item.get("address") for item in inventory["datasets"]}
    added = []
    for item in discovery["datasets"]:
        if item["address"] in known:
            continue
        # These names describe physical objects, never asserted original paths.
        path = f"/recovered/object_{item['address']:x}"
        if any(entry["path"] == path for entry in inventory["datasets"]):
            path = f"/recovered/detached_{item['address']:x}"
        entry = {"path": path, "address": item["address"], "detached": True,
                 "aliases": [], "attributes": [], "attributes_omitted": [],
                 "is_scale": False, "dimensions": []}
        inventory["datasets"].append(entry)
        added.append(path)
    if added and not any(item["path"] == "/recovered" for item in inventory["groups"]):
        inventory["groups"].append({"path": "/recovered", "attributes": [], "attributes_omitted": []})
    inventory["detached_discovery"] = {"objects_added": added, "complete_namespace": False}
    return inventory


def rescue_all(source: str | Path, output: str | Path, report_path: str | Path, *,
               streaming_budget: dict[str, Any] | None = None,
               related_files: str | Path | None = None,
               resume_dir: str | Path | None = None, _bundle_kind=None, _bundle_manifest=None) -> dict[str, Any]:
    """Recover discovered local datasets and rebuild their available context."""
    import h5py
    from .large_streaming import LargeBudget, sparse_snapshot, _deadline
    from .metadata import UnsupportedCase
    from .recovery import (VERSION, RecoveryError, _validate_paths, _verify_source, sha256_file)
    from .rescue import auto_rescue
    from .source_session import share_image
    from .filter_registry import register_optional
    register_optional()
    import time
    from dataclasses import asdict

    source, output, report_path = Path(source).absolute(), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    budget = LargeBudget(**(streaming_budget or {}))
    if _bundle_kind == "family":
        from .family_bundle import _manifest
        _, members = _manifest(_bundle_manifest)
        if source.resolve(strict=True) != Path(members[0]['path']).resolve(strict=True):
            raise RecoveryError('Family source argument must be manifest member zero')
    elif _bundle_kind == "split":
        from .split_bundle import _load_manifest
        metadata, _, _, _ = _load_manifest(_bundle_manifest)
        if source.resolve(strict=True) != metadata.resolve(strict=True):
            raise RecoveryError('Split source argument must be the metadata member')
    deadline = time.monotonic() + budget.max_seconds
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, copied):
        with share_image(image, digest, size, copied, budget), tempfile.TemporaryDirectory(prefix=".h5reclaim-all-", dir=output.parent) as outdir, ExitStack() as related_stack:
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-all-", dir=report_path.parent) as repdir:
                directory = Path(outdir)
                if _bundle_kind:
                    from .bundle_stream import inventory_bundle
                    inventory = inventory_bundle(_bundle_kind, _bundle_manifest, directory, budget)
                else:
                    inventory = _inventory(image, directory, budget=budget)
                from .related_recovery import RelatedRecovery
                related = related_stack.enter_context(RelatedRecovery(related_files, budget, directory)) if related_files else None
                if related:
                    inventory = related.expand(inventory, digest)
                checkpoint = None
                reused = 0
                if resume_dir is not None:
                    from .checkpoint import Checkpoint
                    checkpoint = related_stack.enter_context(Checkpoint(resume_dir, digest, {"budget": asdict(budget),
                        "related_files_sha256": sha256_file(Path(related_files)) if related_files else None,
                        "bundle_manifest_sha256": sha256_file(Path(_bundle_manifest)) if _bundle_manifest else None}))
                for link in inventory.get("external_links", []):
                    if related_files is None:
                        inventory["skipped"].append({"path": link["path"], "reason": "external link needs --related-files"})
                    else:
                        inventory["datasets"].append({"path": link["path"], "aliases": [], "attributes": [],
                            "attributes_omitted": [], "is_scale": False, "dimensions": [], "dependency_route": "external_link"})
                if not inventory["datasets"] and not inventory["complete"]:
                    raise RecoveryError("no dataset could be discovered; select a known dataset path explicitly")
                staged_output, staged_report = directory / "output.h5", Path(repdir) / "report.json"
                records, failures, context = [], [], {"groups": [], "datasets": [], "scales": []}
                from .output_annotations import available_metadata_group, report_metadata_group, ensure_group, commit_type
                from .userblock import matlab_header
                metadata_group = available_metadata_group((entry["path"] for section in
                    ("groups", "datasets", "named_types", "external_links", "soft_links", "group_aliases")
                    for entry in inventory.get(section, [])), parent="/#refs#" if matlab_header(image) else "/")
                from .userblock import userblock_size, copy_userblock
                application_header_size = userblock_size(image)
                with h5py.File(staged_output, "x", track_order=True, userblock_size=application_header_size) as merged:
                    root_meta = ensure_group(merged, metadata_group + "/datasets")
                    for group in inventory["groups"]:
                        target = merged["/"] if group["path"] == "/" else ensure_group(merged, group["path"])
                        # Dense attributes and creation order are needed by common scientific containers.
                        omitted = group["attributes_omitted"] + _restore_attributes(target, group["attributes"])
                        context["groups"].append({"path": group["path"], "attributes_omitted": omitted})
                    context["named_types"] = []
                    type_paths = {}
                    for entry in inventory.get("named_types", []):
                        parent_path, name = entry["path"].rsplit("/", 1)
                        parent = ensure_group(merged, parent_path or "/")
                        type_key = (entry.get("storage_digest", digest), entry["address"])
                        if type_key in type_paths:
                            parent[name] = merged[type_paths[type_key]]
                        else:
                            typ = h5py.h5t.decode(bytes.fromhex(entry["type"]))
                            commit_type(parent, name.encode("utf-8"), typ)
                            type_paths[type_key] = entry["path"]
                        omitted = entry["attributes_omitted"] + _restore_attributes(merged[entry["path"]], entry["attributes"])
                        context["named_types"].append({"path": entry["path"], "attributes_omitted": omitted})
                    for number, entry in enumerate(inventory["datasets"]):
                        _deadline(deadline)
                        path = entry["path"]
                        unit_output, unit_report = directory / f"dataset-{number}.h5", directory / f"dataset-{number}.json"
                        selection_progress = str(checkpoint.directory / ("selections-" + hashlib.sha256(path.encode()).hexdigest())) if checkpoint else None
                        try:
                            cached = checkpoint.load(path) if checkpoint else None
                            if cached:
                                recovered, unit_output, unit_report = cached
                                reused += 1
                            else:
                                remaining_budget = {**asdict(budget), "max_seconds": max(0.001, deadline - time.monotonic())}
                                if related_files is not None and not entry.get("storage_image"):
                                    from .dependency_routes import inspect_dependencies, load_dependency_manifest
                                    observation = inspect_dependencies(image, path)
                                    kinds = {item["kind"] for item in observation["dependencies"]}
                                    route = ("external_link" if "external_link" in kinds else
                                             "vds" if "virtual_source" in kinds else
                                             "external_raw" if "external_raw_storage" in kinds else None)
                                else:
                                    route = None
                                if _bundle_kind:
                                    from .route_worker import run_route
                                    recovered = run_route("native_" + _bundle_kind, unit_output, unit_report,
                                        manifest=str(Path(_bundle_manifest).absolute()), dataset=path, annotate_history=True,
                                        streaming_budget=json.dumps(remaining_budget),
                                        **({'resume_dir': selection_progress} if selection_progress else {}))
                                elif entry.get("storage_image"):
                                    from .route_worker import run_route
                                    with related.activate(entry["storage_image"]):
                                        recovered = run_route("native_stream", unit_output, unit_report,
                                            source=entry["storage_image"], dataset=path,
                                            source_dataset=entry["storage_path"], annotate_history=True,
                                            streaming_budget=json.dumps(remaining_budget),
                                            **({'resume_dir': selection_progress} if selection_progress else {}))
                                elif entry.get("detached"):
                                    from .route_worker import run_route
                                    recovered = run_route("object", unit_output, unit_report, source=str(image),
                                        dataset=path, object_address=hex(entry["address"]), annotate_history=True,
                                        streaming_budget=json.dumps(remaining_budget))
                                elif route:
                                    from .route_worker import run_route
                                    names = {item["file_name"] for item in observation["dependencies"]}
                                    declared = load_dependency_manifest(related_files)
                                    unit_manifest = directory / f"related-{number}.json"
                                    unit_manifest.write_text(json.dumps({"schema_version": 1,
                                        "files": declared["files"] if route == "vds" else [item for item in declared["files"] if item["declared_name"] in names]}),
                                        encoding="utf-8")
                                    recovered = run_route(route, unit_output, unit_report, source=str(image),
                                        dataset=path, manifest=str(unit_manifest), annotate_history=True,
                                        streaming_budget=json.dumps(remaining_budget))
                                elif entry.get("committed_type") or isinstance(inventory.get("inspection_view"), dict):
                                    from .route_worker import run_route
                                    recovered = run_route("native_stream", unit_output, unit_report, source=str(image),
                                        dataset=path, annotate_history=True, streaming_budget=json.dumps(remaining_budget))
                                else:
                                    recovered = auto_rescue(image, path, unit_output, unit_report,
                                                        audit_science_context=False, streaming_budget=remaining_budget,
                                                        resume_dir=selection_progress)
                                if checkpoint:
                                    checkpoint.store(path, unit_output, unit_report)
                        except (ValueError, OSError, RuntimeError) as exc:
                            if (inventory.get("detached_discovery") and "address" in entry
                                    and not entry.get("detached") and not unit_output.exists()):
                                try:
                                    from .route_worker import run_route
                                    recovered = run_route("object", unit_output, unit_report, source=str(image),
                                        dataset=path, object_address=hex(entry["address"]), annotate_history=True,
                                        streaming_budget=json.dumps({**asdict(budget),
                                            "max_seconds": max(0.001, deadline - time.monotonic())}))
                                    if checkpoint:
                                        checkpoint.store(path, unit_output, unit_report)
                                except (ValueError, OSError, RuntimeError) as detached_error:
                                    failures.append({"path": path, "reason": str(detached_error)[:1200]})
                                    continue
                            else:
                                failures.append({"path": path, "reason": str(exc)[:1200]})
                                continue
                        if recovered.get("source", {}).get("sha256_before") != entry.get("storage_digest", digest):
                            raise RecoveryError("a dataset recovery used a different source snapshot")
                        identifier = f"d{number:06d}"
                        prefix = metadata_group + f"/datasets/{identifier}"
                        unit_metadata = report_metadata_group(recovered)
                        with h5py.File(unit_output, "r") as unit:
                            parent_path, name = path.rsplit("/", 1)
                            parent = ensure_group(merged, parent_path or "/")
                            copy_properties = h5py.h5p.create(h5py.h5p.OBJECT_COPY)
                            copy_properties.set_copy_object(0x40)  # H5O_COPY_MERGE_COMMITTED_DTYPE_FLAG
                            type_key = (entry.get("storage_digest", digest), entry.get("committed_type_address"))
                            if type_key in type_paths:
                                import ctypes
                                from .native_bindings import public_function
                                suggest = public_function("H5Padd_merge_committed_dtype_path", [ctypes.c_int64, ctypes.c_char_p])
                                if suggest(copy_properties.id, type_paths[type_key].encode("utf-8")) < 0:
                                    raise RecoveryError("cannot select the original committed datatype")
                            h5py.h5o.copy(unit.id, path.encode("utf-8"), parent.id, name.encode("utf-8"), copypl=copy_properties)
                            unit.copy(unit_metadata, root_meta, name=identifier)
                        restored = merged[path]
                        collisions = recovered.get("selected_annotation_collisions", [])
                        for annotation in ("h5reclaim_chunk_status", "h5reclaim_element_status"):
                            if annotation in restored.attrs and annotation not in collisions:
                                value = restored.attrs[annotation]
                                if isinstance(value, str) and value.startswith(unit_metadata + "/"):
                                    restored.attrs[annotation] = prefix + value[len(unit_metadata):]
                        if "h5reclaim_warning" in restored.attrs and "h5reclaim_warning" not in collisions:
                            restored.attrs["h5reclaim_warning"] = "Use this dataset's status map; the whole-file report records restored context and unresolved values."
                        omitted = entry["attributes_omitted"] + _restore_attributes(restored, entry["attributes"])
                        aliases = []
                        for alias in entry["aliases"]:
                            parent_path, name = alias.rsplit("/", 1)
                            if alias not in merged:
                                ensure_group(merged, parent_path or "/")[name] = restored
                                aliases.append(alias)
                        report = _rebase(recovered, {str(image): str(source), str(unit_output): str(output),
                            recovered.get("source", {}).get("path", str(image)): entry.get("storage_source", str(source))}, prefix, unit_metadata)
                        report["output_path"] = str(output)
                        report["whole_file_metadata_group"] = prefix
                        report["metadata_group"] = prefix
                        report["dataset"]["whole_file_attributes_omitted"] = omitted
                        serialized = json.dumps(report, sort_keys=True, indent=2) + "\n"
                        meta = merged[prefix]
                        if "report_json" in meta:
                            del meta["report_json"]
                        meta.create_dataset("report_json", data=serialized, dtype=h5py.string_dtype("utf-8"))
                        records.append({"path": path, "aliases": aliases, "report": report})
                        context["datasets"].append({"path": path, "attributes_omitted": omitted})
                        merged.flush()
                        if staged_output.stat().st_size > budget.max_output_bytes:
                            raise UnsupportedCase("whole-file output exceeds its configured byte budget")
                    if not records and inventory["datasets"]:
                        raise RecoveryError("no dataset could be exported: " + "; ".join(item["reason"] for item in failures[:3]))
                    available = {item["path"] for item in records}
                    from .logical_types import decode_value, encode_value, token_bytes, write_value
                    file_paths = {}
                    for entry in inventory["groups"] + inventory["datasets"] + inventory.get("named_types", []):
                        if "address" in entry and entry["path"] in merged:
                            file_paths.setdefault(entry.get("storage_digest", digest), {})[entry["address"]] = entry["path"]
                    for item in records:
                        report = item["report"]
                        paths = file_paths.get(report["source"]["sha256_before"], {})
                        pending_path = report.get("deferred_references")
                        if pending_path and pending_path in merged:
                            target = merged[item["path"]]
                            typ = target.id.get_type()
                            status = merged[report["element_status"]]
                            unresolved = 0
                            for raw_record in merged[pending_path]:
                                pending = json.loads(raw_record)
                                index, token = tuple(pending["index"]), pending["value"]
                                token_paths = file_paths.get(pending.get('source_file_sha256', report['source']['sha256_before']), {})
                                token_reverse = {int(h5py.h5o.get_info(merged[path].id).addr): address
                                                 for address, path in token_paths.items()}
                                try:
                                    value = decode_value(token, typ, merged, token_paths)
                                    write_value(target, index, value)
                                    checked = encode_value(target[index], typ, merged, address_map=token_reverse,
                                                           max_bytes=budget.max_chunk_bytes, max_depth=budget.max_type_depth)
                                    if token_bytes(checked) != token_bytes(token):
                                        raise ValueError("remapped reference readback differs")
                                    if status[index] != 1:
                                        report["accepted_elements"] += 1
                                        report["unknown_elements"] -= 1
                                    status[index] = 1
                                except (OSError, RuntimeError, ValueError, TypeError, KeyError):
                                    if status[index] == 1:
                                        report["accepted_elements"] -= 1
                                        report["unknown_elements"] += 1
                                    status[index] = 4
                                    unresolved += 1
                            report["deferred_reference_elements"] = unresolved
                            report["reference_remapping"] = {"unresolved": unresolved, "scope": "recovered whole-file objects"}
                            report["native_value_sha256_scope"] = "values accepted in the unit export before whole-file reference remapping"
                            report["outcome"] = "partial" if report["unknown_elements"] else "complete"
                    for entry in inventory["groups"] + inventory["datasets"] + inventory.get("named_types", []):
                        if entry["path"] not in merged:
                            continue
                        omitted = entry["attributes_omitted"] + _restore_attributes(merged[entry["path"]], entry["attributes"],
                                                                                  reference_paths=file_paths.get(entry.get("storage_digest", digest), {}))
                        section = (context["groups"] if entry in inventory["groups"] else
                                   context["named_types"] if entry in inventory.get("named_types", []) else context["datasets"])
                        for item in section:
                            if item["path"] == entry["path"]:
                                item["attributes_omitted"] = omitted
                        for item in records:
                            if item["path"] == entry["path"]:
                                item["report"]["dataset"]["whole_file_attributes_omitted"] = omitted
                    context["soft_links"] = []
                    for link in inventory.get("soft_links", []):
                        parent, name = link["path"].rsplit("/", 1)
                        ensure_group(merged, parent or "/")[name] = h5py.SoftLink(link["target"])
                        context["soft_links"].append(link)
                    context["group_aliases"] = []
                    for alias in inventory["group_aliases"]:
                        if alias["path"] not in merged and alias["target"] in merged:
                            parent, name = alias["path"].rsplit("/", 1)
                            ensure_group(merged, parent or "/")[name] = merged[alias["target"]]
                            context["group_aliases"].append(alias)
                    for entry in inventory["datasets"]:
                        if entry["path"] in available and entry["is_scale"]:
                            target = merged[entry["path"]]
                            if not target.is_scale:
                                target.make_scale(entry["scale_name"])
                    for entry in inventory["datasets"]:
                        if entry["path"] not in available:
                            continue
                        target = merged[entry["path"]]
                        for dimension in entry["dimensions"]:
                            axis = dimension["axis"]
                            try:
                                target.dims[axis].label = dimension["label"]
                                for scale in dimension["scales"]:
                                    if scale in available:
                                        target.dims[axis].attach_scale(merged[scale])
                                        context["scales"].append({"dataset": entry["path"], "axis": axis, "scale": scale})
                                    else:
                                        context.setdefault("scales_omitted", []).append(scale)
                            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                                context.setdefault("issues", []).append({"path": entry["path"], "reason": str(exc)[:300]})
                    context_complete = (not context.get("issues") and not context.get("scales_omitted")
                                        and not any(item["attributes_omitted"]
                                                    for item in context["groups"] + context["datasets"] + context["named_types"]))
                    complete = (inventory["complete"] and context_complete and not inventory["skipped"] and not failures
                                and all(item["report"].get("outcome") == "complete" for item in records))
                    result = {"schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                              "mode": "whole_file_recovery", "bundle_driver": _bundle_kind, "metadata_group": metadata_group, "outcome": "complete" if complete else "partial",
                              "source": {"path": str(source), "size_bytes": size, "sha256_before": digest,
                                         "sha256_after": digest, "snapshot_physical_data_bytes_copied": copied},
                              "userblock": {"size_bytes": application_header_size},
                              "output_path": str(output), "datasets_discovered": len(inventory["datasets"]),
                              "datasets_exported": len(records), "datasets_failed": len(failures),
                              "datasets": records, "failures": failures, "scientific_context": context,
                              "checkpoint": {"enabled": checkpoint is not None, "datasets_reused": reused,
                                             "granularity": "completed datasets and native selections"},
                              "inventory": {key: inventory[key] for key in ("complete", "issues", "skipped", "inspection_view")},
                              "historical_integrity": {"status": "unknown", "comparison": "no prior capture supplied"}}
                    serialized = json.dumps(result, sort_keys=True, indent=2) + "\n"
                    for item in records:
                        meta = merged[item["report"]["whole_file_metadata_group"]]
                        meta["report_json"][()] = json.dumps(item["report"], sort_keys=True, indent=2) + "\n"
                    if len(serialized.encode("utf-8")) > budget.max_metadata_bytes * 4:
                        raise UnsupportedCase("whole-file evidence report exceeds its publication budget")
                    merged[metadata_group].create_dataset("report_json", data=serialized,
                                                       dtype=h5py.string_dtype("utf-8"))
                    merged[metadata_group].attrs["source_sha256"] = digest
                    merged.flush()
                copy_userblock(image, staged_output, application_header_size, budget.block_bytes)
                staged_report.write_text(serialized, encoding="utf-8")
                check = hashlib.sha256()
                with image.open("rb") as stream:
                    while block := stream.read(budget.block_bytes):
                        _deadline(deadline)
                        check.update(block)
                if check.hexdigest() != digest:
                    raise RecoveryError("whole-file source snapshot changed during recovery")
                if related:
                    related.verify()
                related_stack.close()
                _verify_source(source, identity, digest)
                _validate_paths(source, output, report_path)
                os.link(staged_report, report_path)
                try:
                    os.link(staged_output, output)
                except Exception:
                    report_path.unlink(missing_ok=True)
                    raise
                return result


def main() -> int:
    if len(sys.argv) not in (3, 4, 5):
        return 2
    response = Path(sys.argv[2])
    try:
        os.environ["HDF5_PLUGIN_PRELOAD"] = "::"
        os.environ.pop("HDF5_PLUGIN_PATH", None)
        os.environ.pop("HDF5_EXTFILE_PREFIX", None)
        from .native_worker import _apply_memory_limit
        from .large_streaming import LargeBudget
        budget = LargeBudget(**json.loads(sys.argv[4])) if len(sys.argv) > 4 else _metadata_budget()
        _apply_memory_limit(budget.worker_memory_bytes)
        from .source_session import activate_worker_session
        activate_worker_session()
        image = Path(sys.argv[1])
        try:
            inventory = _inventory_local(image, sys.argv[3] if len(sys.argv) > 3 else "/", budget=budget)
            if inventory["issues"] and any(
                    not any(marker in item["reason"] for marker in ("inventory is incomplete", "too deep"))
                    for item in inventory["issues"]):
                try:
                    inventory = _augment_detached_inventory(image, inventory)
                except (OSError, RuntimeError, KeyError, ValueError) as exc:
                    inventory["issues"].append({"path": "/", "reason": "detached discovery: " + str(exc)[:300]})
        except (OSError, RuntimeError, KeyError, ValueError) as exc:
            inventory = _augment_detached_inventory(image, None, exc)
        result = {"status": "ok", "inventory": inventory}
        encoded = json.dumps(result).encode("utf-8")
        if len(encoded) > budget.max_metadata_bytes * 4:
            raise ValueError("whole-file inventory exceeds the response budget")
        response.write_bytes(encoded)
        return 0
    except Exception as exc:
        response.write_text(json.dumps({"status": "error", "detail": str(exc)[:300]}), encoding="utf-8")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
