"""Read-only triage of an HDF5 candidate, including files HDF5 cannot open.

This command reports observable evidence and an appropriate next action. It
does not repair a file, infer a schema, or treat a recognizable signature as
proof that any scientific measurements survive. Dataset value reads and raw
payload scans are intentionally absent.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .recovery import _verify_source, sha256_file, source_snapshot
from .survey import SurveyError, _survey_snapshot


HDF5_SIGNATURE = bytes.fromhex("894844460d0a1a0a")
MAX_CANDIDATE_PATHS = 20
MAX_REASON_COUNTS = 20
MAX_ERROR_CHARS = 300


_QUESTIONS = (
    ("dataset_path", "Which dataset path did the instrument write?",
     "This selects a local object to inspect; an asserted path cannot establish ownership of detached bytes."),
    ("dataset_schema", "What shape, chunk shape, datatype, and filters did the instrument use?",
     "A documented schema can expose a metadata conflict, but matching raw bytes is not proof of origin."),
    ("external_evidence", "Do you have instrument logs, sidecar checksums, or earlier metadata exports?",
     "Independent records may support later validation; this triage does not ingest or authenticate them."),
    ("symptom", "Which operation failed, and what exact error did it show?",
     "The symptom can guide investigation without implying a specific byte-level failure."),
    ("external_or_virtual_dependencies", "Does this dataset use external raw storage or a virtual dataset, and are the referenced files available?",
     "Missing dependent files can look like corruption, while this release does not follow them."),
)


def _questions(condition: str, selection: dict[str, Any] | None) -> list[dict[str, str]]:
    wanted = {"dataset_path", "dataset_schema", "external_evidence", "symptom"}
    if condition == "metadata_inventoried" and selection is not None:
        wanted.discard("dataset_path")
    if condition in ("metadata_unreadable", "inventory_partial") or (
        selection is not None and (
            selection.get("layout") == "virtual" or selection.get("external_storage")
            or any(item["code"] == "external_storage" for item in selection["support"]["reasons"])
        )
    ):
        wanted.add("external_or_virtual_dependencies")
    return [
        {"key": key, "prompt": prompt, "why": why}
        for key, prompt, why in _QUESTIONS if key in wanted
    ]


def _signature(image: Path, size: int) -> dict[str, Any]:
    """Probe only HDF5's documented superblock locations, with tiny reads."""
    with image.open("rb") as handle:
        offset = 0
        while offset + len(HDF5_SIGNATURE) <= size:
            handle.seek(offset)
            if handle.read(len(HDF5_SIGNATURE)) == HDF5_SIGNATURE:
                version = handle.read(1)
                return {
                    "present": True,
                    "byte_offset": offset,
                    # Observed byte, not a validated or supported version.
                    "superblock_version_byte": version[0] if version else None,
                }
            offset = 512 if offset == 0 else offset * 2
    return {"present": False, "byte_offset": None, "superblock_version_byte": None}


def _short_error(error: Exception) -> str:
    # Single-line bounded diagnostic even if an HDF5 driver emits long paths.
    return " ".join(str(error).split())[:MAX_ERROR_CHARS]


def _choose_route(
    inventory: dict[str, Any], dataset_path: str | None,
) -> tuple[str, str, dict[str, Any] | None]:
    if inventory["outcome"] != "complete":
        return (
            "resolve_inventory_issues",
            "The metadata inventory stopped early. No dataset is cleared for recovery by this triage.",
            None,
        )
    datasets = inventory["datasets"]
    selected = None
    if dataset_path is not None:
        selected = next(
            (item for item in datasets if dataset_path in (item["selected_path"], *item["aliases"])),
            None,
        )
        if selected is None:
            return (
                "select_existing_local_dataset",
                "The requested local dataset was not found. A supplied path does not establish data ownership.",
                None,
            )
        if selected["support"]["status"] == "candidate":
            return (
                "inspect_anchored_index",
                "Selected metadata fits a supported version-1 chunk index. Inspect actual chunks before export; this is not a successful recovery claim.",
                selected,
            )
        if selected["support"]["status"] == "indeterminate":
            return (
                "investigate_selected_metadata",
                "Selected metadata could not be classified reliably. Do not assign raw bytes to this dataset by shape alone.",
                selected,
            )
        reason_codes = {reason["code"] for reason in selected["support"]["reasons"]}
        if "external_storage" in reason_codes:
            return (
                "resolve_external_or_virtual_dependencies",
                "The selected dataset refers to external or virtual storage. Identify its dependent files before claiming local data loss; this release does not follow those files.",
                selected,
            )
        if "index_unsupported" in reason_codes:
            return (
                "investigate_index_variant",
                "The selected dataset has a different chunk-index variant or structure. This release cannot reconstruct it; an index-specific strategy needs independent evidence.",
                selected,
            )
        if "file_format_unsupported" in reason_codes:
            return (
                "investigate_format_variant",
                "The file's metadata format is outside this release's raw parser. The recognizable HDF5 signature alone does not make its chunks attributable.",
                selected,
            )
        return (
            "no_supported_recovery_strategy",
            "This release has no justified recovery strategy for the selected layout. Existing readable values may still be exported by ordinary HDF5 tools.",
            selected,
        )
    candidates = [item for item in datasets if item["support"]["status"] == "candidate"]
    if candidates:
        return (
            "select_dataset_for_inspection",
            "Select a local candidate dataset and inspect its chunks. A candidate is a metadata match, not evidence of recovered values.",
            None,
        )
    return (
        "no_supported_recovery_strategy",
        "No local dataset fits this release's recovery conditions. The inventory can still guide format-specific follow-up.",
        None,
    )


