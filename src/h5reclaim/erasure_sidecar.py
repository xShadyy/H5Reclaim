"""Prospective multiple-erasure parity for independently rooted nominal chunks.

This is systematic GF(256) parity, retained apart from a complete prior
coordinate-hash baseline. It can replace up to four bad/missing decoded chunks
per stripe, provided every surviving companion and reconstructed result
matches that earlier baseline. It cannot recover schema or index metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from math import prod
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .gf256 import MAX_PARITY_SHARDS, encode_parity, recover_data
from .metadata import UnsupportedCase
from .output_annotations import add_output_annotations
from .parity_sidecar import _origins, _serialized
from .recovery import (
    MAX_CHUNKS, STATUS_CODES, VERSION, RecoveryError, _identity, _validate_paths,
    _verify_source, analyze, sha256_file,
)
from .replica_recovery import (
    MAX_MANIFEST_BYTES, MAX_REPORT_BYTES, _baseline_chunks, _digest,
    _index_evidence, _input_aliases, _load_json, _pinned, _schema, _unique_pairs,
)


MAX_ARCHIVE_BYTES = 256 << 20
MAX_STRIPE_WIDTH = 16
MAX_NOMINAL_BYTES = 1 << 20


def capture_erasure_sidecar(
    source: str | Path, dataset_path: str, baseline_path: str | Path,
    destination: str | Path, *, stripe_width: int = 4, parity_shards: int = 2,
) -> dict[str, Any]:
    """Capture m parity shards per stripe from a complete verified acquisition."""
    source, baseline_path, destination = Path(source), Path(baseline_path), Path(destination)
    if (type(stripe_width) is not int or not 2 <= stripe_width <= MAX_STRIPE_WIDTH
            or type(parity_shards) is not int or not 2 <= parity_shards <= min(MAX_PARITY_SHARDS, stripe_width)):
        raise RecoveryError("erasure capture needs stripe_width 2..16 and parity_shards 2..min(4,width)")
    if not destination.parent.is_dir() or destination.exists() or destination.is_symlink():
        raise RecoveryError("erasure destination must be a new file in an existing directory")
    baseline, baseline_digest, baseline_identity = _load_json(baseline_path, "baseline")
    _input_aliases([source, baseline_path], destination, destination)
    analysis = analyze(source, dataset_path)
    if not analysis.report["complete"]:
        raise UnsupportedCase("erasure capture requires a complete selected chunk grid")
    original_digest = analysis.report["source"]["sha256_before"]
    if baseline.get("source_sha256") != original_digest:
        raise RecoveryError("baseline was not captured from this intact source")
    hashes = _baseline_chunks(baseline, analysis)
    origins = _origins(analysis.spec)
    records = {record.coordinate: record for record in analysis.records}
    if len(records) != len(origins) or set(records) != set(origins) or set(hashes) != set(origins):
        raise RecoveryError("erasure capture requires unique rooted records and prior hashes")
    size = analysis.spec.chunk_bytes
    if not 0 < size <= MAX_NOMINAL_BYTES:
        raise UnsupportedCase("nominal erasure chunk exceeds 1 MiB")
    stripe_count = (len(origins) + stripe_width - 1) // stripe_width
    if stripe_count * parity_shards * size > MAX_ARCHIVE_BYTES:
        raise UnsupportedCase("erasure shards exceed 256 MiB")
    for origin, record in records.items():
        if len(record.payload) != size or hashlib.sha256(record.payload).hexdigest() != hashes[origin]:
            raise RecoveryError("prior coordinate hash differs from captured chunk")
        _index_evidence(analysis, record)
    stripes = []
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-erasure-", dir=destination.parent) as tmp:
        staged = Path(tmp) / "erasure.zip"
        with zipfile.ZipFile(staged, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for ordinal, start in enumerate(range(0, len(origins), stripe_width)):
                members = origins[start:start + stripe_width]
                payloads = [records[origin].payload for origin in members]
                shards = encode_parity(payloads, parity_shards)
                entries = []
                for row, payload in enumerate(shards):
                    name = f"parity/{ordinal:06d}-{row:02d}.bin"
                    archive.writestr(name, payload, compress_type=zipfile.ZIP_STORED)
                    entries.append({"row": row, "entry": name, "sha256": hashlib.sha256(payload).hexdigest()})
                stripes.append({"ordinal": ordinal, "members": [list(origin) for origin in members],
                                "parity": entries})
            document = {
                "schema_version": 1, "kind": "prospective_gf256_erasure_sidecar",
                "source_sha256": original_digest, "baseline_sha256": baseline_digest,
                "dataset": _schema(analysis.spec), "nominal_chunk_bytes": size,
                "stripe_width": stripe_width, "parity_shards": parity_shards,
                "stripes": stripes,
                "trust_note": "Prior capture hashes, parity, and provenance must be retained independently before damage.",
            }
            manifest_bytes = _serialized(document)
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise UnsupportedCase("erasure manifest exceeds its metadata bound")
            archive.writestr("manifest.json", manifest_bytes, compress_type=zipfile.ZIP_STORED)
        if staged.stat().st_size > MAX_ARCHIVE_BYTES + MAX_MANIFEST_BYTES + 1024 * MAX_CHUNKS:
            raise UnsupportedCase("erasure archive exceeds its size bound")
        _verify_source(source, analysis.source_identity, original_digest)
        _verify_source(baseline_path, baseline_identity, baseline_digest)
        _input_aliases([source, baseline_path], destination, destination)
        if destination.exists() or destination.is_symlink():
            raise RecoveryError("erasure destination appeared during capture")
        os.link(staged, destination)
    return {**document, "archive_sha256": sha256_file(destination)}


def _read_archive(
    path: Path, analysis: Any, baseline_digest: str,
    hashes: dict[tuple[int, ...], str],
) -> tuple[dict[str, Any], list[tuple[list[tuple[int, ...]], tuple[bytes, ...], list[str]]]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or len(names) > MAX_CHUNKS * MAX_PARITY_SHARDS + 1:
                raise RecoveryError("erasure archive has duplicate or excessive entries")
            if "manifest.json" not in names:
                raise RecoveryError("erasure archive has no manifest")
            info = archive.getinfo("manifest.json")
            if info.compress_type != zipfile.ZIP_STORED or info.file_size > MAX_MANIFEST_BYTES:
                raise RecoveryError("erasure archive manifest is compressed or oversized")
            doc = json.loads(archive.read("manifest.json").decode("utf-8"),
                             object_pairs_hook=_unique_pairs)
            required = {"schema_version", "kind", "source_sha256", "baseline_sha256",
                        "dataset", "nominal_chunk_bytes", "stripe_width", "parity_shards",
                        "stripes", "trust_note"}
            if (not isinstance(doc, dict) or set(doc) != required
                    or type(doc["schema_version"]) is not int or doc["schema_version"] != 1
                    or doc["kind"] != "prospective_gf256_erasure_sidecar"):
                raise RecoveryError("unsupported erasure archive manifest")
            _digest(doc["source_sha256"], "erasure source_sha256")
            if (doc["baseline_sha256"] != baseline_digest
                    or doc["dataset"] != _schema(analysis.spec)):
                raise RecoveryError("erasure sidecar conflicts with prior baseline or selected schema")
            size = analysis.spec.chunk_bytes
            width, count = doc["stripe_width"], doc["parity_shards"]
            if (type(doc["nominal_chunk_bytes"]) is not int or doc["nominal_chunk_bytes"] != size
                    or not 0 < size <= MAX_NOMINAL_BYTES or type(width) is not int
                    or not 2 <= width <= MAX_STRIPE_WIDTH or type(count) is not int
                    or not 2 <= count <= min(MAX_PARITY_SHARDS, width)):
                raise RecoveryError("erasure stripe parameters or chunk size disagree")
            origins = _origins(analysis.spec)
            if set(hashes) != set(origins):
                raise RecoveryError("baseline does not cover selected chunk grid")
            expected_stripes = (len(origins) + width - 1) // width
            rows = doc["stripes"]
            if (not isinstance(rows, list) or len(rows) != expected_stripes
                    or len(names) != expected_stripes * count + 1
                    or expected_stripes * count * size > MAX_ARCHIVE_BYTES):
                raise RecoveryError("erasure archive stripe count disagrees with grid")
            checked = []
            for ordinal, row in enumerate(rows):
                members = origins[ordinal * width:(ordinal + 1) * width]
                if (not isinstance(row, dict) or set(row) != {"ordinal", "members", "parity"}
                        or type(row["ordinal"]) is not int or row["ordinal"] != ordinal
                        or row["members"] != [list(origin) for origin in members]
                        or not isinstance(row["parity"], list) or len(row["parity"]) != count):
                    raise RecoveryError("erasure stripe coordinates contradict canonical partition")
                payloads, entries = [], []
                for index, entry in enumerate(row["parity"]):
                    name = f"parity/{ordinal:06d}-{index:02d}.bin"
                    if (not isinstance(entry, dict) or set(entry) != {"row", "entry", "sha256"}
                            or type(entry["row"]) is not int or entry["row"] != index
                            or entry["entry"] != name):
                        raise RecoveryError("erasure parity row contradicts canonical partition")
                    _digest(entry["sha256"], "erasure shard.sha256")
                    if name not in names:
                        raise RecoveryError("erasure archive is missing a shard")
                    part = archive.getinfo(name)
                    if (part.compress_type != zipfile.ZIP_STORED or part.file_size != size
                            or part.compress_size != size or part.is_dir()):
                        raise RecoveryError("erasure shard is compressed or has wrong length")
                    payload = archive.read(name)
                    if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
                        raise RecoveryError("erasure shard disagrees with manifest digest")
                    entries.append(name)
                    payloads.append(payload)
                checked.append((members, tuple(payloads), entries))
            return doc, checked
    except RecoveryError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError, zipfile.BadZipFile) as exc:
        raise RecoveryError(f"erasure archive could not be read safely: {exc}") from exc


def restore_from_erasure(
    source: str | Path, dataset_path: str, manifest: str | Path,
    output: str | Path, report_path: str | Path,
) -> dict[str, Any]:
    """Restore no more than m bad chunks per stripe with exact prior hashes.

    The recovery manifest declares SHA-256-pinned damaged, baseline, and
    erasure inputs. The damaged dataset's current selected metadata and index
    must still be rooted and parseable. The parity alone never locates a chunk.
    """
    source, manifest_path = Path(source), Path(manifest)
    output, report_path = Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    manifest_data, manifest_hash, manifest_identity = _load_json(manifest_path, "erasure recovery manifest")
    if (set(manifest_data) != {"schema_version", "damaged_sha256", "baseline", "erasure"}
            or type(manifest_data["schema_version"]) is not int
            or manifest_data["schema_version"] != 1):
        raise RecoveryError("erasure recovery manifest needs damaged_sha256, baseline, and erasure pins")
    damaged_hash = _digest(manifest_data["damaged_sha256"], "damaged_sha256")
    baseline_pin = _pinned(manifest_data["baseline"], "baseline")
    erasure_pin = _pinned(manifest_data["erasure"], "erasure")
    inputs = [source, manifest_path, baseline_pin.path, erasure_pin.path]
    _input_aliases(inputs, output, report_path)
    if sha256_file(source) != damaged_hash:
        raise RecoveryError("damaged file differs from pinned recovery manifest")
    baseline, baseline_hash, baseline_identity = _load_json(baseline_pin.path, "baseline")
    if baseline_hash != baseline_pin.sha256:
        raise RecoveryError("baseline differs from its independently retained SHA-256")
    archive_info = erasure_pin.path.stat()
    archive_identity = _identity(archive_info)
    if (archive_info.st_size > MAX_ARCHIVE_BYTES + MAX_MANIFEST_BYTES + 1024 * MAX_CHUNKS
            or sha256_file(erasure_pin.path) != erasure_pin.sha256
            or _identity(erasure_pin.path.stat()) != archive_identity):
        raise RecoveryError("erasure sidecar is oversized or differs from its SHA-256 pin")
    damaged = analyze(source, dataset_path)
    if damaged.report["source"]["sha256_before"] != damaged_hash:
        raise RecoveryError("damaged source changed during structural analysis")
    expected = _baseline_chunks(baseline, damaged)
    document, stripes = _read_archive(erasure_pin.path, damaged, baseline_hash, expected)
    if document["source_sha256"] != baseline["source_sha256"]:
        raise RecoveryError("erasure sidecar and baseline refer to different acquisition bytes")
    if (sha256_file(erasure_pin.path) != erasure_pin.sha256
            or _identity(erasure_pin.path.stat()) != archive_identity):
        raise RecoveryError("erasure sidecar changed while it was opened")

    spec = damaged.spec
    records = {record.coordinate: record for record in damaged.records}
    if len(records) != len(damaged.records):
        raise RecoveryError("damaged index assigns one coordinate more than once")
    status = np.full(spec.chunk_grid, STATUS_CODES["allocation_unknown"], dtype="u1")
    chosen: dict[tuple[int, ...], tuple[bytes, dict[str, Any]]] = {}
    unresolved: list[dict[str, Any]] = []
    reconstructed = 0
    for ordinal, (members, parity, entries) in enumerate(stripes):
        known: dict[int, bytes] = {}
        unknown: list[int] = []
        for position, origin in enumerate(members):
            record = records.get(origin)
            if record is not None and hashlib.sha256(record.payload).hexdigest() == expected[origin]:
                known[position] = record.payload
                chosen[origin] = (record.payload, {
                    "coordinate": list(origin), "source_id": "damaged",
                    "expected_capture_decoded_sha256": expected[origin],
                    "evidence": _index_evidence(damaged, record),
                })
            else:
                unknown.append(position)
        rebuilt: dict[int, bytes] = {}
        if unknown and len(unknown) <= len(parity):
            try:
                candidates = recover_data(len(members), known, parity)
            except ValueError:
                candidates = {}
            # A stale or forged parity arrangement cannot partially assign a
            # stripe. Every recovered chunk must agree with its own prior hash.
            if set(candidates) == set(unknown) and all(
                hashlib.sha256(candidates[position]).hexdigest() == expected[members[position]]
                for position in unknown
            ):
                rebuilt = candidates
        for position, payload in rebuilt.items():
            origin = members[position]
            chosen[origin] = (payload, {
                "coordinate": list(origin), "source_id": "erasure_parity",
                "expected_capture_decoded_sha256": expected[origin],
                "parity": {"stripe": ordinal, "shards": entries[:len(unknown)],
                           "companions": [list(members[index]) for index in known]},
            })
            reconstructed += 1
        for position in unknown:
            if position in rebuilt:
                continue
            origin = members[position]
            record = records.get(origin)
            unresolved.append({
                "coordinate": list(origin), "stripe": ordinal,
                "reason": ("more missing chunks than retained parity shards" if len(unknown) > len(parity)
                           else "parity candidate conflicts with prior coordinate hash"),
                "observed_decoded_sha256": hashlib.sha256(record.payload).hexdigest() if record else None,
            })
            indices = tuple(pos // width for pos, width in zip(origin, spec.chunks))
            status[indices] = (STATUS_CODES["decode_failed"] if any(
                row.get("coordinate") == list(origin) for row in damaged.report.get("failed_chunks", ())
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
        "execution_state": "finished", "operation": "prospective_multi_erasure_recovery",
        "complete": complete, "outcome": "complete" if complete else "partial",
        "source": {"path": str(source), "sha256_before": damaged_hash,
                   "sha256_after": damaged_hash, "size_bytes": source.stat().st_size},
        "dataset": {**_schema(spec), "chunk_grid": list(spec.chunk_grid)},
        "manifest": {"path": str(manifest_path), "sha256": manifest_hash},
        "baseline": {"path": str(baseline_pin.path), "sha256": baseline_hash,
                     "captured_source_sha256": baseline["source_sha256"]},
        "erasure": {"path": str(erasure_pin.path), "sha256": erasure_pin.sha256,
                    "stripe_width": document["stripe_width"],
                    "parity_shards": document["parity_shards"], "stripes": len(stripes)},
        "counts": counts, "reconstructed_from_erasure": reconstructed,
        "mappings": mappings, "unresolved_chunks": unresolved,
        "validity": {"dataset": "/_h5reclaim/chunk_status", "codes": STATUS_CODES,
                     "granularity": "one code per selected dataset chunk"},
        "trust_note": (
            "Every accepted survivor and reconstructed chunk agrees with an operator supplied prior "
            "coordinate hash. Capture timing and scientific correctness need independent provenance; "
            "this route needs surviving selected metadata and no more than m losses per stripe."
        ),
    }
    annotation_values = {
        "h5reclaim_chunk_status": "/_h5reclaim/chunk_status",
        "h5reclaim_complete": complete,
        "h5reclaim_warning": "Unknown output fill is not a measurement. Check chunk_status.",
    }
    annotation_collisions = sorted(set(annotation_values) & {
        name for name, _value in spec.attributes
    })
    report["selected_annotation_collisions"] = annotation_collisions
    report_bytes = _serialized(report)
    if len(report_bytes) > MAX_REPORT_BYTES:
        raise UnsupportedCase("erasure report exceeds its publication bound")
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-erasure-", dir=output.parent) as outdir:
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-erasure-", dir=report_path.parent) as repdir:
            staged, staged_report = Path(outdir) / "output.h5", Path(repdir) / "report.json"
            published = False
            try:
                with h5py.File(staged, "x") as file:
                    data = file.create_dataset(spec.path, shape=spec.shape,
                                               maxshape=spec.maxshape or spec.shape,
                                               chunks=spec.chunks, dtype=spec.dtype, fillvalue=0)
                    for origin, (payload, _mapping) in sorted(chosen.items()):
                        data.id.write_direct_chunk(origin, payload, filter_mask=0)
                        mask, roundtrip = data.id.read_direct_chunk(origin)
                        if mask or roundtrip != payload:
                            raise RecoveryError("erasure output changed an accepted nominal chunk")
                    for name, value in spec.attributes:
                        data.attrs[name] = value
                    meta = file.create_group("/_h5reclaim")
                    validity = meta.create_dataset("chunk_status", data=status, dtype="u1")
                    validity.attrs["codes_json"] = json.dumps(STATUS_CODES, sort_keys=True)
                    meta.create_dataset("report_json", data=report_bytes.decode("utf-8"),
                                        dtype=h5py.string_dtype(encoding="utf-8"))
                    if add_output_annotations(data, annotation_values) != annotation_collisions:
                        raise RecoveryError("selected attributes changed during publication")
                    meta.attrs["source_sha256"] = damaged_hash
                staged_report.write_bytes(report_bytes)
                _verify_source(source, damaged.source_identity, damaged_hash)
                _verify_source(manifest_path, manifest_identity, manifest_hash)
                _verify_source(baseline_pin.path, baseline_identity, baseline_hash)
                _verify_source(erasure_pin.path, archive_identity, erasure_pin.sha256)
                _validate_paths(source, output, report_path)
                _input_aliases(inputs, output, report_path)
                os.link(staged, output)
                published = True
                os.link(staged_report, report_path)
                return report
            except Exception:
                if published:
                    output.unlink(missing_ok=True)
                raise
