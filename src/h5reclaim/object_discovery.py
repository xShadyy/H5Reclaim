"""Discover checksummed modern dataset headers when group links are unreadable.

Names are never inferred from a scan. A candidate's own object header supplies
its schema and chunk index; physical placement is independently validated.
Detached candidates are explicitly distinct from a complete rooted inventory.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import tempfile
import time
import sys
import subprocess
from contextlib import contextmanager
from pathlib import Path

import h5py

from .format import FormatError, UnsupportedFormat, H5File
from .large_streaming import LargeBudget, _deadline, sparse_snapshot
from .metadata import UnsupportedCase
from .metadata_fallback import _messages, _old_messages, _old_root_address, _unique
from .modern_indexes import ModernH5File, lookup3
from .recovery import VERSION, RecoveryError, _verify_source, sha256_file


def open_by_address(handle, address):
    """Wrap the installed HDF5 library's public H5Oopen_by_addr API."""
    if type(address) is not int or address < 0:
        raise ValueError("object address must be a nonnegative integer")
    try:
        library = ctypes.CDLL(h5py.h5o.__file__)
        function = library.H5Oopen_by_addr
    except (OSError, AttributeError) as exc:
        # Windows wheels may keep HDF5 exports in their package DLL.
        libraries = list((Path(h5py.__file__).parent.parent / "h5py.libs").glob("*hdf5*.dll"))
        function = None
        for path in libraries:
            try:
                library = ctypes.CDLL(str(path))
                function = library.H5Oopen_by_addr
                break
            except (OSError, AttributeError):
                continue
        if function is None:
            raise UnsupportedCase("the installed HDF5 library does not expose H5Oopen_by_addr") from exc
    function.argtypes, function.restype = [ctypes.c_int64, ctypes.c_uint64], ctypes.c_int64
    identifier = function(handle.id.id, address)
    if identifier < 0:
        raise RecoveryError("native HDF5 could not open the checked dataset header")
    wrapped = h5py.h5i.wrap_identifier(identifier)
    if not isinstance(wrapped, h5py.h5d.DatasetID):
        wrapped.close()
        raise UnsupportedCase("selected object address is not a dataset")
    return h5py.Dataset(wrapped)


def _scan_headers(reader, budget, deadline):
    """Scan physical data extents, including signature crossings at blocks."""
    stream = reader._file
    cursor, scanned, found = 0, 0, set()
    sparse = hasattr(os, "SEEK_DATA") and hasattr(os, "SEEK_HOLE")
    while cursor < reader.size:
        _deadline(deadline)
        if sparse:
            try:
                start = os.lseek(stream.fileno(), cursor, os.SEEK_DATA)
                end = min(reader.size, os.lseek(stream.fileno(), start, os.SEEK_HOLE))
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break
                if cursor == 0 and exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
                    sparse = False
                    continue
                raise UnsupportedCase("metadata discovery could not enumerate sparse extents") from exc
        else:
            start, end = cursor, reader.size
        tail = b""
        position = start
        while position < end:
            _deadline(deadline)
            stream.seek(position)
            block = stream.read(min(budget.block_bytes, end - position))
            if not block:
                raise RecoveryError("source ended during metadata discovery")
            scanned += len(block)
            if scanned > budget.max_copied_bytes:
                raise UnsupportedCase("metadata scan exceeds the configured physical-byte budget")
            data = tail + block
            base, offset = position - len(tail), 0
            while True:
                index = data.find(b"OHDR", offset)
                if index < 0:
                    break
                found.add(base + index - reader.superblock.base_address)
                if len(found) > budget.max_objects:
                    raise UnsupportedCase("metadata discovery has too many candidate signatures")
                offset = index + 4
            offset = 0
            while True:
                index = data.find(b"\x01\x00", offset)
                if index < 0:
                    break
                absolute = base + index
                prefix = data[index:index + 16]
                if (absolute % 8 == 0 and len(prefix) == 16 and prefix[12:] == b"\x00" * 4
                        and 1 <= int.from_bytes(prefix[2:4], "little") <= 4096
                        and 1 <= int.from_bytes(prefix[4:8], "little") <= budget.max_links
                        and 8 <= int.from_bytes(prefix[8:12], "little") <= 1024 * 1024):
                    found.add(absolute - reader.superblock.base_address)
                    if len(found) > budget.max_objects:
                        raise UnsupportedCase("metadata discovery exceeds its candidate budget")
                offset = index + 2
            tail = data[-15:]
            position += len(block)
        cursor = end
    return sorted(found), scanned



