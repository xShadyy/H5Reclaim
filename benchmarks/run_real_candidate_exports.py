"""Score intact candidate exports against evaluator-only authentic science data.

This tests intact-index export of the selected real layouts for correct value
bits, coordinates and source extents against the unchanged originals.
The recovery subprocess receives only its disposable copy as an input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmarks"))

from run_real_corpus import run as survey_corpus  # noqa: E402


class CandidateError(RuntimeError):
    """Experimental precondition or benchmark artifact is invalid."""


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def score(original: Path, input_copy: Path, output: Path, report_file: Path,
          dataset_path: str) -> dict[str, Any]:
    """Check each accepted coordinate, value bit pattern, and claimed extent."""
    issues: list[str] = []
    report = json.loads(report_file.read_text(encoding="utf-8"))
    if (report.get("source", {}).get("sha256_before") != digest(input_copy)
            or report["source"].get("sha256_after") != digest(input_copy)):
        issues.append("input hash in tool report does not match the unchanged disposable copy")
    if report.get("dataset", {}).get("path") != dataset_path:
        issues.append("selected scientific path was changed")
    with h5py.File(original, "r") as reference, h5py.File(output, "r") as recovered:
        source, selected = reference[dataset_path], recovered[dataset_path]
        if (source.shape, source.dtype, source.chunks) != (selected.shape, selected.dtype, selected.chunks):
            issues.append("scientific shape, datatype, or chunk dimensions changed")
        expected = {tuple(source.id.get_chunk_info(index).chunk_offset):
                    source.id.get_chunk_info(index)
                    for index in range(source.id.get_num_chunks())}
        status = recovered["/_h5reclaim/chunk_status"][...]
        if status.size != len(expected) or not np.all(status == 1):
            issues.append("intact original did not export every allocated chunk as accepted")
        records = report.get("mappings", [])
        seen: set[tuple[int, ...]] = set()
        exact = wrong = 0
        for mapping in records:
            coordinate = tuple(mapping.get("coordinate", ()))
            if coordinate in seen or coordinate not in expected:
                issues.append("mapping has a duplicate or unallocated scientific coordinate")
                continue
            seen.add(coordinate)
            native = expected[coordinate]
            if (mapping.get("source_absolute_offset"), mapping.get("size_bytes")) != (
                    native.byte_offset, native.size):
                issues.append("mapped physical extent differs from authentic HDF5 index")
            region = tuple(slice(position, min(position + width, shape))
                           for position, width, shape in zip(coordinate, source.chunks, source.shape))
            if selected[region].tobytes() == source[region].tobytes():
                exact += 1
            else:
                wrong += 1
        if seen != set(expected):
            issues.append("some allocated source chunks lack a mapping")
        embedded = recovered["/_h5reclaim/report_json"][()]
        if isinstance(embedded, bytes):
            embedded = embedded.decode("utf-8")
        if json.loads(embedded) != report:
            issues.append("embedded and external report disagree")
    return {
        "decision": "output", "source_chunks": len(expected),
        "exact_chunks": exact, "wrong_chunks": wrong,
        "reconstructed_links": report.get("reconstructed_chunks"),
        "reported_operation": report.get("operation"),
        "issues": issues,
    }


def run(work_dir: Path, *, python: str = sys.executable) -> dict[str, Any]:
    if work_dir.exists() and (not work_dir.is_dir() or any(work_dir.iterdir())):
        raise CandidateError("work directory must be new or empty")
    work_dir.mkdir(parents=True, exist_ok=True)
    inventory = survey_corpus()
    if not inventory["baseline_matches_manifest"] or inventory["baseline_unpinned"]:
        raise CandidateError("current format inventory differs from the pinned baseline")
    manifest = json.loads((ROOT / "corpus" / "manifest.json").read_text(encoding="utf-8"))
    by_id = {entry["id"]: entry for entry in manifest["entries"]}
    cases = []
    for entry in inventory["files"]:
        original = ROOT / "corpus" / by_id[entry["id"]]["path"]
        for selected in entry["candidate_paths"]:
            folder = work_dir / f"candidate_{len(cases):03d}"
            folder.mkdir()
            input_copy, output, report_file = (folder / name for name in
                                               ("current_input.h5", "exported.h5", "evidence.json"))
            source_hash = digest(original)
            shutil.copyfile(original, input_copy)
            copy_hash = digest(input_copy)
            if copy_hash != source_hash:
                raise CandidateError("disposable source copy differs from pinned original")
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
            try:
                completed = subprocess.run(
                    [python, "-m", "h5reclaim", "recover", str(input_copy), "--dataset", selected,
                     "--output", str(output), "--report", str(report_file)],
                    cwd=folder, env=environment, capture_output=True, text=True,
                    timeout=90, check=False,
                )
            except subprocess.TimeoutExpired:
                observed: dict[str, Any] = {"decision": "timeout", "exact_chunks": 0,
                                            "wrong_chunks": 0, "issues": ["90-second subprocess timeout"]}
            else:
                if completed.returncode == 0 and output.is_file() and report_file.is_file():
                    observed = score(original, input_copy, output, report_file, selected)
                elif completed.returncode == 2 and not output.exists() and not report_file.exists():
                    observed = {"decision": "refused", "exact_chunks": 0, "wrong_chunks": 0,
                                "issues": [], "reason": (completed.stderr or completed.stdout)[-400:]}
                else:
                    observed = {"decision": "invalid", "exact_chunks": 0, "wrong_chunks": 0,
                                "issues": [f"exit {completed.returncode}; published output={output.exists()}, "
                                           f"report={report_file.exists()}; "
                                           f"{(completed.stderr or completed.stdout)[-300:]}"]}
            if digest(original) != source_hash or digest(input_copy) != copy_hash:
                observed["issues"].append("original or disposable input changed during export")
            cases.append({
                "source_id": entry["id"], "dataset": selected,
                "source_sha256": source_hash, "input_sha256": copy_hash,
                "input": str(input_copy), "observed": observed,
                "passed": (observed["decision"] == "output" and not observed["issues"]
                           and observed["wrong_chunks"] == 0
                           and observed["exact_chunks"] == observed["source_chunks"]
                           and observed["reconstructed_links"] == 0),
            })
    result = {
        "schema_version": 1, "experiment": "intact authentic scientific candidate exports",
        "original_files_verified": inventory["original_files_verified"],
        "dataset_count": sum(item["dataset_count"] for item in inventory["files"]),
        "survey_candidate_count": inventory["dataset_support_counts"].get("candidate", 0),
        "candidate_cases": len(cases),
        "exact_chunks": sum(case["observed"]["exact_chunks"] for case in cases),
        "wrong_chunks": sum(case["observed"]["wrong_chunks"] for case in cases),
        "all_cases_passed": all(case["passed"] for case in cases),
        "cases": cases,
        "limits": (
            "The originals are intact; all outputs are intact-index exports. A candidate is "
            "measured only when its public export passes bitwise/coordinate and source-range "
            "comparison against evaluator-only authentic truth. Per-candidate results record exact "
            "chunks, wrong chunks and export outcomes for the declared scientific datasets."
        ),
    }
    (work_dir / "candidate_exports.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def render(result: dict[str, Any]) -> str:
    lines = [
        "H5Reclaim intact scientific candidate exports",
        f"Verified originals: {result['original_files_verified']} | "
        f"datasets surveyed: {result['dataset_count']} | candidates: {result['survey_candidate_count']}",
        f"Exports: {result['candidate_cases']} | exact chunks: {result['exact_chunks']} | "
        f"wrong chunks: {result['wrong_chunks']} | "
        f"{'PASS' if result['all_cases_passed'] else 'FAIL'}",
    ]
    for case in result["cases"]:
        observed = case["observed"]
        lines.append(f"  {'PASS' if case['passed'] else 'FAIL'} {case['source_id']} "
                     f"{case['dataset']}: {observed['decision']}, "
                     f"{observed['exact_chunks']} exact")
    lines.extend(["", "These originals were healthy; this verifies export, not damage recovery.",
                  "Full independent scoring is saved to candidate_exports.json; add --json to print it."])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, help="new or empty retained evaluation directory")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    folder = args.work_dir or Path(tempfile.mkdtemp(prefix="h5reclaim-candidate-exports-"))
    try:
        result = run(folder, python=args.python)
    except (CandidateError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        parser.exit(2, f"candidate evaluation failed: {exc}\nwork directory: {folder}\n")
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
    print(f"Work directory: {folder}", file=sys.stderr)
    return 0 if result["all_cases_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
