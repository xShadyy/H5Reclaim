"""Bounded, read-only inventory of local HDF5 dataset metadata.

Survey is a preflight aid, not a recovery attempt. It never requests dataset
values or chunk payloads. An eligible result only means that the observed
metadata fits the current recovery entry conditions; damaged payloads or
contradictory index evidence can still make recovery fail.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import h5py

from .format import FormatError, H5File, UnsupportedFormat
from .modern_indexes import ModernH5File
from .recovery import (
    MAX_CHUNKS,
    MAX_NODES,
    _verify_source,
    sha256_file,
    source_snapshot,
)


MAX_LINKS = 10_000
MAX_GROUPS = 4_096
MAX_DATASETS = 4_096
MAX_DEPTH = 64
MAX_PATH_CHARS = 4_096
MAX_FILTERS = 32
MAX_ISSUES = 128
MAX_TEXT_CHARS = 300


class SurveyError(ValueError):
    """The source cannot be safely surveyed within the declared limits."""


def _short(value: object) -> str:
    value_text = str(value)
    return value_text[:MAX_TEXT_CHARS] + ("..." if len(value_text) > MAX_TEXT_CHARS else "")


def _reason(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": _short(detail)}


def _layout_name(layout: int) -> str:
    return {
        h5py.h5d.COMPACT: "compact",
        h5py.h5d.CONTIGUOUS: "contiguous",
        h5py.h5d.CHUNKED: "chunked",
        h5py.h5d.VIRTUAL: "virtual",
    }.get(layout, f"unknown_{layout}")


def _describe_dataset(dataset: h5py.Dataset, path: str) -> dict[str, Any]:
    """Describe creation metadata only; never use ``dataset[...]``."""
    result: dict[str, Any] = {
        "selected_path": path,
        "aliases": [],
        "object_address": int(h5py.h5o.get_info(dataset.id).addr),
    }
    reasons: list[dict[str, str]] = []
    try:
        shape = tuple(int(length) for length in dataset.shape)
        chunks = dataset.chunks
        chunks = tuple(int(length) for length in chunks) if chunks is not None else None
        maxshape = dataset.maxshape
        creation = dataset.id.get_create_plist()
        nfilters = creation.get_nfilters()
        filters: list[dict[str, Any]] = []
        for i in range(min(nfilters, MAX_FILTERS)):
            filter_id, _flags, _values, name = creation.get_filter(i)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")
            filters.append({"id": int(filter_id), "name": _short(name)})
        if nfilters > MAX_FILTERS:
            reasons.append(_reason("too_many_filters", "filter metadata exceeds survey limit"))

        layout = _layout_name(creation.get_layout())
        dtype = str(dataset.dtype)
        result.update({
            "rank": len(shape),
            "shape": list(shape),
            "maxshape": list(maxshape) if maxshape is not None else None,
            "dtype": dtype[:MAX_TEXT_CHARS],
            "dtype_truncated": len(dtype) > MAX_TEXT_CHARS,
            "chunks": list(chunks) if chunks is not None else None,
            "layout": layout,
            "filters": filters,
            "external_storage": creation.get_external_count() != 0,
            "virtual_storage": bool(dataset.is_virtual),
        })

        if path == "/_h5reclaim" or path.startswith("/_h5reclaim/"):
            reasons.append(_reason("reserved_path", "the output metadata namespace is reserved"))
        rank = len(shape)
        if rank not in (1, 2) or chunks is None or len(chunks) != rank or layout != "chunked":
            reasons.append(_reason("layout", "requires a rank-one or rank-two chunked dataset"))
        if maxshape != shape:
            reasons.append(_reason("extendible", "extendible datasets are unsupported"))
        if chunks is not None and rank in (1, 2) and len(chunks) == rank:
            if any(length <= 0 or chunk <= 0 or length % chunk for length, chunk in zip(shape, chunks)):
                reasons.append(_reason("edge_chunks", "dimensions must be positive and divisible by chunks"))
            element_count = 1
            chunk_elements = 1
            grid_count = 1
            for length, chunk in zip(shape, chunks):
                element_count *= length
                chunk_elements *= chunk
                if chunk > 0:
                    grid_count *= (length + chunk - 1) // chunk
            if element_count > 1_048_576:
                reasons.append(_reason("element_limit", "dataset exceeds 1,048,576 elements"))
            if chunk_elements * (8 if rank == 1 else 4) > 1_048_576:
                reasons.append(_reason("chunk_size_limit", "chunk exceeds 1 MiB"))
            if grid_count > MAX_CHUNKS:
                reasons.append(_reason("chunk_count_limit", f"dataset exceeds {MAX_CHUNKS} chunks"))
        else:
            if any(length <= 0 for length in shape):
                reasons.append(_reason("empty_dimension", "empty dimensions are unsupported"))

        datatype = dataset.id.get_type()
        uint32 = (
            datatype.get_class() == h5py.h5t.INTEGER
            and datatype.get_size() == 4
            and datatype.get_sign() == h5py.h5t.SGN_NONE
            and datatype.get_order() == h5py.h5t.ORDER_LE
            and datatype.get_precision() == 32
            and datatype.get_offset() == 0
            and datatype.get_pad() == (h5py.h5t.PAD_ZERO, h5py.h5t.PAD_ZERO)
        ) if datatype.get_class() == h5py.h5t.INTEGER else False
        float64 = (
            datatype.get_class() == h5py.h5t.FLOAT
            and datatype.get_size() == 8
            and datatype.get_order() == h5py.h5t.ORDER_LE
            and datatype.get_precision() == 64
            and datatype.get_offset() == 0
            and datatype.get_pad() == (h5py.h5t.PAD_ZERO, h5py.h5t.PAD_ZERO)
            and datatype.get_fields() == (63, 52, 11, 0, 52)
            and datatype.get_ebias() == 1023
            and datatype.get_norm() == h5py.h5t.NORM_IMPLIED
        ) if datatype.get_class() == h5py.h5t.FLOAT else False
        if not ((rank == 2 and uint32) or (rank == 1 and float64)):
            reasons.append(_reason("datatype", "requires rank-two canonical <u4 or rank-one canonical IEEE <f8"))
        filter_info = [creation.get_filter(i) for i in range(min(nfilters, 2))]
        supported_float_filters = (
            nfilters == 2
            and [item[0] for item in filter_info] == [h5py.h5z.FILTER_FLETCHER32, h5py.h5z.FILTER_DEFLATE]
            and filter_info[0][2] == ()
            and len(filter_info[1][2]) == 1
            and 0 <= filter_info[1][2][0] <= 9
        )
        if not ((rank == 2 and nfilters == 0) or (rank == 1 and supported_float_filters)):
            reasons.append(_reason("filters", "requires unfiltered rank-two chunks or rank-one Fletcher32 then DEFLATE"))
        if result["external_storage"] or result["virtual_storage"]:
            reasons.append(_reason("external_storage", "external or virtual storage is unsupported"))
        if result["dtype_truncated"]:
            reasons.append(_reason("dtype_limit", "datatype description exceeds survey limit"))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        reasons.append(_reason("metadata_unreadable", f"could not inspect dataset metadata: {exc}"))

    result["support"] = {"status": "unsupported" if reasons else "candidate", "reasons": reasons}
    return result


def _probe_index(source: Path, entries: list[dict[str, Any]]) -> None:
    """Read selected index metadata without requesting chunk payloads."""
    candidates = [entry for entry in entries if entry["support"]["status"] == "candidate"]
    if not candidates:
        return
    try:
        legacy = H5File(source)
    except UnsupportedFormat:
        # The legacy parser intentionally stops at newer superblocks. Modern
        # parsing starts from the checksum-verified v2/v3 superblock and the
        # selected v2 object header, never from signatures found by scanning.
        try:
            with ModernH5File(source) as modern:
                for entry in candidates:
                    reasons: list[dict[str, str]] = entry["support"]["reasons"]
                    try:
                        rank = entry["rank"]
                        index = modern.read_index(
                            entry["object_address"], tuple(entry["shape"]),
                            tuple(entry["chunks"]), 8 if rank == 1 else 4,
                            maxshape=tuple(entry["maxshape"]),
                            filters=tuple(item["id"] for item in entry["filters"]),
                            max_chunks=MAX_CHUNKS,
                        )
                        entry["index"] = {
                            "type": index.index_type,
                            "layout_version": index.layout_version,
                            "allocated_chunks": len(index.chunks),
                            "broken_links": 0,
                        }
                    except UnsupportedFormat as exc:
                        reasons.append(_reason("index_unsupported", str(exc)))
                        entry["support"]["status"] = "unsupported"
                    except (FormatError, OSError, ValueError, KeyError) as exc:
                        reasons.append(_reason("index_unreadable", str(exc)))
                        entry["support"]["status"] = "indeterminate"
        except UnsupportedFormat as exc:
            for entry in candidates:
                entry["support"]["reasons"].append(_reason("file_format_unsupported", str(exc)))
                entry["support"]["status"] = "unsupported"
        except (FormatError, OSError, ValueError, KeyError) as exc:
            for entry in candidates:
                entry["support"]["reasons"].append(_reason("file_format_unreadable", str(exc)))
                entry["support"]["status"] = "indeterminate"
        return
    except (FormatError, OSError, ValueError, KeyError) as exc:
        for entry in candidates:
            entry["support"]["reasons"].append(_reason("file_format_unreadable", str(exc)))
            entry["support"]["status"] = "indeterminate"
        return
    try:
        with legacy as reader:
            for entry in candidates:
                reasons: list[dict[str, str]] = entry["support"]["reasons"]
                try:
                    rank = entry["rank"]
                    element_size = 8 if rank == 1 else 4
                    layout = reader.read_dataset_layout(entry["object_address"], rank=rank)
                    if list(layout.chunk_shape) != entry["chunks"]:
                        raise FormatError("raw chunk layout disagrees with HDF5 metadata")
                    if layout.element_size != element_size:
                        raise FormatError("raw chunk element size disagrees with HDF5 metadata")
                    root = reader.read_tree(layout.root_address, rank=rank,
                                            element_size=element_size)
                    walk = reader.walk_tree(layout.root_address, max_nodes=MAX_NODES,
                                            rank=rank, element_size=element_size)
                    if root.level == 0:
                        if (
                            len(walk.nodes) != 1 or walk.broken_links
                            or root.left_sibling is not None or root.right_sibling is not None
                        ):
                            raise FormatError("level-zero root is not a complete leaf")
                    else:
                        if len(walk.broken_links) > 1:
                            raise UnsupportedFormat("more than one broken child pointer")
                        levels = {node.address: node.level for node in walk.nodes}
                        if any(levels[gap.parent_address] != 1 for gap in walk.broken_links):
                            raise UnsupportedFormat("missing internal subtree below the root")
                    entry["index"] = {
                        "type": "v1_raw_data_btree",
                        "root_level": root.level,
                        "reachable_nodes": len(walk.nodes),
                        "broken_links": len(walk.broken_links),
                    }
                except UnsupportedFormat as exc:
                    reasons.append(_reason("index_unsupported", str(exc)))
                    entry["support"]["status"] = "unsupported"
                except (FormatError, OSError, ValueError, KeyError) as exc:
                    reasons.append(_reason("index_unreadable", str(exc)))
                    entry["support"]["status"] = "indeterminate"
    except UnsupportedFormat as exc:
        for entry in candidates:
            entry["support"]["reasons"].append(_reason("file_format_unsupported", str(exc)))
            entry["support"]["status"] = "unsupported"
    except (FormatError, OSError, ValueError, KeyError) as exc:
        for entry in candidates:
            entry["support"]["reasons"].append(_reason("file_format_unreadable", str(exc)))
            entry["support"]["status"] = "indeterminate"


def survey(source: str | Path) -> dict[str, Any]:
    """Inventory bounded local hard-linked datasets without reading their values.

    Soft and external HDF5 links are counted but never resolved. The first
    encountered path to an object becomes its selected path; additional direct
    hard-link aliases are recorded. Cyclic group links are visited once.
    """
    source = Path(source)
    with source_snapshot(source) as (snapshot, digest, identity, size):
        result = _survey_snapshot(snapshot, source, size)
        if sha256_file(snapshot) != digest:
            raise SurveyError("private source snapshot changed during survey")
        _verify_source(source, identity, digest)
        return result


def _survey_snapshot(image: Path, source: Path, size: int) -> dict[str, Any]:

    entries: list[dict[str, Any]] = []
    by_address: dict[int, dict[str, Any]] = {}
    issues: list[dict[str, str]] = []
    skipped = {"soft_links": 0, "external_links": 0, "other_links": 0, "named_types": 0}
    link_count = 0
    group_count = 0
    complete = True

    def issue(code: str, detail: str) -> None:
        nonlocal complete
        complete = False
        if len(issues) < MAX_ISSUES:
            issues.append(_reason(code, detail))

    try:
        with h5py.File(image, "r") as handle:
            queue: deque[tuple[h5py.Group, str, int]] = deque([(handle["/"], "/", 0)])
            seen_groups: set[int] = set()
            while queue:
                group, prefix, depth = queue.popleft()
                address = int(h5py.h5o.get_info(group.id).addr)
                if address in seen_groups:
                    continue
                if group_count >= MAX_GROUPS:
                    issue("group_limit", f"stopped after {MAX_GROUPS} groups")
                    break
                seen_groups.add(address)
                group_count += 1
                try:
                    for name in group.keys():
                        if link_count >= MAX_LINKS:
                            issue("link_limit", f"stopped after {MAX_LINKS} local links")
                            queue.clear()
                            break
                        link_count += 1
                        path = prefix.rstrip("/") + "/" + name
                        if len(path) > MAX_PATH_CHARS:
                            issue("path_limit", "a dataset or group path exceeds the survey limit")
                            continue
                        try:
                            link = group.get(name, getlink=True)
                            if isinstance(link, h5py.ExternalLink):
                                skipped["external_links"] += 1
                                continue
                            if isinstance(link, h5py.SoftLink):
                                skipped["soft_links"] += 1
                                continue
                            if not isinstance(link, h5py.HardLink):
                                skipped["other_links"] += 1
                                continue
                            obj = group.get(name)
                            if isinstance(obj, h5py.Group):
                                if depth >= MAX_DEPTH:
                                    issue("depth_limit", f"group nesting exceeds {MAX_DEPTH} at {path}")
                                else:
                                    queue.append((obj, path, depth + 1))
                            elif isinstance(obj, h5py.Dataset):
                                object_address = int(h5py.h5o.get_info(obj.id).addr)
                                if object_address in by_address:
                                    by_address[object_address]["aliases"].append(path)
                                elif len(entries) >= MAX_DATASETS:
                                    issue("dataset_limit", f"stopped after {MAX_DATASETS} datasets")
                                    queue.clear()
                                    break
                                else:
                                    entry = _describe_dataset(obj, path)
                                    entries.append(entry)
                                    by_address[object_address] = entry
                            elif isinstance(obj, h5py.Datatype):
                                skipped["named_types"] += 1
                            else:
                                issue("unknown_object", f"unrecognized local object at {path}")
                        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                            issue("link_unreadable", f"{path}: {_short(exc)}")
                except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                    issue("group_unreadable", f"{prefix}: {_short(exc)}")
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise SurveyError(f"HDF5 could not open local metadata: {_short(exc)}") from exc

    if not complete:
        for entry in entries:
            if entry["support"]["status"] == "candidate":
                entry["support"]["status"] = "indeterminate"
                entry["support"]["reasons"].append(
                    _reason("incomplete_inventory", "full local dataset inventory is unavailable")
                )
    else:
        _probe_index(image, entries)

    return {
        "schema_version": 1,
        "outcome": "complete" if complete else "partial",
        "source": {"path": str(source), "size_bytes": size},
        "dataset_count": len(entries),
        "datasets": entries,
        "visited_groups": group_count,
        "visited_links": link_count,
        "skipped": skipped,
        "issues": issues,
        "note": (
            "candidate means metadata permits an attempted recovery, not that its chunks "
            "are readable, correctly attributed, or historically intact"
        ),
    }
