"""Capture an independent chunk-hash baseline before a file is damaged.

This is an acquisition-time integrity record, not a way to infer historical
values from a file first seen after corruption. Keep the JSON separately from
the source and pin its hash in any later replica-reconciliation manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .metadata import UnsupportedCase
from .recovery import RecoveryError, _verify_source, analyze


def capture_baseline(source: str | Path, dataset_path: str, destination: str | Path) -> dict[str, Any]:
    """Record hashes of every *allocated* nominal decoded chunk in a healthy capture.

    The source is never written. A complete, checksum-validated structural
    traversal is required so an unknown allocation cannot become an apparent
    expected fill value. The output is written with exclusive creation and no
    existing destination is replaced.
    """
    source, destination = Path(source), Path(destination)
    if not destination.parent.is_dir():
        raise RecoveryError("baseline destination directory does not exist")
    if destination.exists() or destination.is_symlink():
        raise RecoveryError("baseline destination already exists")
    if source.resolve(strict=True) == destination.resolve(strict=False):
        raise RecoveryError("baseline destination aliases the input")
    analysis = analyze(source, dataset_path)
    if not analysis.report["complete"]:
        raise UnsupportedCase("baseline capture requires every selected chunk to be allocated and decoded")
    spec = analysis.spec
    source_digest = analysis.report["source"]["sha256_before"]
    document: dict[str, Any] = {
        "schema_version": 1,
        "kind": "prospective_chunk_hash_baseline",
        "source_sha256": source_digest,
        "dataset_path": spec.path,
        "dtype": spec.dtype,
        "shape": list(spec.shape),
        "chunks": list(spec.chunks),
        "maxshape": list(spec.maxshape or spec.shape),
        "filter_pipeline": [
            {"id": item.id, "flags": item.flags, "values": list(item.values)}
            for item in spec.filter_pipeline
        ],
        "chunk_hashes": [
            {"coordinate": list(record.coordinate), "sha256": hashlib.sha256(record.payload).hexdigest()}
            for record in sorted(analysis.records, key=lambda value: value.index)
        ],
        "integrity_note": (
            "Hashes represent the bytes observed at capture time; recording them does not "
            "prove earlier measurements were correct. Protect this manifest independently."
        ),
    }
    serialized = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-baseline-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "baseline.json"
        staged.write_bytes(serialized)
        _verify_source(source, analysis.source_identity, source_digest)
        if destination.exists() or destination.is_symlink():
            raise RecoveryError("baseline destination already exists")
        os.link(staged, destination)
    return document
