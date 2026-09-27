"""Optional acquisition-time XOR parity for one selected chunked dataset.

This sidecar must be captured while a complete source and its separately kept
chunk-hash baseline are available. It repairs one unmatched nominal chunk per
stripe only when every companion is independently located by a rooted HDF5
index and matches its *prior* baseline hash. It cannot establish when the
operator's baseline was captured or recover two lost chunks in one stripe.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from itertools import product
from math import prod
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .metadata import UnsupportedCase
from .recovery import (
    MAX_CHUNKS, STATUS_CODES, VERSION, RecoveryError, _identity, _validate_paths,
    _verify_source, analyze, sha256_file,
)
from .replica_recovery import (
    MAX_MANIFEST_BYTES, MAX_REPORT_BYTES, _baseline_chunks, _digest,
    _index_evidence, _input_aliases, _load_json, _pinned, _schema,
    _unique_pairs,
)


MAX_PARITY_BYTES = 256 * 1024 * 1024
MAX_STRIPE_WIDTH = 16
DEFAULT_STRIPE_WIDTH = 4


def _origins(spec: Any) -> list[tuple[int, ...]]:
    if prod(spec.chunk_grid) > MAX_CHUNKS:
        raise UnsupportedCase("parity dataset exceeds the structural chunk-grid limit")
    return [tuple(i * width for i, width in zip(indices, spec.chunks))
            for indices in product(*(range(length) for length in spec.chunk_grid))]


def _xor(target: bytearray, payload: bytes) -> None:
    if len(target) != len(payload):
        raise RecoveryError("nominal decoded chunk does not match selected chunk size")
    np.bitwise_xor(np.frombuffer(target, dtype="u1"), np.frombuffer(payload, dtype="u1"),
                   out=np.frombuffer(target, dtype="u1"))


def _serialized(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def capture_parity_sidecar(
    source: str | Path, dataset_path: str, baseline_path: str | Path,
    destination: str | Path, *, stripe_width: int = DEFAULT_STRIPE_WIDTH,
) -> dict[str, Any]:
    """Create a separate ZIP with uncompressed XOR parity and a bound manifest.

    The baseline must already exist and match every chunk of the fully decoded
    current source. Capture never overwrites a destination and never writes the
    source or baseline. Retain the sidecar and its SHA-256 independently.
    """
    source, baseline_path, destination = Path(source), Path(baseline_path), Path(destination)
    if type(stripe_width) is not int or not 1 <= stripe_width <= MAX_STRIPE_WIDTH:
        raise RecoveryError(f"stripe_width must be an integer from 1 through {MAX_STRIPE_WIDTH}")
    if not destination.parent.is_dir() or destination.exists() or destination.is_symlink():
        raise RecoveryError("parity destination parent must exist and destination must be new")
    if source.resolve(strict=True) == destination.resolve(strict=False):
        raise RecoveryError("parity destination aliases source")
    baseline, baseline_digest, baseline_identity = _load_json(baseline_path, "baseline")
    _input_aliases([source, baseline_path], destination, destination)
    analysis = analyze(source, dataset_path)
    if not analysis.report["complete"]:
        raise UnsupportedCase("parity capture requires every selected chunk to be allocated and decoded")
    source_digest = analysis.report["source"]["sha256_before"]
    if baseline.get("source_sha256") != source_digest:
        raise RecoveryError("baseline source hash differs from source at parity capture")
    hashes = _baseline_chunks(baseline, analysis)
    origins = _origins(analysis.spec)
    records = {record.coordinate: record for record in analysis.records}
    if len(records) != len(origins) or set(records) != set(origins) or set(hashes) != set(origins):
        raise RecoveryError("parity capture needs one prior hash and one rooted record per chunk")
    size = analysis.spec.chunk_bytes
    if size <= 0 or size > 1024 * 1024:
        raise UnsupportedCase("nominal parity chunk exceeds the 1 MiB chunk limit")
    if ((len(origins) + stripe_width - 1) // stripe_width) * size > MAX_PARITY_BYTES:
        raise UnsupportedCase("parity sidecar exceeds its 256 MiB payload limit")
    for origin, record in records.items():
        if len(record.payload) != size or hashlib.sha256(record.payload).hexdigest() != hashes[origin]:
            raise RecoveryError("baseline chunk hash differs from source at parity capture")
        _index_evidence(analysis, record)

    stripes: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-parity-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "parity.zip"
        with zipfile.ZipFile(staged, "x", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as archive:
            for ordinal, start in enumerate(range(0, len(origins), stripe_width)):
                members = origins[start:start + stripe_width]
                parity = bytearray(size)
                for origin in members:
                    _xor(parity, records[origin].payload)
                entry = f"parity/{ordinal:06d}.bin"
                archive.writestr(entry, parity, compress_type=zipfile.ZIP_STORED)
                stripes.append({
                    "ordinal": ordinal, "entry": entry, "members": [list(c) for c in members],
                    "byte_length": size, "sha256": hashlib.sha256(parity).hexdigest(),
                })
            document = {
                "schema_version": 1,
                "kind": "prospective_xor_parity_sidecar",
                "source_sha256": source_digest,
                "baseline_sha256": baseline_digest,
                "dataset": _schema(analysis.spec),
                "nominal_chunk_bytes": size,
                "stripe_width": stripe_width,
                "stripes": stripes,
                "trust_note": (
                    "Retain this file separately. Hash and parity bind observed capture bytes, "
                    "not the date, provenance, or scientific correctness of that capture."
                ),
            }
            encoded = _serialized(document)
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise UnsupportedCase("parity manifest exceeds the metadata limit")
            archive.writestr("manifest.json", encoded, compress_type=zipfile.ZIP_STORED)
        if staged.stat().st_size > MAX_PARITY_BYTES + MAX_MANIFEST_BYTES + 512 * MAX_CHUNKS:
            raise UnsupportedCase("parity archive exceeds the sidecar size limit")
        _verify_source(source, analysis.source_identity, source_digest)
        _verify_source(baseline_path, baseline_identity, baseline_digest)
        _input_aliases([source, baseline_path], destination, destination)
        if destination.exists() or destination.is_symlink():
            raise RecoveryError("parity destination already exists")
        os.link(staged, destination)
    return document


def _read_sidecar(
    archive: zipfile.ZipFile, analysis: Any, baseline_digest: str,
    hashes: dict[tuple[int, ...], str],
) -> tuple[dict[str, Any], list[tuple[list[tuple[int, ...]], bytes, str]]]:
    names = archive.namelist()
    if len(names) != len(set(names)) or len(names) > MAX_CHUNKS + 1:
        raise RecoveryError("parity archive has duplicate or excessive entries")
    if "manifest.json" not in names:
        raise RecoveryError("parity archive has no manifest")
    info = archive.getinfo("manifest.json")
    if info.compress_type != zipfile.ZIP_STORED or info.file_size > MAX_MANIFEST_BYTES:
        raise RecoveryError("parity archive manifest is compressed or oversized")
    try:
        doc = json.loads(archive.read("manifest.json").decode("utf-8"),
                         object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RuntimeError, zipfile.BadZipFile) as exc:
        raise RecoveryError(f"parity archive manifest is invalid: {exc}") from exc
    keys = {"schema_version", "kind", "source_sha256", "baseline_sha256", "dataset",
            "nominal_chunk_bytes", "stripe_width", "stripes", "trust_note"}
    if not isinstance(doc, dict) or set(doc) != keys or type(doc["schema_version"]) is not int \
            or doc["schema_version"] != 1 or doc["kind"] != "prospective_xor_parity_sidecar":
        raise RecoveryError("unsupported or malformed parity archive manifest")
    _digest(doc["source_sha256"], "parity source_sha256")
    if doc["baseline_sha256"] != baseline_digest:
        raise RecoveryError("parity sidecar is bound to a different baseline")
    if doc["dataset"] != _schema(analysis.spec):
        raise RecoveryError("parity sidecar schema contradicts damaged dataset")
    size = analysis.spec.chunk_bytes
    width = doc["stripe_width"]
    if type(doc["nominal_chunk_bytes"]) is not int or doc["nominal_chunk_bytes"] != size \
            or not 0 < size <= 1024 * 1024 or type(width) is not int \
            or not 1 <= width <= MAX_STRIPE_WIDTH:
        raise RecoveryError("parity sidecar chunk size or stripe width is invalid")
    origins = _origins(analysis.spec)
    if set(hashes) != set(origins):
        raise RecoveryError("baseline does not cover every selected chunk")
    expected_count = (len(origins) + width - 1) // width
    rows = doc["stripes"]
    if not isinstance(rows, list) or len(rows) != expected_count or len(names) != expected_count + 1:
        raise RecoveryError("parity archive stripe count differs from dataset grid")
    if expected_count * size > MAX_PARITY_BYTES:
        raise RecoveryError("parity payload exceeds sidecar size limit")
    checked = []
    for ordinal, row in enumerate(rows):
        members = origins[ordinal * width:(ordinal + 1) * width]
        entry = f"parity/{ordinal:06d}.bin"
        if not isinstance(row, dict) or set(row) != {"ordinal", "entry", "members", "byte_length", "sha256"} \
                or type(row["ordinal"]) is not int or row["ordinal"] != ordinal \
                or row["entry"] != entry or row["members"] != [list(c) for c in members] \
                or type(row["byte_length"]) is not int or row["byte_length"] != size:
            raise RecoveryError("parity stripe contradicts canonical coordinate partition")
        _digest(row["sha256"], "parity stripe.sha256")
        if entry not in names:
            raise RecoveryError("parity archive is missing a stripe")
        info = archive.getinfo(entry)
        if info.compress_type != zipfile.ZIP_STORED or info.file_size != size \
                or info.compress_size != size or info.is_dir():
            raise RecoveryError("parity stripe is compressed or has a wrong length")
        try:
            payload = archive.read(entry)
        except (RuntimeError, zipfile.BadZipFile) as exc:
            raise RecoveryError(f"parity stripe failed ZIP integrity: {exc}") from exc
        if hashlib.sha256(payload).hexdigest() != row["sha256"]:
            raise RecoveryError("parity stripe differs from recorded digest")
        checked.append((members, payload, entry))
    return doc, checked


def restore_from_parity(
    source: str | Path, dataset_path: str, manifest: str | Path,
    output: str | Path, report_path: str | Path,
) -> dict[str, Any]:
    """Publish a validity-mapped output using one prior-hash-checked loss per stripe.

    Manifest fields: schema_version=1, damaged_sha256, and absolute, SHA-256
    pinned baseline and parity paths. The manifest itself is separate from
    the capture and does not prove when it was authored.
    """
    source, manifest_path = Path(source), Path(manifest)
    output, report_path = Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    manifest_data, manifest_hash, manifest_identity = _load_json(manifest_path, "parity recovery manifest")
    if set(manifest_data) != {"schema_version", "damaged_sha256", "baseline", "parity"} \
            or type(manifest_data["schema_version"]) is not int \
            or manifest_data["schema_version"] != 1:
        raise RecoveryError("parity recovery manifest requires schema_version, damaged_sha256, baseline, parity")
    source_hash = _digest(manifest_data["damaged_sha256"], "damaged_sha256")
    baseline_pin = _pinned(manifest_data["baseline"], "baseline")
    parity_pin = _pinned(manifest_data["parity"], "parity")
    inputs = [source, manifest_path, baseline_pin.path, parity_pin.path]
    _input_aliases(inputs, output, report_path)
    if sha256_file(source) != source_hash:
        raise RecoveryError("damaged source differs from pinned manifest hash")
    baseline, baseline_hash, baseline_identity = _load_json(baseline_pin.path, "baseline")
    if baseline_hash != baseline_pin.sha256:
        raise RecoveryError("baseline differs from pinned manifest hash")
    if parity_pin.path.stat().st_size > MAX_PARITY_BYTES + MAX_MANIFEST_BYTES + 512 * MAX_CHUNKS:
        raise RecoveryError("parity archive exceeds sidecar size limit")
    parity_identity = _identity(parity_pin.path.stat())
    if sha256_file(parity_pin.path) != parity_pin.sha256 or _identity(parity_pin.path.stat()) != parity_identity:
        raise RecoveryError("parity differs from pinned manifest hash")
    damaged = analyze(source, dataset_path)
    if damaged.report["source"]["sha256_before"] != source_hash:
        raise RecoveryError("damaged source changed during structural analysis")
    hashes = _baseline_chunks(baseline, damaged)
    try:
        with zipfile.ZipFile(parity_pin.path) as archive:
            document, stripes = _read_sidecar(archive, damaged, baseline_hash, hashes)
    except (OSError, zipfile.BadZipFile) as exc:
        raise RecoveryError(f"parity archive cannot be read: {exc}") from exc
    if document["source_sha256"] != baseline["source_sha256"]:
        raise RecoveryError("parity source digest differs from captured baseline source")
    spec = damaged.spec
    records = {record.coordinate: record for record in damaged.records}
    if len(records) != len(damaged.records):
        raise RecoveryError("damaged file has duplicate chunk coordinate evidence")
    status = np.full(spec.chunk_grid, STATUS_CODES["allocation_unknown"], dtype="u1")
    chosen: dict[tuple[int, ...], tuple[bytes, dict[str, Any]]] = {}
    unresolved: list[dict[str, Any]] = []
    reconstructed = 0
    for ordinal, (members, parity, entry) in enumerate(stripes):
        good: dict[tuple[int, ...], tuple[bytes, dict[str, Any]]] = {}
        unknown: list[tuple[int, ...]] = []
        for origin in members:
            record = records.get(origin)
            if record is not None and hashlib.sha256(record.payload).hexdigest() == hashes[origin]:
                good[origin] = (record.payload, {
                    "coordinate": list(origin), "source_id": "damaged",
                    "expected_capture_decoded_sha256": hashes[origin],
                    "evidence": _index_evidence(damaged, record),
                })
            else:
                unknown.append(origin)
        chosen.update(good)
        if len(unknown) == 1:
            candidate = bytearray(parity)
            for payload, _mapping in good.values():
                _xor(candidate, payload)
            origin = unknown[0]
            if hashlib.sha256(candidate).hexdigest() == hashes[origin]:
                chosen[origin] = (bytes(candidate), {
                    "coordinate": list(origin), "source_id": "parity",
                    "expected_capture_decoded_sha256": hashes[origin],
                    "parity": {"stripe": ordinal, "entry": entry,
                               "stripe_sha256": hashlib.sha256(parity).hexdigest(),
                               "companion_evidence": [item[1] for item in good.values()]},
                })
                reconstructed += 1
                continue
        for origin in unknown:
            record = records.get(origin)
            current_hash = hashlib.sha256(record.payload).hexdigest() if record else None
            unresolved.append({
                "coordinate": list(origin),
                "reason": ("multiple unverified chunks in one parity stripe" if len(unknown) > 1 else
                           "parity reconstruction contradicts prior chunk hash"),
                "stripe": ordinal, "observed_decoded_sha256": current_hash,
            })
            indices = tuple(pos // width for pos, width in zip(origin, spec.chunks))
            status[indices] = (STATUS_CODES["decode_failed"] if any(
                item.get("coordinate") == list(origin) for item in damaged.report.get("failed_chunks", ())
            ) else STATUS_CODES["unavailable"])

    mappings = []
    for origin, (_payload, mapping) in sorted(chosen.items()):
        indices = tuple(pos // width for pos, width in zip(origin, spec.chunks))
        status[indices] = STATUS_CODES["recovered"]
        mappings.append({**mapping, "chunk_index": list(indices)})
    counts = {name: int(np.count_nonzero(status == code)) for name, code in STATUS_CODES.items()}
    complete = len(chosen) == prod(spec.chunk_grid)
    report = {
        "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
        "execution_state": "finished", "operation": "prospective_xor_parity_recovery",
        "complete": complete, "outcome": "complete" if complete else "partial",
        "source": {"path": str(source), "sha256_before": source_hash,
                   "sha256_after": source_hash, "size_bytes": source.stat().st_size},
        "dataset": {**_schema(spec), "chunk_grid": list(spec.chunk_grid)},
        "manifest": {"path": str(manifest_path), "sha256": manifest_hash},
        "baseline": {"path": str(baseline_pin.path), "sha256": baseline_hash,
                     "captured_source_sha256": baseline["source_sha256"]},
        "parity": {"path": str(parity_pin.path), "sha256": parity_pin.sha256,
                   "stripe_width": document["stripe_width"], "stripes": len(stripes)},
        "counts": counts, "reconstructed_from_parity": reconstructed,
        "mappings": mappings, "unresolved_chunks": unresolved,
        "trust_note": (
            "A prior SHA-256 match verifies equality to operator supplied capture bytes only. "
            "The capture date and scientific validity require independent provenance. "
            "One unknown chunk can be restored per stripe when all companions match prior hashes."
        ),
        "metadata_note": (
            "Output holds one selected dataset, bounded scalar attributes, and chunk validity. "
            "Other links, scales, siblings, and scientific context are not copied."
        ),
    }
    report_text = _serialized(report)
    if len(report_text) > MAX_REPORT_BYTES:
        raise UnsupportedCase("parity report exceeds publication size limit")

    with tempfile.TemporaryDirectory(prefix=".h5reclaim-parity-", dir=output.parent) as out_dir:
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-parity-", dir=report_path.parent) as rep_dir:
            output_temp, report_temp = Path(out_dir) / "output.h5", Path(rep_dir) / "report.json"
            published = False
            try:
                with h5py.File(output_temp, "x") as file:
                    data = file.create_dataset(spec.path, shape=spec.shape,
                                               maxshape=spec.maxshape or spec.shape,
                                               chunks=spec.chunks, dtype=spec.dtype, fillvalue=0)
                    for origin, (payload, _mapping) in sorted(chosen.items()):
                        data.id.write_direct_chunk(origin, payload, filter_mask=0)
                        mask, roundtrip = data.id.read_direct_chunk(origin)
                        if mask or roundtrip != payload:
                            raise RecoveryError("parity output chunk differs from accepted nominal bytes")
                    for name, value in spec.attributes:
                        data.attrs[name] = value
                    meta = file.create_group("/_h5reclaim")
                    validity = meta.create_dataset("chunk_status", data=status, dtype="u1")
                    validity.attrs["codes_json"] = json.dumps(STATUS_CODES, sort_keys=True)
                    meta.create_dataset("report_json", data=report_text.decode("utf-8"),
                                        dtype=h5py.string_dtype(encoding="utf-8"))
                    data.attrs["h5reclaim_chunk_status"] = "/_h5reclaim/chunk_status"
                    data.attrs["h5reclaim_complete"] = complete
                    data.attrs["h5reclaim_warning"] = (
                        "Check chunk_status before using values; output fill at unknown coordinates "
                        "is not an accepted measurement. Capture provenance requires review."
                    )
                    meta.attrs["source_sha256"] = source_hash
                    meta.attrs["report_schema_version"] = 1
                report_temp.write_bytes(report_text)
                _verify_source(source, damaged.source_identity, source_hash)
                _verify_source(baseline_pin.path, baseline_identity, baseline_hash)
                _verify_source(parity_pin.path, parity_identity, parity_pin.sha256)
                _verify_source(manifest_path, manifest_identity, manifest_hash)
                _validate_paths(source, output, report_path)
                _input_aliases(inputs, output, report_path)
                os.link(output_temp, output)
                published = True
                os.link(report_temp, report_path)
                return report
            except Exception:
                if published:
                    output.unlink(missing_ok=True)
                raise
