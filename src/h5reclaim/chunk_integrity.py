"""Prospective per-chunk integrity gate for a damaged file without replicas.

The prior capture supplies a coordinate-specific hash, not missing payload
bytes. An independently rooted, decoded current chunk is exported only if it
matches its prior hash. A mismatch remains unknown even when a native HDF5
reader returns a plausible value from the damaged source.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from itertools import product
from math import prod
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .metadata import UnsupportedCase
from .output_annotations import add_output_annotations
from .recovery import (
    MAX_CHUNKS, STATUS_CODES, VERSION, RecoveryError, _validate_paths,
    _verify_source, analyze, sha256_file,
)
from .replica_recovery import (
    MAX_REPORT_BYTES, _baseline_chunks, _index_evidence, _input_aliases,
    _load_json, _schema,
)


def export_verified_chunks(
    source: str | Path, dataset_path: str, baseline_path: str | Path,
    baseline_sha256: str, output: str | Path, report_path: str | Path,
) -> dict[str, Any]:
    """Keep exact current chunks that match a separately retained prior capture.

    This route detects a changed unchecksummed chunk only when the prior
    baseline was captured before damage and protected independently. It does
    not turn hash digests into replacement measurements.
    """
    source, baseline_path = Path(source), Path(baseline_path)
    output, report_path = Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    _input_aliases([source, baseline_path], output, report_path)
    baseline, baseline_digest, baseline_identity = _load_json(baseline_path, "baseline")
    if baseline_digest != baseline_sha256:
        raise RecoveryError("baseline differs from the retained SHA-256")
    analysis = analyze(source, dataset_path)
    spec = analysis.spec
    source_digest = analysis.report["source"]["sha256_before"]
    if prod(spec.chunk_grid) > MAX_CHUNKS:
        raise UnsupportedCase("prospective baseline comparison exceeds the chunk-grid limit")
    expected = _baseline_chunks(baseline, analysis)
    origins = {
        tuple(index * width for index, width in zip(indices, spec.chunks))
        for indices in product(*(range(length) for length in spec.chunk_grid))
    }
    if set(expected) != origins:
        raise RecoveryError("prior baseline does not cover every selected chunk coordinate")
    records = {record.coordinate: record for record in analysis.records}
    if len(records) != len(analysis.records):
        raise RecoveryError("damaged file has duplicate coordinate assignments")
    status = analysis.status.copy()
    accepted = []
    mappings = []
    unresolved = []
    for indices in product(*(range(length) for length in spec.chunk_grid)):
        origin = tuple(index * width for index, width in zip(indices, spec.chunks))
        record = records.get(origin)
        if record is None:
            unresolved.append({
                "coordinate": list(origin), "chunk_index": list(indices),
                "reason": "no structurally decoded current chunk",
                "existing_status": next(name for name, code in STATUS_CODES.items()
                                        if code == status[indices]),
            })
            continue
        digest = hashlib.sha256(record.payload).hexdigest()
        if digest != expected[origin]:
            status[indices] = STATUS_CODES["unavailable"]
            unresolved.append({
                "coordinate": list(origin), "chunk_index": list(indices),
                "reason": "current decoded payload differs from prior coordinate hash",
                "current_decoded_sha256": digest,
                "expected_capture_decoded_sha256": expected[origin],
            })
            continue
        evidence = _index_evidence(analysis, record)
        accepted.append(record)
        mappings.append({
            "coordinate": list(origin), "chunk_index": list(indices),
            "source_absolute_offset": record.absolute_offset,
            "size_bytes": record.length, "filter_mask": record.filter_mask,
            "current_decoded_sha256": digest,
            "expected_capture_decoded_sha256": expected[origin],
            "integrity": "matches_operator_supplied_prior_capture_sha256",
            "evidence": evidence,
        })
    counts = {name: int(np.count_nonzero(status == code)) for name, code in STATUS_CODES.items()}
    complete = len(accepted) == len(origins)
    report: dict[str, Any] = {
        "schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
        "operation": "prospective_chunk_baseline_reconciliation",
        "execution_state": "finished", "outcome": "complete" if complete else "partial",
        "complete": complete,
        "source": {"path": str(source), "size_bytes": source.stat().st_size,
                   "sha256_before": source_digest, "sha256_after": source_digest},
        "dataset": {**_schema(spec), "chunk_grid": list(spec.chunk_grid),
                    "attributes_copied": [name for name, _value in spec.attributes],
                    "attributes_omitted": list(spec.omitted_attributes)},
        "baseline": {"path": str(baseline_path), "sha256": baseline_digest,
                     "captured_source_sha256": baseline["source_sha256"],
                     "recorded_chunk_hashes": len(expected)},
        "counts": counts, "mappings": mappings, "unresolved_chunks": unresolved,
        "original_structural_outcome": analysis.report["outcome"],
        "original_structural_failed_chunks": analysis.report.get("failed_chunks", []),
        "structural_evidence": {
            "index": analysis.report.get("index"),
            "metadata_resolution": analysis.report.get("metadata_resolution"),
            "ownership_inventory": analysis.report.get("ownership_inventory"),
            "unresolved_links": analysis.report.get("unresolved_links", []),
            "reconstructed_chunks_before_baseline_comparison":
                analysis.report.get("reconstructed_chunks", 0),
            "assumptions": analysis.report.get("assumptions", []),
        },
        "validity": {"dataset": "/_h5reclaim/chunk_status", "codes": STATUS_CODES,
                     "granularity": "one code per selected dataset chunk"},
        "trust_note": (
            "Each accepted current chunk has its own rooted index and decoded payload evidence "
            "and matches a coordinate SHA-256 from an operator supplied prior capture. "
            "The capture date and scientific correctness cannot be authenticated by this file. "
            "Hashes detect differences; they cannot reconstruct unmatched or missing chunks."
        ),
        "metadata_note": (
            "Output holds one selected numeric dataset, bounded supported scalar attributes, "
            "and chunk validity; other groups, dimension scales and context are not copied."
        ),
    }
    annotation_values = {
        "h5reclaim_chunk_status": "/_h5reclaim/chunk_status",
        "h5reclaim_complete": complete,
        "h5reclaim_warning": (
            "Unknown output chunks read as zero but are not known measurements. "
            "Check the chunk status and retained baseline evidence."
        ),
    }
    annotation_collisions = sorted(set(annotation_values) & {
        name for name, _value in spec.attributes
    })
    report["selected_annotation_collisions"] = annotation_collisions
    report_bytes = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(report_bytes) > MAX_REPORT_BYTES:
        raise UnsupportedCase("prospective baseline report exceeds publication limit")
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-chunk-integrity-", dir=output.parent) as out_tmp:
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-chunk-integrity-", dir=report_path.parent) as rep_tmp:
            staged = Path(out_tmp) / "output.h5"
            staged_report = Path(rep_tmp) / "report.json"
            published = False
            try:
                with h5py.File(staged, "x") as file:
                    dataset = file.create_dataset(
                        spec.path, shape=spec.shape, maxshape=spec.maxshape or spec.shape,
                        chunks=spec.chunks, dtype=spec.dtype, fillvalue=0,
                    )
                    for record in accepted:
                        dataset.id.write_direct_chunk(record.coordinate, record.payload,
                                                      filter_mask=0)
                        mask, roundtrip = dataset.id.read_direct_chunk(record.coordinate)
                        if mask or roundtrip != record.payload:
                            raise RecoveryError("verified output chunk changed during publication")
                    for name, value in spec.attributes:
                        dataset.attrs[name] = value
                    meta = file.create_group("/_h5reclaim")
                    validity = meta.create_dataset("chunk_status", data=status, dtype="u1")
                    validity.attrs["codes_json"] = json.dumps(STATUS_CODES, sort_keys=True)
                    validity.attrs["axis_meaning"] = ", ".join(
                        f"chunk axis {dimension}" for dimension in range(len(spec.shape))
                    )
                    meta.create_dataset("report_json", data=report_bytes.decode("utf-8"),
                                        dtype=h5py.string_dtype("utf-8"))
                    if add_output_annotations(dataset, annotation_values) != annotation_collisions:
                        raise RecoveryError("selected attributes changed during publication")
                    meta.attrs["source_sha256"] = source_digest
                    meta.attrs["report_schema_version"] = 1
                    file.flush()
                staged_report.write_bytes(report_bytes)
                _verify_source(source, analysis.source_identity, source_digest)
                _verify_source(baseline_path, baseline_identity, baseline_digest)
                _validate_paths(source, output, report_path)
                _input_aliases([source, baseline_path], output, report_path)
                os.link(staged, output)
                published = True
                os.link(staged_report, report_path)
            except Exception:
                if published:
                    output.unlink(missing_ok=True)
                raise
    return report
