"""Recover native-readable variable strings and ragged numeric elements."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from itertools import product
from math import prod
from pathlib import Path

import h5py
import numpy as np

from .metadata import UnsupportedCase
from .readable_export import (
    _allocated_selections, _check_competing_owners, _check_storage,
    _create_matching_dataset, _safe_fixed_type, _selected_dataset, _source_records,
)
from .recovery import (VERSION, RecoveryError, _validate_paths, _verify_source,
                       sha256_file, source_snapshot)

MAX_ELEMENTS = 1_048_576
MAX_ELEMENT_BYTES = 1024 * 1024
MAX_VALUE_BYTES = 512 * 1024 * 1024


def _variable_type(dataset: h5py.Dataset) -> tuple[str, np.dtype | None]:
    typ = dataset.id.get_type()
    if typ.get_class() == h5py.h5t.STRING and typ.is_variable_str():
        if typ.get_cset() not in (h5py.h5t.CSET_ASCII, h5py.h5t.CSET_UTF8):
            raise UnsupportedCase("variable string encoding is not recognized")
        return "string", None
    if typ.get_class() == h5py.h5t.VLEN:
        base = typ.get_super()
        dtype = np.dtype(base.dtype)
        _safe_fixed_type(base, dtype)
        if dtype.kind not in "iuf" or dtype.fields or dtype.subdtype or dtype.hasobject:
            raise UnsupportedCase("ragged native export needs a primitive numeric base type")
        return "numeric", dtype
    raise UnsupportedCase("selected dataset is not variable strings or ragged numeric data")


def _value(value, kind: str, dtype: np.dtype | None) -> tuple[object, bytes]:
    if kind == "string":
        if isinstance(value, str):
            value = value.encode("utf-8")
        if not isinstance(value, bytes):
            raise ValueError("variable string read returned an unexpected representation")
        encoded = b"S" + len(value).to_bytes(8, "little") + value
    else:
        value = np.asarray(value, dtype=dtype)
        if value.ndim != 1 or value.dtype != dtype:
            raise ValueError("ragged numeric element differs from its declared base type")
        encoded = b"N" + len(value).to_bytes(8, "little") + value.tobytes(order="C")
    if len(encoded) > MAX_ELEMENT_BYTES:
        raise ValueError("decoded variable element exceeds its byte budget")
    return value, encoded


def _write_value(dataset: h5py.Dataset, index: tuple[int, ...], value,
                 kind: str, dtype: np.dtype | None) -> None:
    if kind == "string":
        dataset[index] = value
        return
    # h5py's object/VLEN bridge marshals a native numeric buffer using the
    # declared file base type. Pass that type's bytes through a native view,
    # avoiding Dataset.__setitem__'s additional base-dtype conversion.
    encoded = np.ascontiguousarray(value, dtype=dtype)
    block = np.empty((1,), dtype=object)
    block[0] = encoded.view(dtype.newbyteorder("="))
    file_space = dataset.id.get_space()
    if index:
        file_space.select_hyperslab(index, (1,) * len(index))
    dataset.id.write(h5py.h5s.create_simple((1,)), file_space, block)


def export_variable(source: str | Path, dataset_path: str, output: str | Path,
                    report_path: str | Path, *, published_output: str | Path | None = None) -> dict:
    """Export individually readable elements with explicit element status.

    Native HDF5 resolves heap-backed values. Descriptor allocation and owner
    checks establish current placement; this route makes no historical hash
    claim about the source heap. Every accepted decoded element is read back
    from the derived file before publication.
    """
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    with source_snapshot(source) as (image, digest, identity, size):
        with h5py.File(image, "r") as opened:
            selected = _selected_dataset(opened, dataset_path)
            kind, base = _variable_type(selected)
            if selected.shape is None:
                raise UnsupportedCase("null variable dataset has no stored elements")
            shape = tuple(int(value) for value in selected.shape)
            if len(shape) > 32 or any(value < 1 for value in shape) or prod(shape) > MAX_ELEMENTS:
                raise UnsupportedCase("variable dataset element grid exceeds its byte and iteration budget")
            # H5Tget_size reports the in-memory pointer or hvl_t size. File
            # descriptors store a 4-byte length and an address + 4-byte heap ID.
            from .rescue import file_condition
            offset_size = file_condition(image).get("offset_size")
            if offset_size not in (1, 2, 4, 8):
                raise UnsupportedCase("variable file descriptor width is unavailable")
            file_itemsize = offset_size + 8
            storage = _check_storage(selected, size, file_itemsize)
            ownership = _check_competing_owners(image, selected, storage)
            status = np.full(shape, 2, dtype="u1")
            source_values = hashlib.sha256()
            accepted = value_bytes = failed = 0
            failures = []
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-variable-", dir=output.parent) as outdir:
                with tempfile.TemporaryDirectory(prefix=".h5reclaim-variable-", dir=report_path.parent) as repdir:
                    staged = Path(outdir) / "output.h5"
                    staged_report = Path(repdir) / "report.json"
                    with h5py.File(staged, "x") as exported:
                        target = _create_matching_dataset(exported, selected, dataset_path)
                        for selection in _allocated_selections(storage, shape, selected.chunks, file_itemsize):
                            indices = product(*(range(part.start, part.stop) for part in selection))
                            for index in indices:
                                try:
                                    value, encoded = _value(selected[index], kind, base)
                                except (OSError, RuntimeError, ValueError, TypeError, OverflowError) as exc:
                                    status[index] = 6
                                    failed += 1
                                    if len(failures) < 128:
                                        failures.append({"coordinate": list(index), "reason": str(exc)[:240]})
                                    continue
                                value_bytes += len(encoded)
                                if value_bytes > MAX_VALUE_BYTES:
                                    raise UnsupportedCase("decoded variable values exceed the output byte budget")
                                _write_value(target, index, value, kind, base)
                                token = json.dumps(index).encode("ascii") + encoded
                                source_values.update(token)
                                status[index] = 1
                                accepted += 1
                                if accepted % 128 == 0:
                                    exported.flush()
                                    if staged.stat().st_size > MAX_VALUE_BYTES:
                                        raise UnsupportedCase("variable output exceeds its byte budget")
                        exported.require_group("/_h5reclaim").create_dataset("element_status", data=status, dtype="u1")
                        exported.flush()
                    output_values = hashlib.sha256()
                    with h5py.File(staged, "r") as checked:
                        target = checked[dataset_path]
                        if not target.id.get_type().equal(selected.id.get_type()):
                            raise RecoveryError("derived variable datatype differs from the source")
                        for selection in _allocated_selections(storage, shape, selected.chunks, file_itemsize):
                            for index in product(*(range(part.start, part.stop) for part in selection)):
                                if status[index] == 1:
                                    _, encoded = _value(target[index], kind, base)
                                    output_values.update(json.dumps(index).encode("ascii") + encoded)
                    if output_values.digest() != source_values.digest():
                        raise RecoveryError("derived variable elements differ from the source reads")
                    report = {"schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                              "mode": "variable_native_readable_export",
                              "outcome": "complete" if accepted == prod(shape) else "partial",
                              "source": {"path": str(source), "size_bytes": size,
                                         "sha256_before": digest, "sha256_after": digest},
                              "dataset": {"path": dataset_path, "shape": list(shape), "kind": kind,
                                          "file_type_encoding_hex": selected.id.get_type().encode().hex(),
                                          "file_descriptor_bytes": file_itemsize,
                                          "source_chunk_records": _source_records(image, storage),
                                          "chunks": list(selected.chunks) if selected.chunks else None,
                                          "attributes_copied": [],
                                          "attributes_omitted": list(selected.attrs)},
                              "accepted_elements": accepted, "unknown_elements": prod(shape) - accepted,
                              "decoded_value_bytes": value_bytes, "failed_elements": failed,
                              "failed_element_examples": failures,
                              "native_value_sha256": source_values.hexdigest(),
                              "validity": {"dataset": "/_h5reclaim/element_status", "granularity": "element",
                                           "codes": {"1": "allocated and read back equal", "2": "unallocated or unknown",
                                                     "6": "native element decoding failed"}},
                              "ownership_inventory": ownership, "output_path": str(published_output or output),
                              "value_evidence": "Native heap interpretation with decoded element readback verification."}
                    serialized = json.dumps(report, sort_keys=True, indent=2) + "\n"
                    with h5py.File(staged, "r+") as exported:
                        exported["/_h5reclaim"].create_dataset("report_json", data=serialized,
                                                             dtype=h5py.string_dtype("utf-8"))
                        exported["/_h5reclaim"].attrs["source_sha256"] = digest
                        exported.flush()
                    if staged.stat().st_size > MAX_VALUE_BYTES:
                        raise UnsupportedCase("variable output exceeds its byte budget")
                    staged_report.write_text(serialized, encoding="utf-8")
                    if sha256_file(image) != digest:
                        raise RecoveryError("variable source snapshot changed during export")
                    _verify_source(source, identity, digest)
                    _validate_paths(source, output, report_path)
                    os.link(staged_report, report_path)
                    try:
                        os.link(staged, output)
                    except Exception:
                        report_path.unlink(missing_ok=True)
                        raise
                    return report
