"""Controlled prospective-baseline integrity trials on pinned scientific HDF5.

The public capture commands see the authentic files before damage. The public
rescue commands receive only disposable damaged copies and independently pinned
hash sidecars. An evaluator reads the originals to score exact accepted values.
The declared controlled faults exercise element and chunk integrity checks.
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

from h5reclaim.nonchunked_recovery import read_nonchunked_spec  # noqa: E402
from h5reclaim.recovery import analyze  # noqa: E402


SOURCES = {
    "zenodo_qubit_feedback": {
        "file": "fast_feedback_raw_data.h5",
        "sha256": "dd2a3d48e86ea81094b44439fb58a3d9788757e26799bd4ee7497eb94798ed08",
        "dataset": "/circuit_0/result/hard_measurements/36",
        "shape": (1000, 2), "dtype": "uint8",
    },
    "gwosc_gw150914_h1_strain": {
        "file": "H-H1_GWOSC_4KHZ_R1-1126259447-32.hdf5",
        "sha256": "c5ea87beced5094b56b4d694f3a36e6d59f74d3a46e546b980a6a199d1f252a9",
        "dataset": "/strain/Strain", "shape": (131072,), "dtype": "float64",
    },
}


class IntegrityTrialError(RuntimeError):
    """The input or controlled trial cannot be evaluated honestly."""


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def _original(identifier: str) -> tuple[Path, dict[str, Any]]:
    selected = SOURCES[identifier]
    entries = json.loads((ROOT / "corpus" / "manifest.json").read_text(encoding="utf-8"))["entries"]
    matches = [row for row in entries if row.get("id") == identifier]
    if len(matches) != 1:
        raise IntegrityTrialError(f"pinned corpus entry is missing or duplicate: {identifier}")
    entry = matches[0]
    expected_path = "files/" + selected["file"]
    if (entry.get("path") != expected_path or entry.get("sha256") != selected["sha256"]
            or entry.get("representative_dataset") != selected["dataset"]):
        raise IntegrityTrialError(f"corpus manifest disagrees with trusted {identifier} pin")
    source = ROOT / "corpus" / expected_path
    if (not source.is_file() or source.stat().st_size != entry.get("size_bytes")
            or digest(source) != selected["sha256"]):
        raise IntegrityTrialError(f"authentic {identifier} file differs from pinned bytes")
    with h5py.File(source, "r") as file:
        dataset = file[selected["dataset"]]
        if dataset.shape != selected["shape"] or dataset.dtype != np.dtype(selected["dtype"]):
            raise IntegrityTrialError(f"authentic {identifier} dataset schema differs")
    return source, selected


def _command(folder: Path, python: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    try:
        return subprocess.run(
            [python, "-m", "h5reclaim", *arguments], cwd=folder, env=env,
            capture_output=True, text=True, timeout=90, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise IntegrityTrialError("public H5Reclaim subprocess timed out after 90 seconds") from exc


def _succeeded(completed: subprocess.CompletedProcess[str], label: str) -> None:
    if completed.returncode != 0:
        raise IntegrityTrialError(
            f"{label} exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout)[-1000:]}"
        )


def _mutate(source: Path, copied: Path, offset: int) -> dict[str, Any]:
    shutil.copyfile(source, copied)
    if digest(copied) != digest(source):
        raise IntegrityTrialError("disposable source copy does not match the original")
    with copied.open("r+b") as stream:
        stream.seek(offset)
        before = stream.read(1)
        if len(before) != 1:
            raise IntegrityTrialError("selected payload byte is outside the file")
        stream.seek(offset)
        stream.write(bytes([before[0] ^ 1]))
    if copied.stat().st_size != source.stat().st_size or digest(copied) == digest(source):
        raise IntegrityTrialError("controlled mutation did not change the same-size copy")
    with source.open("rb") as good, copied.open("rb") as damaged:
        differences: list[int] = []
        position = 0
        while clean_block := good.read(1024 * 1024):
            damaged_block = damaged.read(len(clean_block))
            differences.extend(position + index for index, (left, right)
                               in enumerate(zip(clean_block, damaged_block)) if left != right)
            position += len(clean_block)
        if differences != [offset] or damaged.read(1):
            raise IntegrityTrialError("controlled mutation changed more than one selected byte")
    return {"absolute_offset": offset, "before_hex": before.hex(),
            "after_hex": bytes([before[0] ^ 1]).hex()}


def _pin_tamper_check(
    folder: Path, python: str, copied: Path, selected: str,
    baseline: Path, baseline_sha: str, route_flag: str, pin_flag: str,
) -> bool:
    tampered = folder / ("tampered_" + baseline.name)
    raw = bytearray(baseline.read_bytes())
    if not raw:
        raise IntegrityTrialError("captured baseline was empty")
    raw[0] ^= 1
    tampered.write_bytes(raw)
    negative_output, negative_report = folder / "must_not_publish.h5", folder / "must_not_publish.json"
    attempted = _command(
        folder, python, "rescue", str(copied), "--dataset", selected,
        route_flag, str(tampered), pin_flag, baseline_sha,
        "--output", str(negative_output), "--report", str(negative_report),
    )
    return (attempted.returncode == 2 and not negative_output.exists()
            and not negative_report.exists() and "baseline" in attempted.stderr.lower())


def _common_checks(
    original: Path, copied: Path, baseline: Path, baseline_sha: str,
    output: Path, report_path: Path, operation: str, selected: str,
) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("operation") != operation or report.get("dataset", {}).get("path") != selected:
        issues.append("report route or dataset differs from the selected trial")
    if report.get("baseline", {}).get("sha256") != baseline_sha or digest(baseline) != baseline_sha:
        issues.append("report or retained baseline digest differs from the independent pin")
    if report.get("baseline", {}).get("captured_source_sha256") != digest(original):
        issues.append("baseline does not identify the verified authentic capture")
    if (report.get("source", {}).get("sha256_before") != digest(copied)
            or report.get("source", {}).get("sha256_after") != digest(copied)):
        issues.append("report does not bind to the unchanged damaged input")
    with h5py.File(output, "r") as file:
        embedded = file["/_h5reclaim/report_json"][()]
        if isinstance(embedded, bytes):
            embedded = embedded.decode("utf-8")
        if json.loads(embedded) != report:
            issues.append("embedded and external evidence disagree")
    return report, issues


def score_elements(
    original: Path, copied: Path, baseline: Path, baseline_sha: str,
    output: Path, report_path: Path, selected: str, changed_index: tuple[int, int],
) -> dict[str, Any]:
    report, issues = _common_checks(
        original, copied, baseline, baseline_sha, output, report_path,
        "prospective_element_baseline_reconciliation", selected,
    )
    with h5py.File(original, "r") as good, h5py.File(copied, "r") as bad, h5py.File(output, "r") as result:
        expected = good[selected][...]
        native = bad[selected][...]
        output_dataset = result[selected]
        values = output_dataset[...]
        status = result["/_h5reclaim/element_status"][...]
        if (output_dataset.dtype != good[selected].dtype or values.shape != expected.shape
                or status.shape != expected.shape):
            issues.append("output changed scientific dtype, shape, or validity extent")
        if np.count_nonzero(native != expected) != 1 or native[changed_index] == expected[changed_index]:
            issues.append("native HDF5 did not read one silently altered scientific value")
        expected_status = np.ones(expected.shape, dtype="u1")
        expected_status[changed_index] = 0
        if not np.array_equal(status, expected_status):
            issues.append("a changed element was accepted or a known element withheld")
        accepted = status == 1
        false_accepted = int(np.count_nonzero(values[accepted] != expected[accepted]))
        if false_accepted:
            issues.append(f"{false_accepted} accepted element values differ from original bits")
        if values[changed_index] != 0:
            issues.append("unknown output element was not zero-filled")
        if report.get("counts") != {"recovered": expected.size - 1, "unknown": 1}:
            issues.append("reported element counts differ from independent validity check")
        return {"unit": "elements", "total": int(expected.size), "exact_accepted":
                int(np.count_nonzero(accepted)) - false_accepted, "unknown":
                int(np.count_nonzero(~accepted)), "false_accepted": false_accepted,
                "native_plausible_changed_values": int(np.count_nonzero(native != expected)),
                "issues": issues}


def score_chunks(
    original: Path, copied: Path, baseline: Path, baseline_sha: str,
    output: Path, report_path: Path, selected: str, changed_origin: int,
) -> dict[str, Any]:
    report, issues = _common_checks(
        original, copied, baseline, baseline_sha, output, report_path,
        "prospective_chunk_baseline_reconciliation", selected,
    )
    with h5py.File(original, "r") as good, h5py.File(copied, "r") as damaged, \
            h5py.File(output, "r") as result:
        old, new = good[selected], result[selected]
        width = old.chunks[0]
        count = old.shape[0] // width
        native_failed = False
        try:
            native = damaged[selected][changed_origin:changed_origin + width]
        except (OSError, RuntimeError, ValueError):
            native_failed = True
        else:
            if np.asarray(native).tobytes(order="C") == np.asarray(
                    old[changed_origin:changed_origin + width]).tobytes(order="C"):
                issues.append("selected native read was unchanged despite the payload mutation")
        status = result["/_h5reclaim/chunk_status"][...]
        if old.dtype != new.dtype or old.shape != new.shape or status.shape != (count,):
            issues.append("output changed scientific dtype, shape, or chunk validity extent")
        expected_status = np.ones(count, dtype="u1")
        expected_status[changed_origin // width] = status[changed_origin // width]
        if status[changed_origin // width] not in (4, 6) or not np.array_equal(status, expected_status):
            issues.append("corrupt chunk accepted or unchanged chunk withheld")
        false_accepted = 0
        for index, code in enumerate(status):
            start, stop = index * width, (index + 1) * width
            actual = np.asarray(new[start:stop]).tobytes(order="C")
            if int(code) == 1:
                if actual != np.asarray(old[start:stop]).tobytes(order="C"):
                    false_accepted += 1
            elif int(code) != 1 and np.any(new[start:stop]):
                issues.append(f"unknown chunk at {start} was not zero-filled")
        if false_accepted:
            issues.append(f"{false_accepted} accepted chunks differ from original bit patterns")
        counts = report.get("counts", {})
        if (counts.get("recovered") != count - 1
                or sum(value for name, value in counts.items() if name != "recovered") != 1):
            issues.append("reported chunk counts differ from independent validity check")
        return {"unit": "chunks", "total": count, "exact_accepted":
                int(np.count_nonzero(status == 1)) - false_accepted,
                "unknown": int(np.count_nonzero(status != 1)),
                "false_accepted": false_accepted,
                "native_failed_on_changed_chunk": native_failed, "issues": issues}


def _element_case(folder: Path, python: str) -> dict[str, Any]:
    original, spec = _original("zenodo_qubit_feedback")
    selected = spec["dataset"]
    folder.mkdir()
    baseline, copied = folder / "prior_element_hashes.zip", folder / "damaged_copy.h5"
    capture = _command(folder, python, "capture-element-baseline", str(original),
                       "--dataset", selected, "--output", str(baseline))
    _succeeded(capture, "authentic element capture")
    baseline_sha = digest(baseline)
    source_offset = read_nonchunked_spec(original, selected).source_absolute_offset
    with h5py.File(original, "r") as file:
        if source_offset != file[selected].id.get_offset() or file[selected].chunks is not None:
            raise IntegrityTrialError("qubit payload address or layout is not independently corroborated")
    changed_index = (127, 1)
    element_linear = changed_index[0] * spec["shape"][1] + changed_index[1]
    mutation = _mutate(original, copied, source_offset + element_linear)
    damaged_sha = digest(copied)
    output, report_path = folder / "integrity_output.h5", folder / "evidence.json"
    rescue = _command(folder, python, "rescue", str(copied), "--dataset", selected,
                      "--element-baseline", str(baseline),
                      "--element-baseline-sha256", baseline_sha,
                      "--output", str(output), "--report", str(report_path))
    _succeeded(rescue, "authentic element integrity rescue")
    observed = score_elements(original, copied, baseline, baseline_sha, output, report_path,
                              selected, changed_index)
    rejected = _pin_tamper_check(folder, python, copied, selected, baseline, baseline_sha,
                                 "--element-baseline", "--element-baseline-sha256")
    if not rejected:
        observed["issues"].append("altered element baseline passed its original independent SHA-256 pin")
    if digest(original) != spec["sha256"] or digest(copied) != damaged_sha:
        observed["issues"].append("original or damaged input changed during evaluation")
    return {"source_id": "zenodo_qubit_feedback", "dataset": selected,
            "damage": "one unchecksummed contiguous uint8 measurement byte toggled",
            "original_sha256": spec["sha256"], "damaged_sha256": damaged_sha,
            "baseline_sha256": baseline_sha, "mutation": mutation,
            "changed_coordinate": list(changed_index), "tampered_baseline_refused": rejected,
            "original_not_supplied_to_rescue": True, "observed": observed,
            "passed": not observed["issues"]}


def _chunk_case(folder: Path, python: str) -> dict[str, Any]:
    original, spec = _original("gwosc_gw150914_h1_strain")
    selected = spec["dataset"]
    folder.mkdir()
    baseline, copied = folder / "prior_chunk_hashes.json", folder / "damaged_copy.h5"
    capture = _command(folder, python, "capture-baseline", str(original),
                       "--dataset", selected, "--output", str(baseline))
    _succeeded(capture, "authentic chunk capture")
    baseline_sha = digest(baseline)
    parsed = analyze(original, selected)
    with h5py.File(original, "r") as file:
        dataset = file[selected]
        if dataset.chunks != (2048,) or dataset.id.get_num_chunks() != 64:
            raise IntegrityTrialError("authentic GWOSC 4 kHz chunk layout differs")
        target = next(record for record in parsed.records if record.coordinate == (2048,))
        native = dataset.id.get_chunk_info_by_coord(target.coordinate)
        if (native.byte_offset != target.absolute_offset or native.size != target.length
                or target.length <= 16):
            raise IntegrityTrialError("GWOSC raw payload extent disagrees with HDF5")
    mutation = _mutate(original, copied, target.absolute_offset + 8)
    damaged_sha = digest(copied)
    output, report_path = folder / "integrity_output.h5", folder / "evidence.json"
    rescue = _command(folder, python, "rescue", str(copied), "--dataset", selected,
                      "--chunk-baseline", str(baseline),
                      "--chunk-baseline-sha256", baseline_sha,
                      "--output", str(output), "--report", str(report_path))
    _succeeded(rescue, "authentic chunk integrity rescue")
    observed = score_chunks(original, copied, baseline, baseline_sha, output,
                            report_path, selected, 2048)
    rejected = _pin_tamper_check(folder, python, copied, selected, baseline, baseline_sha,
                                 "--chunk-baseline", "--chunk-baseline-sha256")
    if not rejected:
        observed["issues"].append("altered chunk baseline passed its original independent SHA-256 pin")
    if digest(original) != spec["sha256"] or digest(copied) != damaged_sha:
        observed["issues"].append("original or damaged input changed during evaluation")
    return {"source_id": "gwosc_gw150914_h1_strain", "dataset": selected,
            "damage": "one filtered chunk physical payload byte toggled",
            "original_sha256": spec["sha256"], "damaged_sha256": damaged_sha,
            "baseline_sha256": baseline_sha, "mutation": mutation,
            "changed_chunk_origin": 2048, "tampered_baseline_refused": rejected,
            "original_not_supplied_to_rescue": True, "observed": observed,
            "passed": not observed["issues"]}


def run(work_dir: Path, *, python: str = sys.executable) -> dict[str, Any]:
    if work_dir.exists() and (not work_dir.is_dir() or any(work_dir.iterdir())):
        raise IntegrityTrialError("work directory must be new or empty")
    work_dir.mkdir(parents=True, exist_ok=True)
    cases = [_element_case(work_dir / "quantum_elements", python),
             _chunk_case(work_dir / "gwosc_chunks", python)]
    result = {"kind": "authentic_scientific_prospective_baseline_integrity_trial",
              "controlled_cases": len(cases), "all_cases_passed": all(c["passed"] for c in cases),
              "cases": cases, "work_dir": str(work_dir),
              "limits": ("Two declared byte mutations in authentic pinned, originally intact files. "
                         "Capture reads the reference before damage; the evaluator compares exact "
                         "accepted coordinates after rescue. Recovery receives the damaged copy "
                         "and pinned hash sidecar. Baseline hashes provide reference-match evidence; "
                         "exact, unknown and false-accepted outcomes are reported separately.")}
    (work_dir / "integrity_trials.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def render_text(result: dict[str, Any]) -> str:
    lines = ["H5Reclaim authentic scientific integrity trials", "=" * 49,
             f"Result: {'PASS' if result['all_cases_passed'] else 'FAIL'} "
             f"({sum(c['passed'] for c in result['cases'])}/{len(result['cases'])} controlled cases)"]
    for case in result["cases"]:
        observed = case["observed"]
        lines.extend(["", f"{case['source_id']}: {case['damage']}",
                      f"  Exact accepted: {observed['exact_accepted']}/{observed['total']} "
                      f"{observed['unit']}; unknown: {observed['unknown']}; "
                      f"false accepted: {observed['false_accepted']}",
                      "  Altered baseline pin refused: " +
                      ("yes" if case["tampered_baseline_refused"] else "NO")])
        for issue in observed["issues"]:
            lines.append("  ERROR: " + issue)
    lines.extend(["", "Controlled faults on pinned authentic sources, independently scored against originals.",
                  "Baseline hashes identify reference-matching measurements and detected changes.",
                  f"Detailed evidence: {result['work_dir']}/integrity_trials.json"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, help="new or empty directory for disposable trial files")
    parser.add_argument("--python", default=sys.executable, help="Python executable for public CLI calls")
    parser.add_argument("--json", action="store_true", help="print machine-readable trial evaluation")
    args = parser.parse_args()
    folder = args.work_dir or Path(tempfile.mkdtemp(prefix="h5reclaim-authentic-integrity-"))
    try:
        result = run(folder, python=args.python)
    except (IntegrityTrialError, OSError, ValueError, KeyError, RuntimeError) as exc:
        parser.exit(2, f"authentic integrity trial failed: {exc}\nwork directory: {folder}\n")
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render_text(result))
    return 0 if result["all_cases_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
