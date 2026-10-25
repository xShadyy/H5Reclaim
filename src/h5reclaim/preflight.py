"""Read-only metadata workbench using H5Reclaim's independent raw parsers.

No HDF5 file is opened through libhdf5, no plugin is loaded, and no measurement
is decompressed. Acceptance concerns the explicitly covered metadata only.
"""

from collections import deque
from math import prod
from pathlib import Path
import time

from .format import H5File, FormatError, UnsupportedFormat, SIGNATURE
from .large_streaming import LargeBudget, sparse_snapshot, _deadline
from .modern_indexes import ModernH5File
from .file_space_repair import inspect_file_space
from .metadata_fallback import (_messages, _old_messages, _old_root_address,
                                _old_group_links, _compact_links, _unique, _filters, _old_filters)


SIGNATURES = {name: name.encode("ascii") for name in (
    "OHDR", "OCHK", "FRHP", "FHDB", "FHIB", "BTHD", "BTIN", "BTLF",
    "EAHD", "EAIB", "EASB", "EADB", "FAHD", "FADB", "TREE", "SNOD", "HEAP", "GCOL", "FSHD", "FSSE")}
MESSAGE_NAMES = {1: "dataspace", 2: "link_info", 3: "datatype", 5: "fill_value",
                 6: "link", 7: "external_storage", 8: "layout", 10: "group_info",
                 11: "filter_pipeline", 12: "attribute", 16: "continuation", 17: "symbol_table"}


def _version(path):
    size, at = path.stat().st_size, 0
    with path.open("rb") as stream:
        while at + 9 <= size:
            stream.seek(at)
            prefix = stream.read(9)
            if prefix[:8] == SIGNATURE:
                return prefix[8]
            at = 512 if at == 0 else at * 2
    raise FormatError("HDF5 signature is absent at all documented offsets")


def _space(raw, width):
    if len(raw) < 4 or raw[0] not in (1, 2):
        raise UnsupportedFormat("unsupported dataspace encoding")
    version, rank, flags = raw[:3]
    kind = raw[3] if version == 2 else 0 if rank == 0 else 1
    prefix = 4 if version == 2 else 8
    if flags & ~1 or rank > 32 or len(raw) < prefix:
        raise FormatError("invalid dataspace flags, rank or prefix")
    if kind == 2 and rank == 0:
        return None, None
    if rank == 0:
        return (), ()
    required = prefix + rank * width * (2 if flags & 1 else 1)
    if len(raw) < required or any(raw[required:]):
        raise FormatError("dataspace dimensions exceed their message")
    values = tuple(int.from_bytes(raw[p:p + width], "little") for p in range(prefix, required, width))
    shape = values[:rank]
    maximum = values[rank:] if flags & 1 else shape
    maximum = tuple(None if n == (1 << (width * 8)) - 1 else n for n in maximum)
    if any(n is not None and n < current for n, current in zip(maximum, shape)):
        raise FormatError("maximum dimensions contradict current extent")
    return shape, maximum