def _discovery_reader(image):
    from .rescue import file_condition
    version = file_condition(Path(image)).get("superblock_version")
    return H5File(image) if version in (0, 1) else ModernH5File(image, large_sparse_scan=True)

def scan_local(image, budget=None):
    budget = budget or LargeBudget()
    deadline = time.monotonic() + budget.max_seconds
    datasets, headers, rejected = [], [], 0
    with _discovery_reader(image) as reader:
        headers.extend(reader.metadata_ranges)
        candidates, scanned = _scan_headers(reader, budget, deadline)
        limit_reached = False
        for address in candidates:
            _deadline(deadline)
            reader.metadata_ranges = reader.metadata_ranges[:1]
            try:
                prefix = reader.read_at(address, 4)
                messages = _messages(reader, address) if prefix == b"OHDR" else _old_messages(reader, address)
                headers.extend(reader.metadata_ranges[1:])
                space, typ, layout = (_unique(messages, kind) for kind in (1, 3, 8))
                if space is None or typ is None or layout is None:
                    continue
                if any(item.flags & 2 for item in (space, typ, layout)):
                    raise UnsupportedCase("detached shared metadata needs its rooted sharing table")
                datatype = h5py.h5t.decode(b"\x03\x00" + typ.data)
                from .logical_types import validate_type
                validate_type(datatype)
                if len(datasets) >= budget.max_objects:
                    limit_reached = True
                    break
                datasets.append({"address": address, "dtype": datatype.dtype.str,
                    "header_validation": "checksum" if prefix == b"OHDR" else "unchecksummed legacy structure and independent native agreement",
                    "file_type_encoding_hex": datatype.encode().hex(),
                    "header_ranges": [list(part) for part in reader.metadata_ranges[1:]],
                    "identity": "checked object address; original dataset path is unknown"})
            except (FormatError, UnsupportedFormat, ValueError, OSError, RuntimeError):
                rejected += 1
        if limit_reached:
            raise UnsupportedCase("detached dataset count exceeds the discovery budget")
    return {"mode": "object_header_discovery", "datasets": datasets,
        "metadata_ranges": [list(part) for part in sorted(set(headers))],
        "scan_physical_bytes": scanned, "signature_candidates": len(candidates),
        "rejected_headers": rejected, "complete_namespace": False,
        "scope": "modern checked and legacy structural object headers; names and unreadable objects remain unresolved"}


