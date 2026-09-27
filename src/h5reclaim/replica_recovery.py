"""Restore selected chunk values from explicitly pinned, independently parsed copies.

This optional route needs a baseline made *before* the investigated damage.
Its per-coordinate decoded SHA-256 values are an operator supplied assertion
about a past capture, not cryptographic proof of when or how it was made.
Each replacement also needs a rooted parser assignment in an independently
hashed HDF5 file. There is no majority vote or signature-based orphan scan.

Only local, canonical, fixed-width chunked datasets supported by ``analyze``
are eligible. A partial replica may be a valid HDF5 file with unallocated
chunks, but an arbitrary byte-range fragment has no independent HDF5 owner
and is outside this route.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from itertools import product
from math import prod
from pathlib import Path
from typing import Any, Mapping

import h5py
import numpy as np

from .metadata import UnsupportedCase
from .recovery import (
    MAX_CHUNKS, STATUS_CODES, VERSION, Analysis, ChunkRecord, RecoveryError,
    _validate_paths, _verify_source, analyze, sha256_file,
)


MAX_REPLICAS = 16
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class _PinnedPath:
    path: Path
    sha256: str


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise RecoveryError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], str, tuple[int, int, int, int, int]]:
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise RecoveryError(f"{label} is missing or exceeds the {MAX_MANIFEST_BYTES}-byte limit")
    from .recovery import _identity

    identity = _identity(path.stat())
    with path.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES or _identity(path.stat()) != identity:
        raise RecoveryError(f"{label} changed while it was read")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError) as exc:
        raise RecoveryError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise RecoveryError(f"{label} must be a JSON object")
    return result, digest, identity


def _pinned(value: object, label: str) -> _PinnedPath:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise RecoveryError(f"{label} requires path and sha256")
    if not isinstance(value["path"], str) or not Path(value["path"]).is_absolute():
        raise RecoveryError(f"{label} must name an absolute path")
    path = Path(value["path"])
    if not path.is_file():
        raise RecoveryError(f"{label} is not a regular file: {path}")
    return _PinnedPath(path, _digest(value["sha256"], f"{label}.sha256"))


def _integers(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 4 or any(type(n) is not int for n in value):
        raise RecoveryError(f"{label} must be a rank-one through rank-four integer array")
    return tuple(value)


def _schema(spec: Any) -> dict[str, Any]:
    if not isinstance(spec.dtype, str):
        raise UnsupportedCase(
            "prior coordinate-hash baseline and parity routes require primitive numeric schema; "
            "fixed-size complex types need their own exact-type sidecar"
        )
    return {
        "path": spec.path, "dtype": spec.dtype, "shape": list(spec.shape),
        "chunks": list(spec.chunks),
        "maxshape": list(spec.maxshape or spec.shape),
        "filter_pipeline": [
            {"id": f.id, "flags": f.flags, "values": list(f.values)}
            for f in spec.filter_pipeline
        ],
    }


def _baseline_chunks(baseline: Mapping[str, Any], analysis: Analysis) -> dict[tuple[int, ...], str]:
    if type(baseline.get("schema_version")) is not int or baseline["schema_version"] != 1:
        raise RecoveryError("unsupported baseline schema_version")
    _digest(baseline.get("source_sha256"), "baseline.source_sha256")
    expected = _schema(analysis.spec)
    for key, value in (("dataset_path", expected["path"]), ("dtype", expected["dtype"]),
                       ("shape", expected["shape"]), ("chunks", expected["chunks"])):
        if baseline.get(key) != value:
            raise RecoveryError(f"baseline {key} contradicts damaged dataset metadata")
    # These keys are emitted by the current capture route. Older baselines
    # with no filter/maxshape description cannot justify this route.
    for key in ("filter_pipeline", "maxshape"):
        if baseline.get(key) != expected[key]:
            raise RecoveryError(f"baseline {key} contradicts or omits dataset metadata")
    rows = baseline.get("chunk_hashes")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CHUNKS:
        raise RecoveryError("baseline needs a bounded, nonempty chunk_hashes list")
    hashes: dict[tuple[int, ...], str] = {}
    for item in rows:
        if not isinstance(item, dict) or set(item) != {"coordinate", "sha256"}:
            raise RecoveryError("baseline chunk requires coordinate and sha256")
        coordinate = _integers(item["coordinate"], "baseline coordinate")
        if len(coordinate) != len(analysis.spec.shape) or any(
            origin < 0 or origin >= length or origin % width
            for origin, width, length in zip(coordinate, analysis.spec.chunks, analysis.spec.shape)
        ):
            raise RecoveryError("baseline has an invalid chunk coordinate")
        if coordinate in hashes:
            raise RecoveryError("baseline repeats a chunk coordinate")
        hashes[coordinate] = _digest(item["sha256"], "baseline chunk sha256")
    return hashes


def _index_evidence(analysis: Analysis, record: ChunkRecord) -> dict[str, Any]:
    """Bind the copied bytes to one accepted proposal in that copy's ledger."""
    ledger = analysis.report.get("evidence_ledger")
    if not isinstance(ledger, dict):
        raise RecoveryError("copy has no independently reconciled index ledger")
    digest = hashlib.sha256(record.payload).hexdigest()
    matches = [p for p in ledger.get("proposals", ()) if
               p.get("coordinate") == list(record.coordinate)
               and p.get("extent", {}).get("offset") == record.absolute_offset
               and p.get("extent", {}).get("length") == record.length
               and p.get("decoded_sha256") == digest]
    decisions = {d.get("proposal_id"): d for d in ledger.get("decisions", ())
                 if d.get("status") == "accepted"}
    if len(matches) != 1 or matches[0].get("proposal_id") not in decisions:
        raise RecoveryError("copy's chunk lacks a unique accepted coordinate and physical extent")
    proposal = matches[0]
    anchors = [item for item in ledger.get("datasets", ())
               if item.get("path") == analysis.spec.path]
    link_items = ledger.get("links", ())
    links = {item["link_id"]: item for item in link_items}
    if (len(anchors) != 1 or len(links) != len(link_items)
            or any(identifier not in links for identifier in proposal["index_link_ids"])):
        raise RecoveryError("copy's accepted chunk has no unique dataset anchor and index path")
    path_links = [links[identifier] for identifier in proposal["index_link_ids"]]
    return {
        "proposal_id": proposal["proposal_id"],
        "dataset_anchor": anchors[0],
        "raw_sha256": proposal["raw_sha256"],
        "decoded_sha256": digest,
        "extent": proposal["extent"],
        "index_link_ids": proposal["index_link_ids"],
        "index_links": path_links,
        "checks": proposal["checks"],
        "checksum": proposal["checksum"],
        "decision": decisions[proposal["proposal_id"]],
        "filter_mask": record.filter_mask,
        "route": record.route,
    }


