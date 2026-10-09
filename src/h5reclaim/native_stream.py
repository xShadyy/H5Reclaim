"""Bounded native exports of fixed, empty, null and heap-backed records."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
import shutil
import tempfile
import time
from itertools import product
from math import prod
from pathlib import Path

import h5py
import numpy as np

from .filter_registry import supported_filters
from .format import FormatError
from .large_streaming import (LargeBudget, _deadline,
                              _hash_range, _stream_blocks, sparse_snapshot)
from .logical_types import (contains_pointers, decode_value, encode_value, type_label,
                            reference_addresses, token_bytes, validate_type, write_value)
from .metadata import UnsupportedCase
from .native_io import read_fixed_block, write_fixed_block
from .native_addresses import chunk_address
from .ownership_inventory import inventory_other_allocations
from .readable_export import _create_matching_dataset, _require_no_competing_owner, _selected_dataset
from .recovery import VERSION, RecoveryError, _validate_paths, _verify_source, sha256_file


def inspect_storage(selected, size, budget, deadline, *, require_codecs=True):
    """Validate physical placement independently of successful native reads."""
    shape = selected.shape
    typ, creation = selected.id.get_type(), selected.id.get_create_plist()
    if creation.get_external_count() or selected.is_virtual:
        raise UnsupportedCase("external and virtual storage require a supplied related-file manifest")
    if creation.get_nfilters() > 32:
        raise UnsupportedCase("filter pipeline exceeds the streaming bound")
    filters = [int(creation.get_filter(i)[0]) for i in range(creation.get_nfilters())]
    available = supported_filters()
    for identifier in filters if require_codecs else ():
        if (identifier not in available or not h5py.h5z.filter_avail(identifier)
                or not h5py.h5z.get_filter_info(identifier) & h5py.h5z.FILTER_CONFIG_DECODE_ENABLED):
            raise UnsupportedCase(f"filter {identifier} is unavailable; install h5reclaim[filters] for packaged codecs")
    records, ranges = [], []
    if shape is None or prod(shape) == 0:
        return records, ranges, filters
    layout = creation.get_layout()
    if layout == h5py.h5d.CHUNKED:
        chunks = selected.chunks
        if prod(chunks) * typ.get_size() > budget.max_chunk_bytes:
            raise UnsupportedCase("decoded chunk exceeds the streaming record budget")
        grid = tuple((length + width - 1) // width for length, width in zip(shape, chunks))
        if prod(grid) > budget.max_grid:
            raise UnsupportedCase("chunk grid exceeds the configured map budget")
        count = selected.id.get_num_chunks()
        if count > budget.max_chunks:
            raise UnsupportedCase("allocated chunk count exceeds the configured budget")
        seen = set()
        for i in range(count):
            _deadline(deadline)
            info = selected.id.get_chunk_info(i)
            origin = tuple(int(n) for n in info.chunk_offset)
            address, length, mask = chunk_address(selected, info.byte_offset), int(info.size), int(info.filter_mask)
            if (len(origin) != len(shape) or origin in seen
                    or any(n < 0 or n >= axis or n % width for n, axis, width in zip(origin, shape, chunks))
                    or not 0 <= address < size or not 0 < length <= min(size - address, 2 * budget.max_chunk_bytes)
                    or mask >> len(filters)):
                raise FormatError("chunk index has a contradictory coordinate, physical range or filter mask")
            check = selected.id.get_chunk_info_by_coord(origin)
            if (chunk_address(selected, check.byte_offset) != address or check.size != length or check.filter_mask != mask
                    or tuple(check.chunk_offset) != origin):
                raise FormatError("chunk enumeration and coordinate lookup disagree")
            for position in range(len(filters)):
                if mask & (1 << position) and not creation.get_filter(position)[1] & h5py.h5z.FLAG_OPTIONAL:
                    raise FormatError("mandatory filter is marked skipped")
            seen.add(origin)
            records.append((origin, address, length, mask))
            ranges.append((address, address + length, origin))
        if selected.id.get_num_chunks() != count:
            raise FormatError("chunk allocation count changed")
        records.sort()
    elif layout == h5py.h5d.CONTIGUOUS:
        length, address = int(selected.id.get_storage_size()), selected.id.get_offset()
        if length:
            if address is None or address < 0 or length > size - address:
                raise FormatError("contiguous allocation lies outside the physical source")
            if not contains_pointers(typ) and length != prod(shape) * typ.get_size():
                raise FormatError("contiguous allocation length disagrees with the fixed datatype")
            ranges.append((int(address), int(address) + length, ()))
    elif layout != h5py.h5d.COMPACT:
        raise UnsupportedCase("unknown local dataset layout")
    ordered = sorted(ranges)
    if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
        raise FormatError("selected allocations overlap")
    return records, ranges, filters


def logical_records(dataset, selection, budget, statistics=None):
    """Read bounded record batches, isolating failed batches to single records."""
    statistics = statistics if statistics is not None else {}
    extent = tuple(part.stop - part.start for part in selection)
    record_bytes = max(dataset.id.get_type().get_size(),
                       (budget.block_bytes + budget.logical_batch_records - 1) // budget.logical_batch_records)
    pieces = _stream_blocks(extent, record_bytes, budget.block_bytes) if extent else [()]
    for local in pieces:
        piece = tuple(slice(parent.start + part.start, parent.start + part.stop)
                      for parent, part in zip(selection, local))
        indices = product(*(range(part.start, part.stop) for part in piece)) if piece else [()]
        try:
            values = dataset[piece]
            statistics['batches'] = statistics.get('batches', 0) + 1
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            statistics['failed_batches'] = statistics.get('failed_batches', 0) + 1
            for index in indices:
                statistics['scalar_fallback_reads'] = statistics.get('scalar_fallback_reads', 0) + 1
                try:
                    value = dataset[index]
                except (OSError, RuntimeError, ValueError, TypeError) as error:
                    yield index, None, error
                else:
                    yield index, value, None
        else:
            for index in indices:
                relative = tuple(value - part.start for value, part in zip(index, piece))
                yield index, values[relative] if piece else values, None


def export_native_stream(source, dataset_path, output, report_path, *, budget=None,
                         published_output=None, hints=None, object_address=None,
                         detached_inventory=None, inspection_image=None, resume_dir=None,
                         source_dataset_path=None, opened_file=None, address_space_size=None, range_hash=None,
                         checkpoint_source_digest=None, bundle_context=None, verify_related=None):
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    budget = budget or LargeBudget()
    deadline = time.monotonic() + budget.max_seconds
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, copied), ExitStack() as checkpoint_stack:
        from contextlib import nullcontext
        from .checked_view import checked_root_view
        with (nullcontext((inspection_image or image, None)) if opened_file is not None or inspection_image else checked_root_view(image, budget)) as (native_view, root_correction), (nullcontext(opened_file) if opened_file is not None else h5py.File(native_view, "r")) as opened:
            if object_address is None:
                selected = _selected_dataset(opened, source_dataset_path or dataset_path)
            else:
                from .object_discovery import open_by_address
                selected = open_by_address(opened, object_address)
            typ, shape = selected.id.get_type(), selected.shape
            validate_type(typ, budget=budget)
            elements = 0 if shape is None else prod(shape)
            if shape is not None and (len(shape) > 32 or any(n < 0 for n in shape)):
                raise UnsupportedCase("dataset rank or extent is invalid")
            if elements * typ.get_size() > budget.max_logical_bytes:
                raise UnsupportedCase("logical records exceed the configured byte budget")
            pointer_type = contains_pointers(typ)
            if pointer_type and elements > budget.max_grid:
                raise UnsupportedCase("heap-backed element map exceeds the configured map budget")
            comparisons = []
            if hints is not None:
                from .hints import compare_hints, require_no_conflicts
                comparisons = compare_hints(hints, observed_fields={"path": dataset_path,
                    "shape": shape, "chunks": selected.chunks, "dtype": type_label(typ),
                    "filters": tuple(selected.id.get_create_plist().get_filter(i)[0]
                                     for i in range(selected.id.get_create_plist().get_nfilters()))},
                    input_sha256=digest)
                require_no_conflicts(comparisons)
            records, ranges, filters = inspect_storage(selected, address_space_size or size, budget, deadline)
            address = int(h5py.h5o.get_info(selected.id).addr)
            if detached_inventory is None:
                inventory = inventory_other_allocations(image, address,
                                  max_objects=budget.max_objects, max_links=budget.max_links,
                                  opened_file=opened if opened_file is not None or root_correction or inspection_image else None,
                                  address_space_size=address_space_size or size,
                                  max_allocations=budget.max_chunks, max_seconds=budget.max_seconds,
                                  max_path_bytes=budget.max_metadata_bytes)
                owners = _require_no_competing_owner(inventory, ranges)
            else:
                owners = detached_inventory
            layout = selected.id.get_create_plist().get_layout()
            chunks = selected.chunks
            allocated = bool(elements and (records if chunks else selected.id.get_storage_size()))
            if not elements:
                selections = []
            elif chunks:
                selections = [tuple(slice(n, min(n + width, axis)) for n, width, axis in zip(origin, chunks, shape))
                              for origin, _, _, _ in records]
            elif allocated:
                selections = _stream_blocks(shape, typ.get_size(), budget.block_bytes)
            else:
                selections = []
            checkpoint = None
            reused_selections = 0
            if resume_dir is not None:
                from .unit_checkpoint import UnitCheckpoint
                checkpoint = checkpoint_stack.enter_context(UnitCheckpoint(resume_dir, checkpoint_source_digest or digest, source_dataset_path or dataset_path,
                                            typ, shape, chunks, budget.block_bytes))
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-native-", dir=output.parent) as directory:
                staged = Path(directory) / "output.h5"
                accepted = failed = deferred_count = value_bytes = 0
                failures, copied_attrs, omitted_attrs = [], [], []
                logical_statistics = {'batch_record_limit': budget.logical_batch_records}
                source_hash = hashlib.sha256()
                from .userblock import userblock_size, copy_userblock
                application_header_size = userblock_size(image)
                with h5py.File(staged, "x", userblock_size=application_header_size) as result, image.open("rb") as raw, ExitStack() as work_stack:
                    from .output_annotations import metadata_group_for_path
                    from .filter_registry import preserve_output_filters
                    preserve_filters = preserve_output_filters(filters)
                    target = _create_matching_dataset(result, selected, dataset_path, preserve_filters=preserve_filters)
                    meta = result.require_group(metadata_group_for_path(dataset_path))
                    meta.attrs["source_sha256"] = digest
                    status_path = None
                    if elements:
                        if pointer_type:
                            status_shape, status_name = shape, "element_status"
                        else:
                            status_shape = tuple((n + w - 1) // w for n, w in zip(shape, chunks)) if chunks else (1,)
                            status_name = "validity"
                        status = meta.create_dataset(status_name, shape=status_shape, dtype="u1", fillvalue=2,
                                      chunks=True if status_shape else None)
                        status_path = status.name
                    pending = meta.create_dataset("deferred_references", shape=(0,), maxshape=(None,),
                                        chunks=(128,), dtype=h5py.string_dtype("utf-8"))
                    source_records, source_allocations = [], None
                    if len(records) > 512:
                        ledger_type = np.dtype([('origin', 'u8', (len(shape),)), ('address', 'u8'),
                                                ('stored_bytes', 'u8'), ('filter_mask', 'u4'), ('raw_sha256', 'S64')])
                        source_allocations = meta.create_dataset('source_allocations', shape=(len(records),),
                                                                 chunks=(min(1024, len(records)),), dtype=ledger_type)
                    for number, (origin, offset, length, mask) in enumerate(records):
                        record = {"origin": list(origin), "address": offset,
                            "stored_bytes": length, "filter_mask": mask,
                            "raw_sha256": (range_hash(offset, length) if range_hash else
                                           _hash_range(raw, offset, length, deadline=deadline, block_bytes=budget.block_bytes))}
                        if source_allocations is not None:
                            source_allocations[number] = (origin, offset, length, mask, record['raw_sha256'].encode('ascii'))
                        else:
                            source_records.append(record)
                    for selection in selections:
                        _deadline(deadline)
                        cached = checkpoint.load(selection) if checkpoint else None
                        cached_frames = None
                        from .unit_checkpoint import read_frames, write_frame
                        selection_stack = work_stack.enter_context(ExitStack())
                        checkpoint_stream = None
                        if cached is not None:
                            reused_selections += 1
                            cached_frames = iter(read_frames(selection_stack.enter_context(cached.open("rb")),
                                                budget.max_metadata_bytes, budget.block_bytes))
                        elif checkpoint:
                            checkpoint_stream = selection_stack.enter_context(checkpoint.temporary(selection).open("wb"))
                        if pointer_type:
                            indices = product(*(range(part.start, part.stop) for part in selection)) if shape else [()]
                            logical = ((index, None, None) for index in indices) if cached_frames is not None else logical_records(selected, selection, budget, logical_statistics)
                            for index, source_value, read_error in logical:
                                _deadline(deadline)
                                try:
                                    if cached_frames is not None:
                                        frame, payload = next(cached_frames)
                                        if frame["index"] != list(index) or payload:
                                            raise RecoveryError("cached logical selection differs from its source placement")
                                        token = frame["token"]
                                    else:
                                        if read_error is not None:
                                            raise read_error
                                        token = encode_value(source_value, typ, opened, max_bytes=budget.max_chunk_bytes,
                                                             max_depth=budget.max_type_depth)
                                except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                                    status[index] = 6
                                    failed += 1
                                    if len(failures) < 128:
                                        failures.append({"index": list(index), "reason": str(exc)[:300]})
                                    continue
                                if checkpoint_stream is not None:
                                    write_frame(checkpoint_stream, {"index": list(index), "token": token})
                                length = len(token_bytes(token))
                                if value_bytes + length > budget.max_logical_bytes:
                                    raise UnsupportedCase("decoded heap values exceed the configured logical byte budget")
                                value_bytes += length
                                referents = reference_addresses(token)
                                if referents:
                                    pending.resize((len(pending) + 1,))
                                    pending[-1] = json.dumps({"index": list(index), "value": token})
                                if referents - {address}:
                                    deferred_count += 1
                                    status[index] = 4
                                    continue
                                value = decode_value(token, typ, result, {address: dataset_path})
                                write_value(target, index, value)
                                reverse = {int(h5py.h5o.get_info(target.id).addr): address}
                                checked = encode_value(target[index], typ, result, address_map=reverse,
                                                       max_bytes=budget.max_chunk_bytes, max_depth=budget.max_type_depth)
                                if token_bytes(checked) != token_bytes(token):
                                    raise RecoveryError("logical record readback differs from source")
                                status[index] = 1
                                source_hash.update(token_bytes(token))
                                accepted += 1
                        else:
                            unit = tuple(part.start // width for part, width in zip(selection, chunks)) if chunks else (0,)
                            extent = tuple(part.stop - part.start for part in selection)
                            local_blocks = _stream_blocks(extent, typ.get_size(), budget.block_bytes) if extent else [()]
                            unit_hash, unit_elements = source_hash.copy(), 0
                            read_failure = None
                            for local in local_blocks:
                                _deadline(deadline)
                                piece = tuple(slice(part.start + sub.start, part.start + sub.stop)
                                              for part, sub in zip(selection, local))
                                try:
                                    if cached_frames is not None:
                                        frame, payload = next(cached_frames)
                                        if frame["selection"] != [[part.start, part.stop] for part in piece]:
                                            raise RecoveryError("cached selection differs from its source placement")
                                        piece_shape = tuple(part.stop - part.start for part in piece)
                                        if len(payload) != prod(piece_shape) * typ.get_size():
                                            raise RecoveryError("cached fixed records have the wrong size")
                                        block = np.frombuffer(payload, dtype=f"V{typ.get_size()}").reshape(piece_shape)
                                    else:
                                        block = read_fixed_block(selected, piece)
                                except (OSError, RuntimeError, ValueError) as exc:
                                    read_failure = exc
                                    break
                                write_fixed_block(target, piece, block)
                                checked = read_fixed_block(target, piece)
                                if block.tobytes() != checked.tobytes():
                                    raise RecoveryError("fixed record readback differs from source")
                                if checkpoint_stream is not None:
                                    write_frame(checkpoint_stream, {"selection": [[part.start, part.stop] for part in piece],
                                                "payload_bytes": block.nbytes}, block.tobytes())
                                unit_hash.update(block.tobytes())
                                unit_elements += block.size
                            if read_failure is not None:
                                if chunks is None:
                                    raise RecoveryError("contiguous native read failed") from read_failure
                                status[unit] = 6
                                failed += prod(part.stop - part.start for part in selection)
                                if len(failures) < 128:
                                    failures.append({"origin": [part.start for part in selection], "reason": str(read_failure)[:300]})
                                selection_stack.close()
                                continue
                            source_hash = unit_hash
                            status[unit] = 1
                            accepted += unit_elements
                        if cached_frames is not None and next(cached_frames, None) is not None:
                            raise RecoveryError("cached selection contains extra records")
                        selection_stack.close()
                        if checkpoint and cached is None:
                            # A logical selection with a failed record is retried as a whole.
                            if not pointer_type or all(status[index] in (1, 4) for index in
                                    (product(*(range(part.start, part.stop) for part in selection)) if shape else [()])):
                                checkpoint.commit(selection)
                        result.flush()
                        if (staged.stat().st_size > budget.max_output_bytes
                                or shutil.disk_usage(staged.parent).free < budget.disk_reserve_bytes):
                            raise UnsupportedCase("native output exceeds its byte or free-disk budget")
                    from .whole_file import _attributes, _restore_attributes
                    attrs, omitted_attrs = _attributes(selected, omit={"DIMENSION_LIST", "REFERENCE_LIST"}, budget=budget)
                    omitted_attrs += _restore_attributes(target, attrs)
                    copied_attrs = [record["name"] for record in attrs if record["name"] not in omitted_attrs]
                    report = {"schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                        "mode": "native_stream_export", "operation": "native_stream_export",
                        "metadata_group": meta.name,
                        "checked_root_correction": root_correction,
                        "output_filter_policy": "preserved reversible pipeline" if preserve_filters else "decoded values stored without source filters",
                        "outcome": "complete" if accepted == elements else "partial",
                        "source": {"path": str(source), "size_bytes": size, "sha256_before": digest,
                                   "sha256_after": digest, "snapshot_physical_data_bytes_copied": copied},
                        "dataset": {"path": dataset_path, "shape": list(shape) if shape is not None else None,
                                    "maxshape": list(selected.maxshape) if selected.maxshape is not None else None,
                                    "dtype": type_label(typ), "chunks": list(chunks) if chunks else None,
                                    "file_type_encoding_hex": typ.encode().hex(), "filters_in_order": filters,
                                    "source_object_header_address": address,
                                    "attributes_copied": copied_attrs, "attributes_omitted": omitted_attrs},
                        "accepted_elements": accepted, "unknown_elements": elements - accepted,
                        "selection_checkpoint": {"enabled": checkpoint is not None, "selections_reused": reused_selections,
                                                 "granularity": "allocated chunks or bounded contiguous blocks"},
                        "failed_elements": failed, "deferred_reference_elements": deferred_count,
                        "logical_reads": logical_statistics if pointer_type else None,
                        "failed_reads": failures, "decoded_value_bytes": value_bytes,
                        "validity_map": status_path, "element_status": status_path if pointer_type else None,
                        "deferred_references": pending.name if len(pending) else None,
                        "source_chunk_records": source_records, "ownership_inventory": owners,
                        "source_allocations": source_allocations.name if source_allocations is not None else None,
                        "source_allocation_count": len(records),
                        "validity_codes": {"1": "decoded and read back equal", "2": "unallocated or unknown",
                                           "4": "reference needs a recovered target", "6": "native read failed"},
                        "native_value_sha256": source_hash.hexdigest(), "output_path": str(published_output or output),
                        "value_evidence": "Current native allocation and logical or fixed-record readback; historical bytes are unknown."}
                    if bundle_context is not None:
                        report['bundle'] = bundle_context
                        report['mode'] = bundle_context['driver'] + '_native_stream_export'
                        report['source']['path'] = bundle_context['members'][0]['path']
                    if comparisons:
                        from dataclasses import asdict
                        report["operator_hints"] = {"trust_level": hints.trust_level,
                                                    "comparisons": [asdict(item) for item in comparisons]}
                    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
                    meta.create_dataset("report_json", data=text, dtype=h5py.string_dtype("utf-8"))
                    result.flush()
                copy_userblock(image, staged, application_header_size, budget.block_bytes)
                if staged.stat().st_size > budget.max_output_bytes:
                    raise UnsupportedCase("native output exceeds the configured byte budget")
                if sha256_file(image) != digest:
                    raise RecoveryError("private image changed during native export")
                _verify_source(source, identity, digest)
                if verify_related is not None:
                    verify_related()
                _validate_paths(source, output, report_path)
                with tempfile.TemporaryDirectory(prefix=".h5reclaim-native-", dir=report_path.parent) as repdir:
                    staged_report = Path(repdir) / "report.json"
                    staged_report.write_text(text, encoding="utf-8")
                    os.link(staged_report, report_path)
                    try:
                        os.link(staged, output)
                    except Exception:
                        report_path.unlink(missing_ok=True)
                        raise
                return report