def diagnose(source: str | Path, dataset_path: str | None = None) -> dict[str, Any]:
    """Return bounded evidence and an honest strategy decision without reading values.

    A stable private image is examined while the original pathname is checked
    before returning. A damaged file with a surviving HDF5 signature may be
    triaged even if h5py cannot open its metadata. No healthy copy is needed.
    """
    source = Path(source)
    with source_snapshot(source) as (image, digest, identity, size):
        signature = _signature(image, size)
        report: dict[str, Any] = {
            "schema_version": 1,
            "source": {"path": str(source), "size_bytes": size, "sha256": digest},
            "format_signature": signature,
            "dataset_requested": dataset_path,
            "recovery_attempted": False,
            "recovered_values": None,
            "inventory": None,
            "selection": None,
        }
        if not signature["present"]:
            report.update({
                "outcome": "limited",
                "condition": "format_unrecognized",
                "next_action": "identify_source_format",
                "detail": (
                    "No HDF5 signature appears at a documented superblock location. "
                    "This could be a different format or a damaged header; no dataset ownership can be established."
                ),
            })
        else:
            try:
                inventory = _survey_snapshot(image, source, size)
            except (SurveyError, OSError, RuntimeError, ValueError, KeyError, TypeError, OverflowError) as exc:
                report.update({
                    "outcome": "limited",
                    "condition": "metadata_unreadable",
                    "next_action": "investigate_metadata_and_external_records",
                    "detail": (
                        "The HDF5 signature survives, but local metadata could not be inventoried. "
                        "A known instrument schema or previous metadata can guide a future forensic strategy, "
                        "but neither establishes surviving values or coordinates by itself."
                    ),
                    "error": {"code": "hdf5_metadata_unreadable", "detail": _short_error(exc)},
                })
            else:
                counts = Counter(item["support"]["status"] for item in inventory["datasets"])
                candidate_paths = [
                    item["selected_path"] for item in inventory["datasets"]
                    if item["support"]["status"] == "candidate"
                ]
                reasons = Counter(
                    reason["code"] for item in inventory["datasets"]
                    for reason in item["support"]["reasons"]
                )
                report["inventory"] = {
                    "outcome": inventory["outcome"],
                    "dataset_count": inventory["dataset_count"],
                    "support_counts": {
                        status: counts[status]
                        for status in ("candidate", "unsupported", "indeterminate")
                    },
                    "candidate_paths": candidate_paths[:MAX_CANDIDATE_PATHS],
                    "candidate_paths_omitted": max(0, len(candidate_paths) - MAX_CANDIDATE_PATHS),
                    "reason_counts": dict(reasons.most_common(MAX_REASON_COUNTS)),
                    "issues": inventory["issues"],
                    "skipped": inventory["skipped"],
                }
                next_action, detail, selection = _choose_route(inventory, dataset_path)
                report.update({
                    "outcome": "triaged" if inventory["outcome"] == "complete" else "limited",
                    "condition": "metadata_inventoried" if inventory["outcome"] == "complete" else "inventory_partial",
                    "next_action": next_action,
                    "detail": detail,
                })
                if selection is not None:
                    report["selection"] = {
                        key: selection.get(key)
                        for key in ("selected_path", "shape", "dtype", "chunks", "layout", "filters", "index", "support")
                    }
                    report["selection"]["aliases"] = selection["aliases"][:MAX_CANDIDATE_PATHS]
                    report["selection"]["aliases_omitted"] = max(
                        0, len(selection["aliases"]) - MAX_CANDIDATE_PATHS
                    )

        report["questions"] = _questions(report["condition"], report["selection"])

        if sha256_file(image) != digest:
            raise SurveyError("private source snapshot changed during diagnosis")
        _verify_source(source, identity, digest)
        return report
