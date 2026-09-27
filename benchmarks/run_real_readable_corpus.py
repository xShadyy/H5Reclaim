"""Independently score native-readable exports of two authentic science layouts.

These originals have intact metadata and payloads. The subprocess receives a
separate copied input; the evaluator alone reads the original truth. This
demonstrates faithful current-value export, never damaged-index recovery.
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
from math import prod
from pathlib import Path
from typing import Any

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

from run_real_corpus import run as survey_corpus  # noqa: E402


class ReadableCorpusError(RuntimeError):
    """The authentic input or scored output is inconsistent."""


TARGETS = ("zenodo_qubit_feedback", "zenodo_pallas_cloud_aircraft")


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def score(original: Path, input_copy: Path, output: Path, report_file: Path,
          dataset_path: str) -> dict[str, Any]:
    issues: list[str] = []
    report = json.loads(report_file.read_text(encoding="utf-8"))
    observed_hash = digest(input_copy)
    if (report.get("mode") != "readable_export" or report.get("outcome") != "complete"
            or report.get("source", {}).get("sha256_before") != observed_hash
            or report["source"].get("sha256_after") != observed_hash):
        issues.append("report mode, completeness, or input identity is wrong")
    if report.get("dataset", {}).get("path") != dataset_path:
        issues.append("report selected a different scientific dataset")
    with h5py.File(original, "r") as reference, h5py.File(output, "r") as exported:
        old, new = reference[dataset_path], exported[dataset_path]
        if (old.shape != new.shape or old.dtype != new.dtype or old.chunks != new.chunks
                or not old.id.get_type().equal(new.id.get_type())):
            issues.append("output changed exact scientific datatype, shape, or storage layout")
        expected = np.asarray(old[...])
        observed = np.asarray(new[...])
        equal = (expected.dtype == observed.dtype and expected.shape == observed.shape
                 and expected.tobytes(order="C") == observed.tobytes(order="C"))
        old_address = old.id.get_offset()
        span = {"start": old_address, "end_exclusive": old_address + old.nbytes}
        if report.get("dataset", {}).get("source_contiguous_byte_range") != span:
            issues.append("reported source byte extent disagrees with native HDF5 address")
        with input_copy.open("rb") as stream:
            stream.seek(old_address)
            raw = stream.read(old.nbytes)
        if (len(raw) != old.nbytes or report.get("dataset", {}).get("source_contiguous_raw_sha256")
                != hashlib.sha256(raw).hexdigest()):
            issues.append("source raw hash or byte extent differs from copied input")
        if (report.get("accepted_elements") != prod(old.shape)
                or report.get("unknown_elements") != 0
                or report.get("validity_map") is not None):
            issues.append("intact contiguous values were not all accepted")
        embedded = exported["/_h5reclaim/report_json"][()]
        if isinstance(embedded, bytes):
            embedded = embedded.decode("utf-8")
        if json.loads(embedded) != report:
            issues.append("embedded report differs from external evidence")
    return {
        "decision": "output", "elements": prod(expected.shape),
        "record_fields": len(expected.dtype.names or ()),
        "exact_current_values": bool(equal), "issues": issues,
    }


def run(work_dir: Path, *, python: str = sys.executable) -> dict[str, Any]:
    if work_dir.exists() and (not work_dir.is_dir() or any(work_dir.iterdir())):
        raise ReadableCorpusError("work directory must be new or empty")
    work_dir.mkdir(parents=True, exist_ok=True)
    inventory = survey_corpus()
    if not inventory["baseline_matches_manifest"] or inventory["baseline_unpinned"]:
        raise ReadableCorpusError("original scientific corpus differs from pinned observations")
    manifest = json.loads((ROOT / "corpus" / "manifest.json").read_text(encoding="utf-8"))
    entries = {entry["id"]: entry for entry in manifest["entries"]}
    cases: list[dict[str, Any]] = []
    for identifier in TARGETS:
        entry = entries[identifier]
        original = ROOT / "corpus" / entry["path"]
        selected = entry["representative_dataset"]
        folder = work_dir / identifier
        folder.mkdir()
        copied, output, report_file = (
            folder / name for name in ("current_input.h5", "readable.h5", "evidence.json"))
        source_hash = digest(original)
        shutil.copyfile(original, copied)
        if digest(copied) != source_hash:
            raise ReadableCorpusError("disposable copy differs from its authentic original")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        try:
            completed = subprocess.run(
                [python, "-m", "h5reclaim", "export-readable", str(copied),
                 "--dataset", selected, "--output", str(output), "--report", str(report_file)],
                cwd=folder, env=env, capture_output=True, text=True,
                timeout=90, check=False,
            )
        except subprocess.TimeoutExpired:
            observed: dict[str, Any] = {"decision": "timeout", "exact_current_values": False,
                                        "issues": ["90-second subprocess timeout"]}
        else:
            if completed.returncode == 0 and output.is_file() and report_file.is_file():
                observed = score(original, copied, output, report_file, selected)
            elif completed.returncode == 2 and not output.exists() and not report_file.exists():
                observed = {"decision": "refused", "exact_current_values": False,
                            "issues": [], "reason": (completed.stderr or completed.stdout)[-400:]}
            else:
                observed = {"decision": "invalid", "exact_current_values": False,
                            "issues": [f"exit {completed.returncode}; output={output.exists()}, "
                                       f"report={report_file.exists()}; "
                                       f"{(completed.stderr or completed.stdout)[-350:]}"]}
        if digest(original) != source_hash or digest(copied) != source_hash:
            observed["issues"].append("original or copied input changed during trial")
        cases.append({
            "source_id": identifier, "dataset": selected,
            "source_sha256": source_hash, "copied_input_sha256": digest(copied),
            "input": str(copied), "observed": observed,
            "passed": observed["decision"] == "output" and observed["exact_current_values"]
                      and not observed["issues"],
        })
    result = {
        "schema_version": 1, "experiment": "native-readable authentic scientific representatives",
        "original_files_verified": inventory["original_files_verified"],
        "structural_candidates_in_corpus": inventory["dataset_support_counts"].get("candidate", 0),
        "case_count": len(cases), "all_cases_passed": all(case["passed"] for case in cases),
        "cases": cases,
        "limits": (
            "Both originals have intact metadata and currently readable values. The aircraft "
            "compound record type is preserved, but this route relies on native HDF5 and does "
            "not reconstruct a damaged index. The result proves neither historical authenticity "
            "nor any probability of recovery from damaged files."
        ),
    }
    (work_dir / "readable_corpus.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def render(result: dict[str, Any]) -> str:
    lines = [
        "H5Reclaim authentic native-readable export evaluation",
        f"Original hashes verified: {result['original_files_verified']} | "
        f"selected intact exports: {result['case_count']} | "
        f"{'PASS' if result['all_cases_passed'] else 'FAIL'}",
    ]
    for case in result["cases"]:
        observed = case["observed"]
        lines.append(f"  {'PASS' if case['passed'] else 'FAIL'} {case['source_id']} "
                     f"{case['dataset']}: {observed['decision']}, "
                     f"{observed.get('elements', 0)} exact current elements, "
                     f"{observed.get('record_fields', 0)} compound fields")
    lines.extend(["", "Native-readable export of healthy originals is not damage recovery.",
                  "Full scoring is saved to readable_corpus.json; add --json to print it."])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, help="new or empty retained trial directory")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    folder = args.work_dir or Path(tempfile.mkdtemp(prefix="h5reclaim-readable-corpus-"))
    try:
        result = run(folder, python=args.python)
    except (ReadableCorpusError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        parser.exit(2, f"readable corpus evaluation failed: {exc}\nwork directory: {folder}\n")
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
    print(f"Work directory: {folder}", file=sys.stderr)
    return 0 if result["all_cases_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
