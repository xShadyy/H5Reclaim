"""Controlled v0.9 trials on the pinned, authentic GWOSC 4 kHz strain file.

The public capture commands read the intact scientific source once, before the
controlled incident. Public rescue sees only a damaged copy and, where needed,
independently SHA-256-pinned prospective sidecars. The evaluator alone reads
the original values afterward. These selected faults are not a field sample.
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

from h5reclaim.format import H5File  # noqa: E402


SOURCE_ID = "gwosc_gw150914_h1_strain"
FILENAME = "H-H1_GWOSC_4KHZ_R1-1126259447-32.hdf5"
SHA256 = "c5ea87beced5094b56b4d694f3a36e6d59f74d3a46e546b980a6a199d1f252a9"
DATASET = "/strain/Strain"
SHAPE = (131072,)
CHUNKS = (2048,)
CHUNK_COUNT = 64


class TrialError(RuntimeError):
    """A pinned input, mutation precondition, or recovery claim was invalid."""


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            value.update(block)
    return value.hexdigest()


def _source() -> tuple[Path, dict[str, Any]]:
    entries = json.loads((ROOT / "corpus" / "manifest.json").read_text(encoding="utf-8"))["entries"]
    matches = [item for item in entries if item.get("id") == SOURCE_ID]
    if len(matches) != 1:
        raise TrialError("pinned authentic corpus entry is absent or duplicated")
    entry = matches[0]
    path = ROOT / "corpus" / "files" / FILENAME
    if (entry.get("path") != "files/" + FILENAME or entry.get("sha256") != SHA256
            or entry.get("representative_dataset") != DATASET or not path.is_file()
            or path.stat().st_size != entry.get("size_bytes") or digest(path) != SHA256):
        raise TrialError("authentic GWOSC source differs from its independent corpus pin")
    return path, entry


def _layout(source: Path) -> dict[str, Any]:
    with h5py.File(source, "r") as file, H5File(source) as raw:
        dataset = file[DATASET]
        if (dataset.shape != SHAPE or dataset.chunks != CHUNKS or dataset.dtype.str != "<f8"
                or dataset.id.get_num_chunks() != CHUNK_COUNT or raw.superblock.version != 0
                or raw.superblock.base_address != 0 or raw.superblock.offset_size != 8):
            raise TrialError("authentic strain dataset or superblock layout changed")
        creation = dataset.id.get_create_plist()
        if [creation.get_filter(i)[0] for i in range(creation.get_nfilters())] != [3, 1]:
            raise TrialError("authentic GWOSC filter pipeline changed")
        header = int(h5py.h5o.get_info(dataset.id).addr)
        parsed = raw.read_dataset_layout(header, rank=1)
        root = raw.read_tree(parsed.root_address, rank=1, element_size=8)
        if root.level != 0 or len(root.entries) != CHUNK_COUNT:
            raise TrialError("authentic GWOSC direct B-tree differs")
        first = root.entries[0]
        if first.address is None or first.key.offsets != (0, 0):
            raise TrialError("authentic first B-tree chunk link differs")
        root_address_offset = raw.superblock.signature_offset + 24 + 5 * raw.superblock.offset_size
        expected_root = int(h5py.h5o.get_info(file["/"].id).addr)
        if int.from_bytes(source.read_bytes()[root_address_offset:root_address_offset + 8], "little") != expected_root:
            raise TrialError("root object address is not independently corroborated")
        first_chunks = []
        for index in (0, 2):
            info = dataset.id.get_chunk_info(index)
            if (info.chunk_offset != (index * CHUNKS[0],) or info.filter_mask != 0
                    or info.byte_offset is None or info.size < 16):
                raise TrialError("compressed chunk extent is not independently corroborated")
            first_chunks.append(int(info.byte_offset))
        last = dataset.id.get_chunk_info(CHUNK_COUNT - 1)
        if last.byte_offset + last.size != source.stat().st_size:
            raise TrialError("last indexed payload does not end at physical EOF")
        return {"root_address_offset": root_address_offset, "root_address": expected_root,
                "header_offset": raw.absolute(header),
                "index_pointer_offset": first.pointer_offset, "index_pointer": first.address,
                "payload_offsets": first_chunks, "last_chunk_start": int(last.byte_offset)}


def _command(folder: Path, python: str, *args: object) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    try:
        return subprocess.run(
            [python, "-m", "h5reclaim", *map(str, args)], cwd=folder, env=env,
            text=True, capture_output=True, check=False, timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise TrialError("public H5Reclaim command exceeded 120 seconds") from exc


def _success(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode:
        raise TrialError(f"{label} exited {result.returncode}: {(result.stderr or result.stdout)[-1200:]}")


def _mutated_copy(source: Path, destination: Path, changes: list[tuple[int, bytes, bytes]]) -> str:
    shutil.copyfile(source, destination)
    if digest(destination) != SHA256:
        raise TrialError("trial input is not initially identical to the authentic original")
    intervals: list[tuple[int, int]] = []
    with destination.open("r+b") as stream:
        for offset, before, after in changes:
            if (not before or len(before) != len(after) or before == after
                    or offset < 0 or offset + len(before) > destination.stat().st_size
                    or any(offset < end and start < offset + len(before) for start, end in intervals)):
                raise TrialError("mutation range is invalid or overlaps a prior mutation")
            intervals.append((offset, offset + len(before)))
            stream.seek(offset)
            if stream.read(len(before)) != before:
                raise TrialError("mutated byte precondition differs from the authentic original")
            stream.seek(offset)
            stream.write(after)
    result = digest(destination)
    if result == SHA256 or digest(source) != SHA256:
        raise TrialError("controlled change did not occur or original changed")
    return result


def _trial_copy(source: Path, folder: Path, label: str,
                changes: list[tuple[int, bytes, bytes]]) -> tuple[Path, str]:
    case = folder / label
    case.mkdir()
    damaged = case / "damaged.h5"
    return damaged, _mutated_copy(source, damaged, changes)


def _score(source: Path, damaged: Path, damaged_sha: str, output: Path, report_path: Path,
           *, route: str, unknown: set[int] = frozenset(),
           capsule_sha: str | None = None, baseline_sha: str | None = None,
           erasure_sha: str | None = None) -> dict[str, Any]:
    if digest(damaged) != damaged_sha or digest(source) != SHA256:
        raise TrialError("trial source or damaged copy changed during rescue")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (report.get("operation") != route
            or report.get("source", {}).get("sha256_before") != damaged_sha
            or report.get("source", {}).get("sha256_after") != damaged_sha
            or report.get("source", {}).get("size_bytes") != damaged.stat().st_size):
        raise TrialError("report route or immutable damaged-input identity is incorrect")
    if capsule_sha is not None and (report.get("capsule", {}).get("sha256") != capsule_sha
                                    or report["capsule"].get("captured_source_sha256") != SHA256):
        raise TrialError("capsule report does not bind to pre-incident capture")
    if baseline_sha is not None and (report.get("baseline", {}).get("sha256") != baseline_sha
                                     or report["baseline"].get("captured_source_sha256") != SHA256):
        raise TrialError("erasure baseline report does not bind to pre-incident capture")
    if erasure_sha is not None and report.get("erasure", {}).get("sha256") != erasure_sha:
        raise TrialError("erasure report does not bind to pinned sidecar")
    with h5py.File(source, "r") as reference, h5py.File(output, "r") as result:
        expected, actual = reference[DATASET], result[DATASET]
        if expected.shape != actual.shape or expected.dtype != actual.dtype or expected.chunks != actual.chunks:
            raise TrialError("scientific dtype, shape, or chunking differs")
        raw_report = result["/_h5reclaim/report_json"][()]
        if isinstance(raw_report, bytes):
            raw_report = raw_report.decode("utf-8")
        if json.loads(raw_report) != report:
            raise TrialError("embedded evidence differs from external report")
        status = result["/_h5reclaim/chunk_status"][...]
        if status.shape != (CHUNK_COUNT,) or status.dtype != np.dtype("u1"):
            raise TrialError("validity map is absent or has the wrong extent")
        omitted = {index for index, value in enumerate(status) if int(value) != 1}
        if omitted != set(unknown):
            raise TrialError(f"unknown chunk positions differ: {sorted(omitted)}")
        false_accepted = 0
        for index, code in enumerate(status):
            selection = slice(index * CHUNKS[0], (index + 1) * CHUNKS[0])
            candidate = np.asarray(actual[selection], dtype="<f8")
            if int(code) == 1:
                if candidate.tobytes(order="C") != np.asarray(expected[selection], dtype="<f8").tobytes(order="C"):
                    false_accepted += 1
            elif candidate.tobytes(order="C") != np.zeros(CHUNKS, dtype="<f8").tobytes(order="C"):
                raise TrialError("unknown chunk was presented as nonzero measurement")
        if false_accepted:
            raise TrialError(f"{false_accepted} accepted chunks differ at the float64 bit level")
        if route == "prospective_recovery_capsule":
            element_status = result["/_h5reclaim/element_status"][...]
            if element_status.shape != SHAPE or not np.all(element_status == 1):
                raise TrialError("capsule element validity differs from all accepted chunks")
            for name in expected.attrs:
                if name not in actual.attrs or not np.array_equal(np.asarray(expected.attrs[name]),
                                                                  np.asarray(actual.attrs[name])):
                    raise TrialError(f"captured scientific attribute {name!r} changed")
        if (report.get("complete") is not (not unknown)
                or report.get("outcome") != ("partial" if unknown else "complete")):
            raise TrialError("reported outcome contradicts independently read status")
        if route != "prospective_recovery_capsule":
            counts = report.get("counts", {})
            if counts.get("recovered") != CHUNK_COUNT - len(unknown) or sum(
                count for name, count in counts.items() if name != "recovered"
            ) != len(unknown):
                raise TrialError("reported counts contradict independently read validity")
        return {"total_chunks": CHUNK_COUNT, "exact_accepted_chunks": CHUNK_COUNT - len(unknown),
                "unknown_chunks": len(unknown), "false_accepted_chunks": false_accepted,
                "scientific_float64_bits_compared": (CHUNK_COUNT - len(unknown)) * CHUNKS[0]}


def _rescue(folder: Path, python: str, damaged: Path, *route: object) -> tuple[Path, Path]:
    output, report = damaged.parent / "recovered.h5", damaged.parent / "report.json"
    _success(_command(folder, python, "rescue", damaged, "--dataset", DATASET,
                      *route, "--output", output, "--report", report), damaged.parent.name)
    if not output.is_file() or not report.is_file():
        raise TrialError("successful rescue did not publish output and evidence")
    return output, report


def run(work_dir: Path, *, python: str = sys.executable) -> dict[str, Any]:
    """Return per-case exact scores; keep every disposable input for inspection."""
    source, entry = _source()
    layout = _layout(source)
    if work_dir.exists() and (not work_dir.is_dir() or any(work_dir.iterdir())):
        raise TrialError("work directory must be new or empty")
    work_dir.mkdir(parents=True, exist_ok=True)
    capsule = work_dir / "prospective_capsule.zip"
    _success(_command(work_dir, python, "capture-capsule", source, "--dataset", DATASET,
                      "--output", capsule), "authentic capsule capture")
    capsule_sha = digest(capsule)
    baseline = work_dir / "prospective_baseline.json"
    _success(_command(work_dir, python, "capture-baseline", source, "--dataset", DATASET,
                      "--output", baseline), "authentic baseline capture")
    baseline_sha = digest(baseline)
    erasure = work_dir / "prospective_erasure.zip"
    _success(_command(work_dir, python, "capture-erasure", source, "--dataset", DATASET,
                      "--baseline", baseline, "--stripe-width", 4,
                      "--parity-shards", 2, "--output", erasure), "authentic erasure capture")
    erasure_sha = digest(erasure)
    if digest(source) != SHA256:
        raise TrialError("prospective captures modified the authentic original")

    original = source.read_bytes()
    width = 8
    root_offset = layout["root_address_offset"]
    header_offset = layout["header_offset"]
    index_offset = layout["index_pointer_offset"]
    damages = (
        ("root_link_lost", "root group object-header pointer replaced", root_offset,
         layout["root_address"].to_bytes(width, "little"), b"\xff" * width),
        ("selected_header_damaged", "selected object-header version byte changed", header_offset,
         b"\x01", b"\x00"),
        ("chunk_index_link_lost", "first chunk index pointer replaced", index_offset,
         layout["index_pointer"].to_bytes(width, "little"), b"\xff" * width),
    )
    cases = []
    for name, fault, offset, before, after in damages:
        damaged, damaged_sha = _trial_copy(source, work_dir, name, [(offset, before, after)])
        output, report = _rescue(work_dir, python, damaged, "--capsule", capsule,
                                 "--capsule-sha256", capsule_sha)
        scored = _score(source, damaged, damaged_sha, output, report,
                        route="prospective_recovery_capsule", capsule_sha=capsule_sha)
        cases.append({"case": name, "damage": fault, "changed_offsets": [offset],
                      "damaged_sha256": damaged_sha, "route": "prospective_recovery_capsule",
                      "evaluation": scored})

    name = "two_chunk_erasure"
    mutations = []
    for offset in layout["payload_offsets"]:
        before = original[offset:offset + 1]
        if before != b"\x78":
            raise TrialError("selected compressed chunks have unexpected zlib headers")
        mutations.append((offset, before, b"\x00"))
    damaged, damaged_sha = _trial_copy(source, work_dir, name, mutations)
    manifest = damaged.parent / "pinned_inputs.json"
    manifest.write_text(json.dumps({
        "schema_version": 1, "damaged_sha256": damaged_sha,
        "baseline": {"path": str(baseline.resolve()), "sha256": baseline_sha},
        "erasure": {"path": str(erasure.resolve()), "sha256": erasure_sha},
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output, report_path = _rescue(work_dir, python, damaged, "--erasure", manifest)
    scored = _score(source, damaged, damaged_sha, output, report_path,
                    route="prospective_multi_erasure_recovery",
                    baseline_sha=baseline_sha, erasure_sha=erasure_sha)
    erasure_report = json.loads(report_path.read_text(encoding="utf-8"))
    recovered_origins = {tuple(item["coordinate"]) for item in erasure_report.get("mappings", [])
                         if item.get("source_id") == "erasure_parity"}
    if erasure_report.get("reconstructed_from_erasure") != 2 or recovered_origins != {(0,), (4096,)}:
        raise TrialError("both selected chunks were not explicitly rebuilt from pinned erasure shards")
    cases.append({"case": name, "damage": "two compressed payload headers changed in one stripe",
                  "changed_offsets": layout["payload_offsets"], "damaged_sha256": damaged_sha,
                  "route": "prospective_multi_erasure_recovery", "evaluation": scored,
                  "reconstructed_chunks": 2})

    name = "tail_cut"
    cut = 100
    if source.stat().st_size - cut <= layout["last_chunk_start"]:
        raise TrialError("tail cut would remove more than part of the final chunk")
    tail_dir = work_dir / name
    tail_dir.mkdir()
    damaged = tail_dir / "damaged.h5"
    shutil.copyfile(source, damaged)
    with damaged.open("r+b") as stream:
        stream.truncate(source.stat().st_size - cut)
    damaged_sha = digest(damaged)
    output, report = _rescue(work_dir, python, damaged, "--truncated-chunks")
    scored = _score(source, damaged, damaged_sha, output, report,
                    route="truncated_chunked_partial_export", unknown={CHUNK_COUNT - 1})
    cases.append({"case": name, "damage": "physical tail cut by 100 bytes within final payload",
                  "changed_offsets": [damaged.stat().st_size], "damaged_sha256": damaged_sha,
                  "route": "truncated_chunked_partial_export", "evaluation": scored})

    # A separately pinned prospective capture cannot be silently substituted.
    tampered = work_dir / "tampered_capsule.zip"
    data = bytearray(capsule.read_bytes())
    data[0] ^= 1
    tampered.write_bytes(data)
    refused_out = work_dir / "must_not_publish.h5"
    refused_report = work_dir / "must_not_publish.json"
    negative = _command(work_dir, python, "rescue", work_dir / "root_link_lost" / "damaged.h5",
                        "--dataset", DATASET, "--capsule", tampered,
                        "--capsule-sha256", capsule_sha, "--output", refused_out,
                        "--report", refused_report)
    if negative.returncode != 2 or refused_out.exists() or refused_report.exists():
        raise TrialError("changed capsule bypassed the retained SHA-256 pin or published output")
    if digest(source) != SHA256 or digest(capsule) != capsule_sha or digest(baseline) != baseline_sha \
            or digest(erasure) != erasure_sha:
        raise TrialError("authentic reference or independently retained sidecars changed")
    summary = {
        "kind": "authentic_scientific_v09_controlled_trials",
        "source_id": SOURCE_ID, "original_sha256": SHA256, "original_size_bytes": entry["size_bytes"],
        "dataset": DATASET, "prospective_pins": {"capsule_sha256": capsule_sha,
                                        "baseline_sha256": baseline_sha,
                                        "erasure_sha256": erasure_sha},
        "controlled_cases": len(cases), "cases": cases,
        "tampered_capsule_pin_refused": True,
        "original_not_supplied_to_rescue": True,
        "all_cases_passed": True, "work_dir": str(work_dir),
        "limits": ("Five selected, controlled damage cases in one authentic GWOSC file, originally intact. "
                   "The evaluator compares against the original only after rescue; the capsule, baseline, and "
                   "erasure sidecar were captured prospectively before the trial faults. No organically damaged "
                   "file or representative field incident rate is measured. Without retained sidecars, lost "
                   "unique bytes cannot be reconstructed by the capsule or parity routes."),
    }
    (work_dir / "v09_trial_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                                                       encoding="utf-8")
    return summary


def render_text(summary: dict[str, Any]) -> str:
    lines = ["H5Reclaim authentic scientific v0.9 trials", "=" * 43,
             f"Original: pinned GWOSC H1 strain ({summary['dataset']})",
             f"Controlled cases: {summary['controlled_cases']}/{summary['controlled_cases']} passed",
             ""]
    for case in summary["cases"]:
        item = case["evaluation"]
        lines.append(f"  {case['case']}: {item['exact_accepted_chunks']}/{item['total_chunks']} "
                     f"exact chunks, {item['unknown_chunks']} unknown, "
                     f"{item['false_accepted_chunks']} false accepted")
    lines.extend(["", "Tampered capsule refused before publication: yes",
                  "Evaluator-only original never supplied to rescue: yes",
                  "Selected controlled faults in one file do not establish a field success rate.",
                  f"Detailed evidence: {summary['work_dir']}/v09_trial_summary.json"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, help="new or empty directory for trial artifacts")
    parser.add_argument("--python", default=sys.executable, help="Python executable for public CLI calls")
    parser.add_argument("--json", action="store_true", help="print full machine-readable trial evidence")
    args = parser.parse_args()
    folder = args.work_dir or Path(tempfile.mkdtemp(prefix="h5reclaim-authentic-v09-"))
    try:
        result = run(folder, python=args.python)
    except (TrialError, OSError, ValueError, KeyError, RuntimeError) as exc:
        parser.exit(2, f"authentic v0.9 trial failed: {exc}\nwork directory: {folder}\n")
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
