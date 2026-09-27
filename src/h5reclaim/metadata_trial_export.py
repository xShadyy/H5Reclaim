"""Publish a bounded native-readable export after a checked private metadata trial.

The corrected container is disposable. Only a derived selected dataset and a
report are published; the source is unchanged and its historical measurements
are not authenticated by the metadata checksum.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

import h5py

from .metadata_correction import create_layout_pointer_trial, create_root_address_trial
from .metadata import UnsupportedCase
from .readable_export import export_readable
from .recovery import RecoveryError, _identity, _validate_paths, _verify_source, sha256_file


MAX_REPORT_BYTES = 8 << 20


def export_metadata_trial(
    source: str | Path, dataset_path: str, output: str | Path,
    report_path: str | Path, *, kind: str,
    published_output: str | Path | None = None,
) -> dict:
    """Try exactly one declared modern metadata field on a disposable copy."""
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    if kind not in ("root", "layout", "dimension"):
        raise RecoveryError("metadata trial requires 'root', 'layout', or 'dimension'")
    identity = _identity(source.stat())
    original_hash = sha256_file(source)
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-meta-trial-", dir=output.parent) as directory:
        private = Path(directory)
        trial = private / "corrected-private.h5"
        temporary_output = private / "native-output.h5"
        temporary_report = private / "native-report.json"
        if kind == "root":
            evidence = create_root_address_trial(source, dataset_path, trial)
        elif kind == "layout":
            evidence = create_layout_pointer_trial(source, dataset_path, trial)
        else:
            from .header_dimension_trial import create_chunk_dimension_trial
            evidence = create_chunk_dimension_trial(source, dataset_path, trial)
        if evidence.source_sha256 != original_hash:
            raise RecoveryError("metadata trial source changed during correction")
        exported = export_readable(trial, dataset_path, temporary_output, temporary_report)
        if exported.get("mode") != "readable_export":
            raise RecoveryError("metadata trial did not yield a bounded native-readable export")
        report = dict(exported)
        report["mode"] = "metadata_trial_readable_export"
        report["operation"] = "single_metadata_byte_original_checksum_disposable_trial"
        report["source"] = {
            "path": str(source), "size_bytes": source.stat().st_size,
            "sha256_before": original_hash, "sha256_after": original_hash,
        }
        report["metadata_correction"] = asdict(evidence)
        report["output_path"] = str(published_output or output)
        report["limits"] = (
            "One declared metadata byte was changed in a disposable copy only. The original checksum, "
            "rooted graph, modern index, native selected-open, and bounded ownership inventory "
            "were checked. A matching metadata checksum cannot prove historical payload bytes. "
            "No corrected HDF5 container is published."
        )
        encoded = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > MAX_REPORT_BYTES:
            raise UnsupportedCase("metadata trial evidence exceeds 8 MiB")
        with h5py.File(temporary_output, "r+") as file:
            meta = file["/_h5reclaim"]
            del meta["report_json"]
            meta.create_dataset("report_json", data=encoded.decode("utf-8"),
                                dtype=h5py.string_dtype(encoding="utf-8"))
            meta.attrs["source_sha256"] = original_hash
        temporary_report.write_bytes(encoded)
        if sha256_file(trial) != evidence.trial_sha256:
            raise RecoveryError("private corrected trial changed during export")
        _verify_source(source, identity, original_hash)
        _validate_paths(source, output, report_path)
        published = False
        try:
            os.link(temporary_output, output)
            published = True
            os.link(temporary_report, report_path)
        except Exception:
            if published:
                output.unlink(missing_ok=True)
            raise
        return report