@contextmanager
def detached_view(image):
    """If needed, use an empty root in a disposable view, leaving source intact.

    Only the superblock root pointer, declared EOF and checksum are changed.
    The added root is outside the original physical EOF and never supplies
    measurement values. Candidate allocations must fit the original source.
    """
    try:
        with h5py.File(image, "r") as handle:
            handle["/"]
        yield Path(image), {"kind": "unmodified_source_image"}
        return
    except (OSError, RuntimeError, KeyError, ValueError):
        pass
    with _discovery_reader(image) as reader:
        sb = reader.superblock
        legacy_root = _old_root_address(reader)[0] if sb.version < 2 else None
    if sb.offset_size != 8 or sb.length_size != 8:
        raise UnsupportedCase("detached root view requires the usual eight-byte HDF5 address widths")
    with tempfile.TemporaryDirectory(prefix="h5reclaim-detached-") as directory:
        empty, view = Path(directory) / "empty.h5", Path(directory) / "view.h5"
        with h5py.File(empty, "w", libver=("v108", "v108")):
            pass
        with ModernH5File(empty) as reader:
            root = reader.superblock.root_object_address
            messages = _messages(reader, root)
            if any(item.kind in (1, 3, 6, 8, 0x10, 0x0C) for item in messages):
                raise RecoveryError("empty root template contains unexpected metadata")
            ranges = reader.metadata_ranges[1:]
            if len(ranges) != 1:
                raise RecoveryError("empty root template has a continuation")
            start, end, _ = ranges[0]
            root_bytes = reader._read_absolute(start, end - start)
        shutil.copyfile(image, view)
        appended = (view.stat().st_size + 7) & ~7
        with view.open("r+b") as stream:
            stream.seek(appended)
            stream.write(root_bytes)
            eof = stream.tell()
            stream.seek(sb.signature_offset)
            if sb.version >= 2:
                raw = bytearray(stream.read(16 + 4 * sb.offset_size))
                raw[12 + 2 * sb.offset_size:12 + 3 * sb.offset_size] = eof.to_bytes(sb.offset_size, "little")
                raw[12 + 3 * sb.offset_size:12 + 4 * sb.offset_size] = (appended - sb.base_address).to_bytes(sb.offset_size, "little")
                raw[-4:] = lookup3(raw[:-4]).to_bytes(4, "little")
            else:
                prefix_size = 28 if sb.version == 1 else 24
                root_entry = prefix_size + 4 * sb.offset_size
                raw = bytearray(stream.read(root_entry + sb.length_size + sb.offset_size + 24))
                raw[prefix_size + 2 * sb.offset_size:prefix_size + 3 * sb.offset_size] = eof.to_bytes(sb.offset_size, "little")
                start = root_entry + sb.length_size
                raw[start:start + sb.offset_size] = (appended - sb.base_address).to_bytes(sb.offset_size, "little")
                raw[start + sb.offset_size:] = bytes(len(raw) - start - sb.offset_size)
            stream.seek(sb.signature_offset)
            stream.write(raw)
        yield view, {"kind": "disposable_empty_root", "original_root_address": legacy_root if sb.version < 2 else sb.root_object_address,
                     "appended_root_address": appended - sb.base_address,
                     "modified_fields": ["superblock root pointer", "declared EOF",
                                         "legacy root cache" if sb.version < 2 else "superblock checksum"]}


def _validate_candidates(image, discovery, budget):
    from .native_stream import inspect_storage
    deadline = time.monotonic() + budget.max_seconds
    allocations, metadata = [], discovery["metadata_ranges"]
    with detached_view(image) as (view, view_record), h5py.File(view, "r") as handle:
        for entry in discovery["datasets"]:
            selected = open_by_address(handle, entry["address"])
            try:
                if selected.id.get_type().encode().hex() != entry["file_type_encoding_hex"]:
                    raise FormatError("native datatype differs from the checked raw datatype message")
                records, ranges, filters = inspect_storage(selected, Path(image).stat().st_size, budget, deadline,
                                                          require_codecs=False)
                entry["shape"] = list(selected.shape) if selected.shape is not None else None
                entry["chunks"] = list(selected.chunks) if selected.chunks else None
                entry["filters"] = filters
                entry["allocations"] = [list(part) for part in ranges]
                for start, end, origin in ranges:
                    if any(start < stop and begin < end for begin, stop, _ in metadata):
                        raise FormatError("detached dataset allocation overlaps a checked object header")
                    allocations.append((start, end, entry["address"]))
            finally:
                selected.id.close()
    allocations.sort()
    if any(a[1] > b[0] for a, b in zip(allocations, allocations[1:])):
        raise FormatError("detached dataset headers claim overlapping allocations")
    discovery["inspection_view"] = view_record
    return discovery