def inspect_metadata_local(path, *, budget=None, profile="forensic"):
    """Check reachable headers, local links and supported chunk indexes."""
    path, budget = Path(path), budget or LargeBudget()
    if profile not in ("forensic", "strict"):
        raise ValueError("profile must be forensic or strict")
    deadline = time.monotonic() + budget.max_seconds
    objects, findings, repairs, payloads = [], [], [], []
    external = 0
    complete = True
    def finding(code, message, *, object_path="/", address=None, severity="error"):
        if len(findings) >= min(budget.max_links, 4096):
            raise UnsupportedFormat("preflight finding budget exceeded")
        findings.append({"code": code, "message": str(message)[:700], "path": object_path,
                         "address": address, "severity": severity})
    version = _version(path)
    reader_type = H5File if version < 2 else ModernH5File
    options = {} if version < 2 else dict(large_sparse_scan=True, max_chunks=budget.max_chunks,
        max_chunk_bytes=min(128 << 20, 2 * budget.max_chunk_bytes),
        repair_scan_bytes=budget.max_scan_bytes, repair_scan_seconds=min(86400, budget.max_seconds))
    with reader_type(path, **options) as reader:
        root, cached = _old_root_address(reader) if version < 2 else (reader.superblock.root_object_address, None)
        queue, seen, links_seen = deque([("/", root, cached, 0)]), set(), 0
        while queue:
            _deadline(deadline)
            selected_path, address, cached, depth = queue.popleft()
            if address in seen:
                continue
            if len(seen) >= budget.max_objects or depth > 64:
                complete = False
                finding("METADATA_BUDGET", "reachable object or depth budget exceeded", severity="unsupported")
                break
            seen.add(address)
            descriptor = {"path": selected_path, "address": address}
            try:
                messages = _old_messages(reader, address) if version < 2 else _messages(reader, address)
                descriptor["messages"] = [{"kind": m.kind, "name": MESSAGE_NAMES.get(m.kind, "other"),
                    "offset": m.absolute_offset, "bytes": len(m.data), "flags": m.flags} for m in messages]
                space, datatype, layout = (_unique(messages, kind) for kind in (1, 3, 8))
                if space is None or layout is None:
                    descriptor["kind"] = "group" if datatype is None else "named_datatype"
                    if datatype is None:
                        links = _old_group_links(reader, address, cached) if version < 2 else _compact_links(reader, address)
                        links_seen += len(links)
                        if links_seen > budget.max_links:
                            raise UnsupportedFormat("reachable link budget exceeded")
                        for name, link in links.items():
                            child = selected_path.rstrip("/") + "/" + name
                            if link is None:
                                external += 1
                                finding("NONLOCAL_LINK", "soft or external link is declared and was not followed",
                                    object_path=child, severity="warning")
                            else:
                                queue.append((child, link.object_address, link.cached_group, depth + 1))
                else:
                    descriptor["kind"] = "dataset"
                    if datatype is None or len(datatype.data) < 8:
                        raise FormatError("dataset datatype is missing or truncated")
                    shape, maximum = _space(space.data, reader.superblock.length_size)
                    width = int.from_bytes(datatype.data[4:8], "little")
                    if not 0 < width <= 1 << 20:
                        raise UnsupportedFormat("datatype record width exceeds bounded inspection")
                    descriptor.update(shape=list(shape) if shape is not None else None,
                        maxshape=list(maximum) if maximum is not None else None,
                        element_size=width, datatype_class=datatype.data[0] & 15,
                        logical_bytes=(prod(shape) * width if shape is not None else 0))
                    if descriptor["logical_bytes"] > budget.max_logical_bytes:
                        raise UnsupportedFormat("dataset exceeds the configured logical byte budget")
                    raw_layout = layout.data
                    if len(raw_layout) < 2:
                        raise FormatError("dataset layout is truncated")
                    layout_class = raw_layout[1] if raw_layout[0] >= 3 else raw_layout[2] if len(raw_layout) > 2 else -1
                    descriptor["layout_class"] = layout_class
                    pipeline_message = _unique(messages, 11)
                    pipeline = (_old_filters if version < 2 else _filters)(
                        pipeline_message.data if pipeline_message else None, width)
                    descriptor["filters"] = [p.id for p in pipeline]
                    if _unique(messages, 7) is not None or layout_class == 3:
                        external += 1
                        finding("DEPENDENT_STORAGE", "external or virtual storage is declared; dependencies were not opened",
                            object_path=selected_path, severity="warning")
                    if version >= 2 and layout_class == 2 and shape and all(shape):
                        if raw_layout[0] not in (4, 5):
                            raise UnsupportedFormat("legacy chunk-layout index is outside modern preflight")
                        ndim, extent_width = raw_layout[3:5]
                        if ndim != len(shape) + 1 or extent_width not in (1, 2, 4, 8):
                            raise FormatError("chunk dimension encoding contradicts dataspace")
                        end = 5 + ndim * extent_width
                        if len(raw_layout) <= end:
                            raise FormatError("chunk layout dimensions are truncated")
                        dimensions = tuple(int.from_bytes(raw_layout[p:p + extent_width], "little")
                            for p in range(5, end, extent_width))
                        if dimensions[-1] != width or any(n <= 0 for n in dimensions):
                            raise FormatError("chunk dimensions contradict datatype width")
                        index = reader.read_index(address, shape, dimensions[:-1], width, maxshape=maximum,
                            filters=tuple(p.id for p in pipeline), max_chunks=budget.max_chunks)
                        descriptor.update(chunks=list(dimensions[:-1]), index_type=index.index_type,
                                          allocated_chunks=len(index.chunks))
                        selected_repairs = list(index.reconstructed_links)
                        if index.reconstructed_data_block_pointer:
                            selected_repairs.append({"kind": "fahd_to_fadb", "source_address": index.base_address,
                                "target_address": index.data_block_address, "pointer_offset": index.data_block_pointer_offset})
                        for repair in selected_repairs:
                            repairs.append({**repair, "dataset": selected_path})
                            finding("INDEX_LINK_REPAIRABLE", "index link has a unique original-checksum-restoring repair",
                                object_path=selected_path, address=address)
                        for chunk in index.chunks:
                            start = reader.absolute(chunk.address)
                            if chunk.size > path.stat().st_size - start:
                                raise FormatError("chunk payload allocation crosses physical EOF")
                            payloads.append((start, start + chunk.size, selected_path))
                            if len(payloads) > budget.max_chunks:
                                raise UnsupportedFormat("aggregate allocation inventory exceeds preflight budget")
                    elif version < 2 and layout_class == 2:
                        if not shape or not all(shape):
                            raise UnsupportedFormat("legacy chunk index needs positive simple dimensions")
                        old_layout = reader.read_dataset_layout(address, rank=len(shape))
                        if old_layout.element_size != width:
                            raise FormatError("legacy chunk element width contradicts datatype")
                        tree = reader.walk_tree(old_layout.root_address, rank=len(shape), element_size=width,
                                                max_nodes=min(budget.max_objects, 100000))
                        if tree.broken_links:
                            finding("BROKEN_LEGACY_INDEX_LINK", "legacy chunk tree has unresolved child links",
                                    object_path=selected_path)
                        descriptor.update(chunks=list(old_layout.chunk_shape), index_type="v1_raw_data_btree")
                        for node in tree.nodes:
                            if node.level:
                                continue
                            for entry in node.entries:
                                coordinate = entry.key.offsets[:-1]
                                if any(start >= size or start % chunk for start, size, chunk
                                       in zip(coordinate, shape, old_layout.chunk_shape)):
                                    raise FormatError("legacy chunk coordinate contradicts selected shape")
                                start = reader.absolute(entry.address)
                                payloads.append((start, start + entry.key.stored_size, selected_path))
                                if len(payloads) > budget.max_chunks:
                                    raise UnsupportedFormat("aggregate legacy allocation budget exceeded")
                    elif layout_class not in (0, 1, 2, 3):
                        raise UnsupportedFormat("unknown storage layout class")
                    elif raw_layout[0] in (3, 4) and layout_class == 1:
                        offset_size, length_size = reader.superblock.offset_size, reader.superblock.length_size
                        if len(raw_layout) < 2 + offset_size + length_size:
                            raise FormatError("contiguous layout is truncated")
                        allocation = int.from_bytes(raw_layout[2:2 + offset_size], "little")
                        length = int.from_bytes(raw_layout[2 + offset_size:2 + offset_size + length_size], "little")
                        if length != descriptor["logical_bytes"]:
                            raise FormatError("contiguous allocation length contradicts dataspace and datatype")
                        if allocation != reader.superblock.undefined_address and length:
                            start = reader.absolute(allocation)
                            if start + length > min(reader.size, reader.superblock.eof_address):
                                raise FormatError("contiguous allocation crosses the file boundary")
                            payloads.append((start, start + length, selected_path))
                    elif raw_layout[0] in (3, 4) and layout_class == 0:
                        if (len(raw_layout) < 4 or int.from_bytes(raw_layout[2:4], "little") != descriptor["logical_bytes"]
                                or len(raw_layout) < 4 + descriptor["logical_bytes"]):
                            raise FormatError("compact layout length contradicts schema")
            except UnsupportedFormat as exc:
                complete = False
                finding("UNSUPPORTED_METADATA", exc, object_path=selected_path, address=address, severity="unsupported")
            except (FormatError, OSError, ValueError, IndexError, OverflowError) as exc:
                complete = False
                finding("CORRUPT_METADATA", exc, object_path=selected_path, address=address)
            objects.append(descriptor)
        ordered = sorted(payloads)
        for left, right in zip(ordered, ordered[1:]):
            if left[1] > right[0]:
                finding("OVERLAPPING_PAYLOADS", f"allocations of {left[2]} and {right[2]} overlap")
        for start, end, selected_path in payloads:
            if any(start < stop and begin < end for begin, stop, _ in reader.metadata_ranges):
                finding("PAYLOAD_METADATA_OVERLAP", "chunk allocation overlaps checked metadata", object_path=selected_path)
        ranges = sorted(set(reader.metadata_ranges))
    file_space = {"version": "legacy_not_covered", "managers": [], "metadata_ranges": []}
    if version >= 2:
        try:
            file_space = inspect_file_space(path, budget=budget)
            for start, end, kind in file_space["metadata_ranges"]:
                if any(start < stop and begin < end for begin, stop, _ in payloads):
                    finding("PAYLOAD_FILE_SPACE_OVERLAP",
                            "file-space metadata allocation overlaps a dataset payload")
            ranges = sorted(set((*ranges, *(tuple(item) for item in file_space["metadata_ranges"]))))
        except UnsupportedFormat as exc:
            complete = False
            finding("UNSUPPORTED_FILE_SPACE", exc, severity="unsupported")
        except (FormatError, OSError, ValueError, IndexError) as exc:
            complete = False
            finding("CORRUPT_FILE_SPACE", exc)
    errors = any(f["severity"] == "error" for f in findings)
    decision = ("reject_corrupt" if errors else "reject_dependencies" if profile == "strict" and external else
                "incomplete" if not complete else "accept_with_warnings" if findings else "accept")
    return {"schema_version": 1, "tool": "h5reclaim", "operation": "metadata_preflight", "profile": profile,
        "decision": decision, "analysis": {"complete": complete, "objects_checked": len(seen), "links_seen": links_seen},
        "objects": objects, "findings": findings, "index_repairs": repairs,
        "metadata_ranges": [list(r) for r in ranges], "declared_dependencies": external,
        "file_space": {"version": file_space["version"], "manager_count": len(file_space["managers"]),
                       "metadata_ranges": file_space["metadata_ranges"]},
        "boundary": {"native_file_opens": False, "payload_reads": False, "decompression": False,
                     "dynamic_plugins": False, "external_file_opens": False, "writes": False},
        "scope": "reachable headers, namespace, supported modern chunk indexes and rooted persistent file-space managers; other semantic constraints and raw measurement integrity are outside this check"}


