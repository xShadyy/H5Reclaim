"""Independent, exact-placement evaluation of the supported broken-link case.

This program owns the pristine reference and mutation log.  It invokes the
recovery CLI in a separate process with only the damaged file and a dataset
path.  Recovery code must never import this program or consult its truth files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import h5py
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tools" / "make_healthy_fixture.py"
DAMAGE_TOOL = ROOT / "tools" / "make_broken_link_fixture.py"
STATUS_NAMES = {
    1: "recovered",
    2: "allocation_unknown",
    3: "ambiguous",
    4: "unavailable",
    5: "unsupported",
    6: "decode_failed",
}


class BenchmarkError(RuntimeError):
    """The experiment could not be evaluated without compromising its rules."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(
    command: list[str], *, timeout: int = 120, env: dict[str, str] | None = None,
    cwd: Path = ROOT,
) -> None:
    try:
        completed = subprocess.run(
            command, cwd=cwd, env=env, text=True, capture_output=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError(f"command exceeded {timeout}s: {command[0:3]}") from exc
    if completed.returncode:
        raise BenchmarkError(
            f"command failed with exit code {completed.returncode}: {command}\n"
            f"stdout:\n{completed.stdout[-4000:]}\n"
            f"stderr:\n{completed.stderr[-4000:]}"
        )


def _native_failures(
    damaged: Path, dataset_path: str, pristine: np.ndarray, chunks: tuple[int, int]
) -> set[tuple[int, int]]:
    """Identify chunks the normal reader fails to return correctly.

    A returned fill value or a misdirected chunk is a failure even when h5py
    raises no exception.  Only this evaluator has the pristine values.
    """
    grid = (pristine.shape[0] // chunks[0], pristine.shape[1] // chunks[1])
    failures: set[tuple[int, int]] = set()
    try:
        with h5py.File(damaged, "r") as handle:
            dataset = handle[dataset_path]
            for i in range(grid[0]):
                for j in range(grid[1]):
                    slices = (
                        slice(i * chunks[0], (i + 1) * chunks[0]),
                        slice(j * chunks[1], (j + 1) * chunks[1]),
                    )
                    try:
                        actual = dataset[slices]
                    except (OSError, RuntimeError, ValueError):
                        failures.add((i, j))
                        continue
                    if not np.array_equal(actual, pristine[slices]):
                        failures.add((i, j))
    except (OSError, KeyError, RuntimeError, ValueError):
        failures = {(i, j) for i in range(grid[0]) for j in range(grid[1])}
    return failures


def evaluate(
    pristine_path: Path, damaged_path: Path, output_path: Path,
    report_path: Path, dataset_path: str = "/measurements",
) -> dict[str, Any]:
    """Score only status-marked measurements at their declared coordinates."""
    with h5py.File(pristine_path, "r") as handle:
        reference = handle[dataset_path]
        pristine = reference[...]
        if reference.chunks is None or len(reference.chunks) != 2:
            raise BenchmarkError("reference must be a rank-two chunked dataset")
        chunks = reference.chunks
        if any(dimension % step for dimension, step in zip(pristine.shape, chunks)):
            raise BenchmarkError("partial edge chunks are outside this benchmark")
    grid = (pristine.shape[0] // chunks[0], pristine.shape[1] // chunks[1])

    with h5py.File(output_path, "r") as handle:
        if dataset_path not in handle or "/_h5reclaim/chunk_status" not in handle:
            raise BenchmarkError("output lacks the recovered dataset or chunk status map")
        result_dataset = handle[dataset_path]
        status_dataset = handle["/_h5reclaim/chunk_status"]
        if result_dataset.shape != pristine.shape or result_dataset.dtype != pristine.dtype:
            raise BenchmarkError("output shape or dtype differs from the reference")
        if status_dataset.shape != grid or status_dataset.dtype != np.dtype("uint8"):
            raise BenchmarkError("status shape or dtype differs from the chunk grid")
        status = status_dataset[...]
        values = result_dataset[...]

    invalid = set(int(code) for code in np.unique(status)) - STATUS_NAMES.keys()
    if invalid:
        raise BenchmarkError(f"status map has undefined codes: {sorted(invalid)}")

    status_counts: Counter[str] = Counter()
    wrong: list[list[int]] = []
    incorrect: list[list[int]] = []
    # Compare full bytes rather than a checksum: a match is called a wrong
    # placement only when exactly one other reference coordinate owns them.
    reference_locations: dict[bytes, list[tuple[int, int]]] = {}
    for i in range(grid[0]):
        for j in range(grid[1]):
            slices = (
                slice(i * chunks[0], (i + 1) * chunks[0]),
                slice(j * chunks[1], (j + 1) * chunks[1]),
            )
            reference_locations.setdefault(pristine[slices].tobytes(), []).append((i, j))
    native_failures = _native_failures(damaged_path, dataset_path, pristine, chunks)
    recovered_native_failures = 0
    for i in range(grid[0]):
        for j in range(grid[1]):
            code = int(status[i, j])
            status_counts[STATUS_NAMES[code]] += 1
            if code == 1:
                if (i, j) in native_failures:
                    recovered_native_failures += 1
                slices = (
                    slice(i * chunks[0], (i + 1) * chunks[0]),
                    slice(j * chunks[1], (j + 1) * chunks[1]),
                )
                actual_bytes = values[slices].tobytes()
                if actual_bytes != pristine[slices].tobytes():
                    matches = reference_locations.get(actual_bytes, [])
                    if len(matches) == 1 and matches[0] != (i, j):
                        wrong.append([i, j])
                    else:
                        incorrect.append([i, j])

    with report_path.open("r", encoding="utf-8") as stream:
        report = json.load(stream)
    if report.get("complete") is not (status_counts["recovered"] == grid[0] * grid[1]):
        raise BenchmarkError("report completeness conflicts with status map")
    if report.get("outcome") not in ("complete", "partial"):
        raise BenchmarkError("report has an unknown outcome")
    if report.get("outcome") == "complete" and not report["complete"]:
        raise BenchmarkError("report outcome conflicts with completeness")
    if report.get("outcome") == "partial" and report["complete"]:
        raise BenchmarkError("report outcome conflicts with completeness")
    report_counts = report.get("counts")
    if not isinstance(report_counts, dict) or not set(STATUS_NAMES.values()) <= report_counts.keys():
        raise BenchmarkError("report lacks machine-readable recovery counts")
    for label in STATUS_NAMES.values():
        if report_counts[label] != status_counts[label]:
            raise BenchmarkError(f"report {label} count conflicts with output status map")
    mappings = report.get("mappings", [])
    if not isinstance(mappings, list):
        raise BenchmarkError("report mappings must be a list")
    reported_source = report.get("source", {})
    damaged_hash = _sha256(damaged_path)
    if (
        reported_source.get("sha256_before") != damaged_hash
        or reported_source.get("sha256_after") != damaged_hash
        or reported_source.get("size_bytes") != damaged_path.stat().st_size
    ):
        raise BenchmarkError("report source identity conflicts with damaged input")
    reported_dataset = report.get("dataset", {})
    if (
        reported_dataset.get("path") != dataset_path
        or reported_dataset.get("shape") != list(pristine.shape)
        or reported_dataset.get("chunks") != list(chunks)
        or reported_dataset.get("chunk_grid") != list(grid)
    ):
        raise BenchmarkError("report dataset metadata conflicts with reference")
    mapped: set[tuple[int, int]] = set()
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise BenchmarkError("mapping must be an object")
        index = mapping.get("chunk_index")
        if (
            not isinstance(index, list) or len(index) != 2
            or not all(type(value) is int for value in index)
            or not (0 <= index[0] < grid[0] and 0 <= index[1] < grid[1])
        ):
            raise BenchmarkError("mapping has an invalid chunk index")
        position = (index[0], index[1])
        if position in mapped:
            raise BenchmarkError("report has duplicate chunk mappings")
        mapped.add(position)
        if mapping.get("coordinate") != [index[0] * chunks[0], index[1] * chunks[1]]:
            raise BenchmarkError("mapping coordinate conflicts with chunk index")
        if mapping.get("route") not in ("intact_tree", "reconstructed_link"):
            raise BenchmarkError("mapping lacks a supported evidence route")
    expected_mapped = {
        (i, j) for i in range(grid[0]) for j in range(grid[1]) if status[i, j] == 1
    }
    if mapped != expected_mapped:
        raise BenchmarkError("report mappings conflict with recovered status cells")

    return {
        "dataset": dataset_path,
        "grid_shape": list(grid),
        "expected_chunks": grid[0] * grid[1],
        "status_counts": {label: status_counts[label] for label in STATUS_NAMES.values()},
        "recovered_chunks": status_counts["recovered"],
        "wrong_placement_chunks": len(wrong),
        "wrong_placement_coordinates": wrong,
        "incorrect_values_chunks": len(incorrect),
        "incorrect_values_coordinates": incorrect,
        "missing_region_chunks": grid[0] * grid[1] - status_counts["recovered"],
        "native_unavailable_chunks": len(native_failures),
        "recovered_from_native_unavailable_chunks": recovered_native_failures,
        "reconstructed_link_mappings": sum(
            mapping.get("route") == "reconstructed_link"
            for mapping in mappings if isinstance(mapping, dict)
        ),
    }


def run_trial(
    work_dir: Path, *, rows: int = 512, cols: int = 512,
    chunk_rows: int = 16, chunk_cols: int = 16,
    dataset_path: str = "/measurements", python: str = sys.executable,
    seed: int | None = None,
) -> dict[str, Any]:
    """Create a new, isolated trial and leave evidence for independent review."""
    work_dir = work_dir.resolve()
    if work_dir.exists() and any(work_dir.iterdir()):
        raise BenchmarkError(f"work directory is not empty: {work_dir}")
    truth = work_dir / "truth"
    inputs = work_dir / "inputs"
    results = work_dir / "results"
    for folder in (truth, inputs, results):
        folder.mkdir(parents=True, exist_ok=False)

    pristine = truth / "pristine.h5"
    manifest = truth / "mutation.json"
    damaged = inputs / "damaged.h5"
    recovered = results / "recovered.h5"
    report = results / "recovery.json"

    _run([
        python, str(GENERATOR), "--output", str(pristine),
        "--rows", str(rows), "--cols", str(cols),
        "--chunk-rows", str(chunk_rows), "--chunk-cols", str(chunk_cols),
    ])
    # The public generator makes the layout.  Secret challenge values are
    # written afterwards and retained only in the benchmark's truth area.
    if seed is None:
        seed = secrets.randbits(64)
    if seed < 0 or seed >= 2**64:
        raise BenchmarkError("challenge seed must fit an unsigned 64-bit integer")
    with h5py.File(pristine, "r+") as handle:
        dataset = handle[dataset_path]
        challenge = np.random.default_rng(seed).integers(
            0, 2**32, size=dataset.shape, dtype=np.uint32
        )
        dataset[...] = challenge
    with h5py.File(pristine, "r") as handle:
        np.testing.assert_array_equal(handle[dataset_path][...], challenge)
    (truth / "challenge.json").write_text(
        json.dumps({"seed": seed, "generator": "numpy.default_rng"}) + "\n",
        encoding="utf-8",
    )
    original_hash = _sha256(pristine)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    _run([
        python, str(DAMAGE_TOOL), "--input", str(pristine),
        "--output", str(damaged), "--manifest", str(manifest),
    ], env=env)
    if _sha256(pristine) != original_hash:
        raise BenchmarkError("damage generation modified the pristine file")
    damaged_hash = _sha256(damaged)
    if damaged_hash == original_hash:
        raise BenchmarkError("damage tool produced an identical file")

    # No pristine path, manifest, generator parameters, or truth data enters
    # this process.  The source tree is needed solely to import the package.
    _run([
        python, "-m", "h5reclaim", "recover", str(damaged),
        "--dataset", dataset_path, "--output", str(recovered),
        "--report", str(report),
    ], env=env, cwd=results)
    if _sha256(damaged) != damaged_hash:
        raise BenchmarkError("recovery modified its source file")
    if _sha256(pristine) != original_hash:
        raise BenchmarkError("recovery modified the pristine reference")

    summary = evaluate(pristine, damaged, recovered, report, dataset_path)
    summary.update({
        "pristine_sha256": original_hash,
        "damaged_sha256": damaged_hash,
        "source_preserved": True,
        "pristine_preserved": True,
        "work_dir": str(work_dir.resolve()),
        "challenge_seed": seed,
    })
    (truth / "evaluation.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def result_exit_code(summary: dict[str, Any]) -> int:
    """Fail a trial for any false value claim or absent recovery gain."""
    return int(
        summary["wrong_placement_chunks"] != 0
        or summary["incorrect_values_chunks"] != 0
        or summary["native_unavailable_chunks"] == 0
        or summary["recovered_from_native_unavailable_chunks"] == 0
    )


def _human_summary(summary: dict[str, Any]) -> str:
    total = summary["expected_chunks"]
    directory = Path(summary["work_dir"])
    result = "PASS" if result_exit_code(summary) == 0 else "FAIL"
    return "\n".join([
        "H5Reclaim | Synthetic controlled recovery trial",
        "=" * 47,
        f"RESULT: {result}",
        "",
        f"Damage: one verified index pointer changed in a separate copy",
        f"Native unreadable or incorrect: {summary['native_unavailable_chunks']}/{total} chunks",
        f"H5Reclaim recovered: {summary['recovered_chunks']}/{total} chunks",
        f"Recovered native failures: {summary['recovered_from_native_unavailable_chunks']}",
        f"Reconstructed links: {summary['reconstructed_link_mappings']} chunk mappings",
        f"Wrong coordinates: {summary['wrong_placement_chunks']}",
        f"Incorrect values: {summary['incorrect_values_chunks']}",
        f"Missing regions: {summary['missing_region_chunks']}",
        "Sources: reference and damaged copy unchanged",
        "",
        f"Recovered data: {directory / 'results' / 'recovered.h5'}",
        f"Recovery report: {directory / 'results' / 'recovery.json'}",
        f"Full evaluation: {directory / 'truth' / 'evaluation.json'}",
        "",
        "Scope: one controlled synthetic fault; no general recovery rate follows.",
        "For machine-readable output, rerun with --json.",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, help="new or empty directory to retain trial files")
    parser.add_argument("--rows", type=int, default=512)
    parser.add_argument("--cols", type=int, default=512)
    parser.add_argument("--chunk-rows", type=int, default=16)
    parser.add_argument("--chunk-cols", type=int, default=16)
    parser.add_argument("--python", default=sys.executable, help="interpreter for child processes")
    parser.add_argument("--seed", type=int, help="challenge seed, recorded only after recovery")
    parser.add_argument("--json", action="store_true", help="print the full machine-readable evaluation")
    args = parser.parse_args()
    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="h5reclaim-trial-"))
    try:
        summary = run_trial(
            work_dir, rows=args.rows, cols=args.cols,
            chunk_rows=args.chunk_rows, chunk_cols=args.chunk_cols,
            python=args.python, seed=args.seed,
        )
    except (BenchmarkError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(2, f"benchmark failed: {exc}\nwork directory: {work_dir}\n")
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else _human_summary(summary))
    return result_exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
