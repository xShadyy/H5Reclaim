"""Verify bundled original research data and report H5Reclaim's current coverage.

This is a format-coverage survey, not a recovery benchmark. The originals are
healthy, and no recovery or corruption is attempted on unsupported layouts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import h5py  # noqa: E402

from h5reclaim.survey import survey  # noqa: E402


DEFAULT_MANIFEST = ROOT / "corpus" / "manifest.json"


class CorpusError(ValueError):
    """The bundled data or manifest cannot be trusted for this run."""


def _source_path(folder: Path, relative: str) -> Path:
    logical = PurePosixPath(relative)
    if logical.is_absolute() or not logical.parts or ".." in logical.parts:
        raise CorpusError(f"unsafe corpus path: {relative!r}")
    path = folder.joinpath(*logical.parts)
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        raise CorpusError(f"corpus path escapes the manifest directory: {relative!r}")
    if not path.is_file():
        raise CorpusError(f"missing original file: {relative}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    """Check exact original bytes before surveying local HDF5 metadata."""
    with manifest_path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    entries = manifest.get("entries")
    if manifest.get("schema_version") != 1 or not isinstance(entries, list) or not entries:
        raise CorpusError("unsupported or empty corpus manifest")

    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    baseline_changes: list[dict[str, Any]] = []
    baseline_unpinned: list[str] = []
    for item in entries:
        identifier = item["id"]
        if identifier in seen:
            raise CorpusError(f"duplicate corpus id: {identifier}")
        seen.add(identifier)
        path = _source_path(manifest_path.parent, item["path"])
        size = path.stat().st_size
        if size != item["size_bytes"]:
            raise CorpusError(f"size changed for {identifier}: {size} bytes")
        digest = _sha256(path)
        if digest != item["sha256"]:
            raise CorpusError(f"SHA-256 changed for {identifier}: {digest}")

        observed = survey(path)
        if observed["outcome"] != "complete":
            raise CorpusError(f"dataset inventory was incomplete for {identifier}")
        datasets = observed["datasets"]
        counts = Counter(row["support"]["status"] for row in datasets)
        representative = next(
            (row for row in datasets if row["selected_path"] == item["representative_dataset"]),
            None,
        )
        if representative is None:
            raise CorpusError(f"representative dataset disappeared for {identifier}")
        selected_support = representative["support"]
        reasons = [reason["code"] for reason in selected_support["reasons"]]
        expected = item["expected_observation"]
        actual_baseline = {
            "dataset_count": len(datasets),
            "candidate_count": counts["candidate"],
            "representative_support": selected_support["status"],
            "representative_reason_codes": reasons,
        }
        if expected is None:
            baseline_unpinned.append(identifier)
        elif actual_baseline != expected:
            baseline_changes.append({
                "id": identifier, "expected": expected, "observed": actual_baseline,
            })
        totals.update(counts)
        results.append({
            "id": identifier,
            "original_bytes_verified": True,
            "sha256": digest,
            "size_bytes": size,
            "survey_outcome": observed["outcome"],
            "dataset_count": len(datasets),
            "support_counts": dict(sorted(counts.items())),
            "representative_dataset": {
                "path": representative["selected_path"],
                "shape": representative.get("shape"),
                "dtype": representative.get("dtype"),
                "layout": representative.get("layout"),
                "chunks": representative.get("chunks"),
                "filters": representative.get("filters"),
                "support": selected_support,
            },
        })

    return {
        "kind": "real_intact_data_coverage_survey",
        "h5py_version": h5py.__version__,
        "hdf5_version": h5py.version.hdf5_version,
        "original_files_verified": len(results),
        "dataset_support_counts": dict(sorted(totals.items())),
        "recovery_evaluation": "not_performed_by_this_survey",
        "baseline_matches_manifest": not baseline_changes,
        "baseline_changes": baseline_changes,
        "baseline_unpinned": baseline_unpinned,
        "files": results,
        "interpretation": (
            "The bundled originals are intact. Survey reports metadata/index coverage only; "
            "it does not recover or validate measurements. No naturally damaged file or "
            "real-world recovery is represented by this corpus."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    try:
        result = run(args.manifest)
    except (CorpusError, OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"corpus verification failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["baseline_matches_manifest"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