def preflight(source, *, budget=None, profile="forensic"):
    budget = budget or LargeBudget()
    with sparse_snapshot(Path(source), budget=budget) as (image, digest, identity, size, copied):
        try:
            result = inspect_metadata_local(image, budget=budget, profile=profile)
        except (FormatError, OSError, ValueError) as exc:
            result = {"schema_version": 1, "tool": "h5reclaim", "operation": "metadata_preflight",
                "decision": "reject_corrupt", "analysis": {"complete": False},
                "findings": [{"code": "SUPERBLOCK_UNREADABLE", "message": str(exc)[:700], "severity": "error"}],
                "objects": [], "index_repairs": []}
        result["source"] = {"path": str(source), "sha256": digest, "size_bytes": size}
        from .recovery import _verify_source
        _verify_source(Path(source), identity, digest)
        return result


def scan_markers(source, *, budget=None):
    """Find bounded signature candidates, including crossings of read blocks."""
    import os
    from .sparse_io import sparse_extents
    budget = budget or LargeBudget()
    deadline = time.monotonic() + budget.max_seconds
    with sparse_snapshot(Path(source), budget=budget) as (image, digest, identity, size, copied):
        candidates, seen, consumed = [], set(), 0
        with image.open("rb", buffering=0) as stream:
            try:
                from itertools import islice
                extents = list(islice(sparse_extents(stream.fileno(), size), budget.max_objects + 1))
                if len(extents) > budget.max_objects:
                    raise UnsupportedFormat("marker extent count exceeds its metadata budget")
            except OSError:
                extents = [(0, size)]
            for start, end in extents:
                tail, position = b"", start
                while position < end:
                    _deadline(deadline)
                    stream.seek(position)
                    block = stream.read(min(budget.block_bytes, end - position))
                    if not block:
                        raise FormatError("source ended during marker scan")
                    consumed += len(block)
                    if consumed > budget.max_scan_bytes:
                        raise UnsupportedFormat("marker scan exceeds the physical byte budget")
                    window, base = tail + block, position - len(tail)
                    for name, signature in SIGNATURES.items():
                        cursor = window.find(signature)
                        while cursor >= 0:
                            absolute = base + cursor
                            if absolute + len(signature) > position and (name, absolute) not in seen:
                                seen.add((name, absolute))
                                if len(seen) > budget.max_objects:
                                    raise UnsupportedFormat("marker candidate budget exceeded")
                                candidates.append({"signature": name, "offset": absolute, "ownership": "unassigned"})
                            cursor = window.find(signature, cursor + 1)
                    tail = window[-3:]
                    position += len(block)
        from .recovery import _verify_source
        _verify_source(Path(source), identity, digest)
        return {"schema_version": 1, "tool": "h5reclaim", "operation": "marker_scan",
            "source": {"path": str(source), "sha256": digest, "size_bytes": size},
            "physical_bytes_scanned": consumed, "candidates": sorted(candidates, key=lambda r: (r["offset"], r["signature"])),
            "scope": "signature candidates only; a marker does not establish ownership, valid metadata or recoverable measurements"}