def _input_aliases(paths: list[Path], output: Path, report: Path) -> None:
    canonical = [path.resolve(strict=True) for path in paths]
    for index, left in enumerate(canonical):
        if any(left.samefile(right) for right in canonical[index + 1:]):
            raise RecoveryError("source, manifest, baseline, and replicas must be distinct files")
    for target in (output, report):
        if target.resolve(strict=False) in canonical:
            raise RecoveryError("destination aliases a pinned input")


def restore_from_replicas(
    source: str | Path, dataset_path: str, manifest: str | Path,
    output: str | Path, report_path: str | Path,
) -> dict[str, Any]:
    """Write a separate validity-mapped HDF5 output using verified replica chunks.

    The manifest JSON has ``schema_version: 1``, ``damaged_sha256``, a
    ``baseline: {path, sha256}``, and one to sixteen ``replicas`` entries of
    ``{path, sha256}``. Every named path is absolute. The baseline is a
    separate capture-baseline JSON. The damaged file and every replica are
    parsed separately, so a replica's raw index, coordinate, checksum, and
    decoded bytes are independently justified. A hash-pinned baseline binds
    the proposed decoded bytes to an earlier operator capture, which must
    itself have been kept trustworthy. A mismatch between replicas leaves
    that coordinate ambiguous; no majority vote occurs.
    """
    source, manifest_path = Path(source), Path(manifest)
    output, report_path = Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    manifest_data, manifest_hash, manifest_identity = _load_json(manifest_path, "replica manifest")
    if set(manifest_data) != {"schema_version", "damaged_sha256", "baseline", "replicas"}:
        raise RecoveryError("replica manifest needs schema_version, damaged_sha256, baseline, replicas")
    if type(manifest_data["schema_version"]) is not int or manifest_data["schema_version"] != 1:
        raise RecoveryError("unsupported replica manifest schema_version")
    source_hash = _digest(manifest_data["damaged_sha256"], "damaged_sha256")
    baseline_pin = _pinned(manifest_data["baseline"], "baseline")
    replica_items = manifest_data["replicas"]
    if not isinstance(replica_items, list) or not 1 <= len(replica_items) <= MAX_REPLICAS:
        raise RecoveryError(f"replicas must contain one to {MAX_REPLICAS} complete HDF5 paths")
    replicas = [_pinned(item, f"replica {index}") for index, item in enumerate(replica_items)]
    inputs = [source, manifest_path, baseline_pin.path, *(pin.path for pin in replicas)]
    _input_aliases(inputs, output, report_path)
    if sha256_file(source) != source_hash:
        raise RecoveryError("damaged file differs from pinned manifest hash")
    baseline, baseline_hash, baseline_identity = _load_json(baseline_pin.path, "baseline")
    if baseline_hash != baseline_pin.sha256:
        raise RecoveryError("baseline differs from pinned manifest hash")
    damaged = analyze(source, dataset_path)
    if damaged.report["source"]["sha256_before"] != source_hash:
        raise RecoveryError("damaged file changed during structural analysis")
    hashes = _baseline_chunks(baseline, damaged)
    spec = damaged.spec
    if prod(spec.chunk_grid) > MAX_CHUNKS:
        raise UnsupportedCase("replica comparison exceeds structural chunk-grid limit")
    candidates: list[tuple[_PinnedPath, Analysis, dict[tuple[int, ...], ChunkRecord]]] = []
    for index, pin in enumerate(replicas):
        if sha256_file(pin.path) != pin.sha256:
            raise RecoveryError(f"replica {index} differs from pinned manifest hash")
        checked = analyze(pin.path, dataset_path)
        if checked.report["source"]["sha256_before"] != pin.sha256:
            raise RecoveryError(f"replica {index} changed during structural analysis")
        if _schema(checked.spec) != _schema(spec):
            raise RecoveryError(f"replica {index} dataset schema contradicts damaged source")
        records = {record.coordinate: record for record in checked.records}
        if len(records) != len(checked.records):
            raise RecoveryError(f"replica {index} has duplicate coordinate evidence")
        candidates.append((pin, checked, records))

    original_records = {record.coordinate: record for record in damaged.records}
    if len(original_records) != len(damaged.records):
        raise RecoveryError("damaged file has duplicate coordinate evidence")
    status = np.full(spec.chunk_grid, STATUS_CODES["allocation_unknown"], dtype="u1")
    chosen: list[tuple[ChunkRecord, Analysis, Path, str]] = []
    mappings: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for chunk_index in product(*(range(length) for length in spec.chunk_grid)):
        origin = tuple(i * width for i, width in zip(chunk_index, spec.chunks))
        expected = hashes.get(origin)
        current = original_records.get(origin)
        proposed = [(index, pin, checked, records[origin])
                    for index, (pin, checked, records) in enumerate(candidates)
                    if origin in records]
        observed = [(index, hashlib.sha256(record.payload).hexdigest())
                    for index, _pin, _checked, record in proposed]
        if expected is None:
            unresolved.append({"coordinate": list(origin), "reason": "no prior baseline checksum"})
            continue
        if len({digest for _index, digest in observed}) > 1:
            status[chunk_index] = STATUS_CODES["ambiguous"]
            unresolved.append({
                "coordinate": list(origin), "reason": "independent replicas disagree",
                "replica_hashes": [{"replica": i, "decoded_sha256": digest} for i, digest in observed],
            })
            continue
        current_digest = hashlib.sha256(current.payload).hexdigest() if current else None
        if current_digest == expected:
            selected, selected_analysis, selected_path, selected_id = current, damaged, source, "damaged"
        else:
            matches = [(index, pin, checked, record) for index, pin, checked, record in proposed
                       if hashlib.sha256(record.payload).hexdigest() == expected]
            if not matches:
                status[chunk_index] = (STATUS_CODES["decode_failed"] if
                                       any(item.get("coordinate") == list(origin) for item in damaged.report.get("failed_chunks", ()))
                                       else STATUS_CODES["unavailable"])
                unresolved.append({
                    "coordinate": list(origin), "reason": "no independently parsed bytes match baseline",
                    "damaged_decoded_sha256": current_digest,
                    "replica_hashes": [{"replica": i, "decoded_sha256": digest} for i, digest in observed],
                })
                continue
            index, pin, selected_analysis, selected = matches[0]
            selected_path, selected_id = pin.path, f"replica:{index}"
        assert selected is not None
        evidence = _index_evidence(selected_analysis, selected)
        status[chunk_index] = STATUS_CODES["recovered"]
        chosen.append((selected, selected_analysis, selected_path, selected_id))
        mappings.append({
            "coordinate": list(origin), "chunk_index": list(chunk_index),
            "source_id": selected_id, "source_sha256": selected_analysis.report["source"]["sha256_before"],
            "source_absolute_offset": selected.absolute_offset, "size_bytes": selected.length,
            "expected_capture_decoded_sha256": expected,
            "damaged_decoded_sha256": current_digest,
            "evidence": evidence,
            "integrity": "matches_operator_supplied_prior_capture_sha256",
        })
    counts = {name: int(np.count_nonzero(status == code)) for name, code in STATUS_CODES.items()}
    complete = len(chosen) == prod(spec.chunk_grid)
    report: dict[str, Any] = {
        "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
        "execution_state": "finished", "operation": "hash_pinned_replica_reconciliation",
        "complete": complete, "outcome": "complete" if complete else "partial",
        "source": {"path": str(source), "size_bytes": source.stat().st_size,
                   "sha256_before": source_hash, "sha256_after": source_hash},
        "dataset": {**_schema(spec), "chunk_grid": list(spec.chunk_grid)},
        "manifest": {"path": str(manifest_path), "sha256": manifest_hash},
        "baseline": {"path": str(baseline_pin.path), "sha256": baseline_hash,
                     "captured_source_sha256": baseline["source_sha256"],
                     "recorded_chunk_hashes": len(hashes)},
        "replicas": [{"source_id": f"replica:{i}", "path": str(pin.path),
                      "sha256": pin.sha256, "size_bytes": pin.path.stat().st_size,
                      "index_type": analysis.report.get("index", {}).get("type")}
                     for i, (pin, analysis, _records) in enumerate(candidates)],
        "counts": counts, "mappings": mappings, "unresolved_chunks": unresolved,
        "replacements_from_replicas": sum(identity.startswith("replica:") for *_, identity in chosen),
        "trust_note": (
            "A matching baseline hash confirms equality to bytes recorded in the operator supplied "
            "capture, not that capture's date, provenance, or scientific correctness. Each selected "
            "current byte range is separately anchored by an HDF5 index and decoded under its filters. "
            "A missing or conflicting replica is not resolved by majority vote."
        ),
        "metadata_note": (
            "Output holds one selected dataset, bounded scalar attributes from the damaged source, "
            "and chunk validity. Other links, scales, siblings, and scientific context are not copied."
        ),
    }
    report_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if len(report_text.encode("utf-8")) > MAX_REPORT_BYTES:
        raise UnsupportedCase("replica report exceeds publication size limit")

    with tempfile.TemporaryDirectory(prefix=".h5reclaim-replica-", dir=output.parent) as out_dir:
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-replica-", dir=report_path.parent) as rep_dir:
            output_temp = Path(out_dir) / "output.h5"
            report_temp = Path(rep_dir) / "report.json"
            published = False
            try:
                with h5py.File(output_temp, "x") as file:
                    data = file.create_dataset(spec.path, shape=spec.shape,
                                               maxshape=spec.maxshape or spec.shape,
                                               chunks=spec.chunks, dtype=spec.dtype, fillvalue=0)
                    for record, _analysis, _path, _identity in chosen:
                        data.id.write_direct_chunk(record.coordinate, record.payload, filter_mask=0)
                        mask, roundtrip = data.id.read_direct_chunk(record.coordinate)
                        if mask or roundtrip != record.payload:
                            raise RecoveryError("replica output chunk differs from selected decoded bytes")
                    for name, value in spec.attributes:
                        data.attrs[name] = value
                    meta = file.create_group("/_h5reclaim")
                    validity = meta.create_dataset("chunk_status", data=status, dtype="u1")
                    validity.attrs["codes_json"] = json.dumps(STATUS_CODES, sort_keys=True)
                    meta.create_dataset("report_json", data=report_text,
                                        dtype=h5py.string_dtype(encoding="utf-8"))
                    data.attrs["h5reclaim_chunk_status"] = "/_h5reclaim/chunk_status"
                    data.attrs["h5reclaim_complete"] = complete
                    data.attrs["h5reclaim_warning"] = (
                        "Check chunk_status before using values; output fill at unknown coordinates "
                        "is not an accepted measurement. Baseline provenance requires operator review."
                    )
                    meta.attrs["source_sha256"] = source_hash
                    meta.attrs["report_schema_version"] = 1
                report_temp.write_text(report_text, encoding="utf-8")
                _verify_source(source, damaged.source_identity, source_hash)
                for pin, checked, _records in candidates:
                    _verify_source(pin.path, checked.source_identity, pin.sha256)
                _verify_source(baseline_pin.path, baseline_identity, baseline_hash)
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