def discover(source, *, budget=None):
    from dataclasses import asdict
    from .source_session import share_image, worker_environment
    from .worker_limits import run_worker
    budget = budget or LargeBudget()
    source = Path(source).absolute()
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, copied):
        with share_image(image, digest, size, copied, budget), tempfile.TemporaryDirectory(prefix="h5reclaim-discover-") as directory:
            response = Path(directory) / "response.json"
            environment = worker_environment()
            environment["HDF5_PLUGIN_PRELOAD"] = "::"
            environment.pop("HDF5_PLUGIN_PATH", None)
            try:
                completed = run_worker([sys.executable, "-m", "h5reclaim.object_discovery",
                    str(image), str(response), json.dumps(asdict(budget))], env=environment,
                    timeout_seconds=budget.max_seconds + 30, memory_bytes=budget.worker_memory_bytes)
            except subprocess.TimeoutExpired as exc:
                raise RecoveryError("metadata discovery worker exceeded its configured deadline") from exc
            if not response.is_file() or response.stat().st_size > budget.max_metadata_bytes * 4:
                raise RecoveryError("metadata discovery worker did not produce a bounded response")
            message = json.loads(response.read_text(encoding="utf-8"))
            if completed.returncode or message.get("status") != "ok":
                if message.get("kind") == "FormatError":
                    raise FormatError(message["detail"])
                raise UnsupportedCase(message.get("detail", "metadata discovery failed"))
            result = message["discovery"]
        result.update({"schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                       "source": {"path": str(source), "sha256": digest, "size_bytes": size}})
        _verify_source(source, identity, digest)
        return result


def export_object(source, address, dataset_path, output, report_path, *, budget=None,
                  published_output=None, hints=None):
    from .native_stream import export_native_stream
    budget = budget or LargeBudget()
    source = Path(source)
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, copied):
        discovery = _validate_candidates(image, scan_local(image, budget), budget)
        candidates = [item for item in discovery["datasets"] if item["address"] == address]
        if len(candidates) != 1:
            raise UnsupportedCase("object address is not a uniquely validated dataset header")
        owners = {"complete": False, "scope": "detached validated object headers",
                  "checked_dataset_headers": len(discovery["datasets"]),
                  "competing_allocations": False, "unreadable_owners_remain_unknown": True}
        from .source_session import share_image
        from .recovery import _validate_paths
        output, report_path = Path(output), Path(report_path)
        _validate_paths(source, output, report_path)
        with share_image(image, digest, size, copied, budget), tempfile.TemporaryDirectory(prefix=".h5reclaim-object-", dir=output.parent) as directory:
            staged_output, staged_report = Path(directory) / "output.h5", Path(directory) / "report.json"
            with detached_view(image) as (view, view_record):
                result = export_native_stream(image, dataset_path, staged_output, staged_report, budget=budget,
                published_output=published_output, hints=hints, object_address=address,
                detached_inventory=owners, inspection_image=view)
            result["mode"] = "detached_object_export"
            result["dataset"]["identity"] = "selected object address; output path supplied by the operator"
            result["metadata_discovery"] = {"inspection_view": view_record,
                                       "candidate": candidates[0], "complete_namespace": False}
            result["source"] = {"path": str(source), "size_bytes": size,
                            "sha256_before": digest, "sha256_after": digest}
            result["output_path"] = str(published_output or output)
            serialized = json.dumps(result, sort_keys=True, indent=2) + "\n"
            with h5py.File(staged_output, "r+") as handle:
                from .output_annotations import report_metadata_group
                handle[report_metadata_group(result) + "/report_json"][()] = serialized
            staged_report.write_text(serialized, encoding="utf-8")
            if staged_output.stat().st_size > budget.max_output_bytes:
                raise UnsupportedCase("annotated object output exceeds the configured byte budget")
            _verify_source(source, identity, digest)
            _validate_paths(source, output, report_path)
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-object-report-", dir=report_path.parent) as repdir:
                report_copy = Path(repdir) / "report.json"
                report_copy.write_text(serialized, encoding="utf-8")
                os.link(report_copy, report_path)
                try:
                    os.link(staged_output, output)
                except Exception:
                    report_path.unlink(missing_ok=True)
                    raise
        return result


def main():
    if len(sys.argv) != 4:
        return 2
    response = Path(sys.argv[2])
    try:
        from .native_worker import _apply_memory_limit
        budget = LargeBudget(**json.loads(sys.argv[3]))
        _apply_memory_limit(budget.worker_memory_bytes)
        from .source_session import activate_worker_session
        activate_worker_session()
        image = Path(sys.argv[1])
        discovery = _validate_candidates(image, scan_local(image, budget), budget)
        message = {"status": "ok", "discovery": discovery}
        code = 0
    except Exception as exc:
        message = {"status": "error", "kind": type(exc).__name__, "detail": str(exc)[:300]}
        code = 2
    response.write_text(json.dumps(message), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
