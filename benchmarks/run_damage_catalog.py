"""Stratified controlled damage and refusal trials on bundled scientific HDF5.

The four original files are authenticated against the bundled manifest. This
independent evaluator mutates disposable byte-for-byte copies, invokes the
public recovery CLI with only an input path and dataset path, and checks every
accepted GWOSC chunk against the untouched original at the same coordinate.
Cases without an accepted output must decline without publishing artifacts.

These deterministic cases measure behavior for these exact layouts and damage
classes. They do not estimate a real-world recovery success percentage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import h5py
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
sys.path.insert(0, str(ROOT / "src"))

from run_gwosc_recovery import DATASET, FILENAME, _inspect_original  # noqa: E402
from run_real_corpus import run as survey_corpus  # noqa: E402
from h5reclaim.format import H5File  # noqa: E402


STATUS_NAMES = {
    1: "recovered", 2: "allocation_unknown", 3: "ambiguous",
    4: "unavailable", 5: "unsupported", 6: "decode_failed",
    7: "decoder_unavailable",
}


class CatalogError(RuntimeError):
    """A trial could not be conducted or its observed result was inconsistent."""


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mutated_copy(source: Path, destination: Path, changes: list[tuple[int, bytes, bytes]]) -> str:
    """Apply verified nonoverlapping byte changes; preserve exact file length."""
    original = source.read_bytes()
    changed = bytearray(original)
    seen: set[int] = set()
    for offset, before, after in changes:
        if not before or len(before) != len(after) or offset < 0 or offset + len(before) > len(original):
            raise CatalogError("mutation is not a bounded same-length replacement")
        positions = set(range(offset, offset + len(before)))
        if seen & positions or original[offset:offset + len(before)] != before or before == after:
            raise CatalogError("mutation overlaps another change or fails its byte precondition")
        seen.update(positions)
        changed[offset:offset + len(before)] = after
    with destination.open("xb") as stream:
        stream.write(changed)
    if _hash(source) != hashlib.sha256(original).hexdigest():
        raise CatalogError("original scientific file changed while making trial copies")
    return _hash(destination)


def _pointer_changes(source: Path, details: dict[str, Any]) -> tuple[list[tuple[int, bytes, bytes]], int]:
    """One interior link, and one edge link to form an unrecoverable pair."""
    width = details["pointer_size"]
    interior = details["pointer_offset"]
    with H5File(source) as reader:
        root = reader.read_tree(details["btree_root_address"], rank=1, element_size=8)
        if root.level != 1 or len(root.entries) != 3:
            raise CatalogError("pinned GWOSC root no longer has the expected three leaves")
        edge = root.entries[0]
        if edge.address is None or edge.pointer_offset == interior:
            raise CatalogError("no independent root link for the multiple-damage case")
        edge_value = edge.address
        edge_offset = edge.pointer_offset
    original = source.read_bytes()
    undefined = b"\xff" * width
    for offset, expected in ((interior, details["pointer_value"]), (edge_offset, edge_value)):
        if original[offset:offset + width] != expected.to_bytes(width, "little"):
            raise CatalogError("root pointer differs from the parsed index")
    return [
        (interior, details["pointer_value"].to_bytes(width, "little"), undefined),
        (edge_offset, edge_value.to_bytes(width, "little"), undefined),
    ], edge_offset


def _payload_change(source: Path, coordinate: int) -> tuple[int, bytes, bytes]:
    with h5py.File(source, "r") as handle:
        dataset = handle[DATASET]
        index = coordinate // dataset.chunks[0]
        info = dataset.id.get_chunk_info(index)
        if info.chunk_offset != (coordinate,) or info.filter_mask != 0:
            raise CatalogError("selected chunk does not match verified compressed coordinates")
        offset = info.byte_offset
    with source.open("rb") as stream:
        stream.seek(offset)
        first_byte = stream.read(1)
    if first_byte not in (b"\x78",):
        raise CatalogError("compressed GWOSC chunk has an unexpected zlib header")
    return offset, first_byte, b"\x00"


def _header_change(source: Path, details: dict[str, Any]) -> tuple[int, bytes, bytes]:
    with H5File(source) as reader:
        offset = reader.absolute(details["source_object_address"])
    with source.open("rb") as stream:
        stream.seek(offset)
        version = stream.read(1)
    if version != b"\x01":
        raise CatalogError("selected object header is not the verified version-one format")
    return offset, version, b"\x00"


def _call_recovery(source: Path, dataset: str, case_dir: Path, python: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    output = case_dir / "recovered.h5"
    report = case_dir / "recovery.json"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    try:
        completed = subprocess.run(
            [python, "-m", "h5reclaim", "recover", str(source),
             "--dataset", dataset, "--output", str(output), "--report", str(report)],
            cwd=case_dir, env=environment, capture_output=True,
            text=True, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CatalogError(f"recovery timed out for {case_dir.name}") from exc
    return completed, output, report


def _evaluate_output(
    source: Path, damaged: Path, output: Path, report_path: Path, *,
    rejected: set[int], detached: set[int], expected_reconstructed: int,
) -> dict[str, Any]:
    """Treat every status claim as a promise, including a claimed omission."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    source_hash = _hash(damaged)
    if (
        report.get("source", {}).get("sha256_before") != source_hash
        or report["source"].get("sha256_after") != source_hash
        or report["source"].get("size_bytes") != damaged.stat().st_size
    ):
        raise CatalogError("recovery report does not identify the unchanged trial input")
    if report.get("dataset", {}).get("path") != DATASET:
        raise CatalogError("recovery selected a different scientific dataset")
    if report.get("execution_state") != "finished":
        raise CatalogError("recovery did not finish its report")
    with h5py.File(source, "r") as reference, h5py.File(output, "r") as result:
        original = reference[DATASET]
        candidate = result[DATASET]
        if candidate.shape != original.shape or candidate.dtype != original.dtype or candidate.chunks != original.chunks:
            raise CatalogError("recovered scientific shape, dtype, or chunking changed")
        chunk_len = original.chunks[0]
        count = original.shape[0] // chunk_len
        original_records = {
            info.chunk_offset[0] // chunk_len: (info.byte_offset, info.size)
            for info in (original.id.get_chunk_info(i) for i in range(original.id.get_num_chunks()))
        }
        if set(original_records) != set(range(count)):
            raise CatalogError("original scientific chunk inventory is incomplete")
        validity = result["/_h5reclaim/chunk_status"][...]
        if validity.shape != (count,) or validity.dtype != np.dtype("u1"):
            raise CatalogError("missing or invalid output chunk status map")
        if not set(int(value) for value in validity) <= STATUS_NAMES.keys():
            raise CatalogError("output contains an unknown chunk status")
        embedded = result["/_h5reclaim/report_json"][()]
        if isinstance(embedded, bytes):
            embedded = embedded.decode("utf-8")
        if json.loads(embedded) != report:
            raise CatalogError("external and embedded reports disagree")
        copied = set(report["dataset"].get("attributes_copied", []))
        omitted = set(report["dataset"].get("attributes_omitted", []))
        if copied & omitted or copied | omitted != set(original.attrs):
            raise CatalogError("attribute report must account for each source attribute")
        for name in copied:
            if name not in candidate.attrs or not np.array_equal(
                np.asarray(original.attrs[name]), np.asarray(candidate.attrs[name])
            ):
                raise CatalogError(f"scientific attribute {name!r} differs or was omitted")
        actual_rejected = {i for i, code in enumerate(validity) if int(code) != 1}
        if actual_rejected != rejected:
            raise CatalogError(f"status omissions {sorted(actual_rejected)} differ from expected {sorted(rejected)}")
        if any(int(validity[i]) != 6 for i in rejected):
            raise CatalogError("corrupt chunk was not marked decode_failed")
        wrong: list[int] = []
        for i, code in enumerate(validity):
            if int(code) != 1:
                continue  # zero fill in a missing region is not a measurement
            position = slice(i * chunk_len, (i + 1) * chunk_len)
            expected_bits = np.asarray(original[position], dtype="<f8").view("<u8")
            observed_bits = np.asarray(candidate[position], dtype="<f8").view("<u8")
            if not np.array_equal(expected_bits, observed_bits):
                wrong.append(i)
        if wrong:
            raise CatalogError(f"claimed recovered chunks have wrong bits at {wrong[:8]}")
    mapped = report.get("mappings")
    if not isinstance(mapped, list) or len(mapped) != count - len(rejected):
        raise CatalogError("per-chunk mappings do not match claimed output")
    seen: set[int] = set()
    reconstructed = 0
    for mapping in mapped:
        index = mapping.get("chunk_index")
        if (
            not isinstance(index, list) or len(index) != 1
            or type(index[0]) is not int or index[0] < 0 or index[0] >= count
            or index[0] in seen or index[0] in rejected
            or mapping.get("coordinate") != [index[0] * chunk_len]
            or mapping.get("integrity") != "fletcher32_verified"
            or mapping.get("route") != (
                "reconstructed_link" if index[0] in detached else "intact_tree"
            )
            or (mapping.get("source_absolute_offset"), mapping.get("size_bytes"))
                != original_records[index[0]]
        ):
            raise CatalogError("claim lacks unique coordinate or checksum evidence")
        seen.add(index[0])
        reconstructed += mapping.get("route") == "reconstructed_link"
    if reconstructed != expected_reconstructed or report.get("reconstructed_chunks") != reconstructed:
        raise CatalogError("reconstructed-link count differs from the intended trial")
    counts = report.get("counts", {})
    actual_counts = {name: int(np.count_nonzero(validity == code)) for code, name in STATUS_NAMES.items()}
    if any(counts.get(name, 0) != number for name, number in actual_counts.items()):
        raise CatalogError("report and embedded status map counts disagree")
    complete = not rejected
    if report.get("complete") is not complete or report.get("outcome") != ("complete" if complete else "partial"):
        raise CatalogError("partial or complete outcome contradicts the status map")
    failed = report.get("failed_chunks", [])
    if (
        not isinstance(failed, list)
        or {item.get("chunk_index", [None])[0] for item in failed} != rejected
        or any("checksum" not in item.get("reason", "").lower()
               and "deflate" not in item.get("reason", "").lower() for item in failed)
    ):
        raise CatalogError("report does not identify every rejected compressed payload")
    return {
        "decision": "complete" if complete else "partial",
        "exact_verified_chunks": len(mapped),
        "rejected_corrupt_chunks": len(rejected),
        "reconstructed_link_chunks": reconstructed,
        "wrong_claimed_chunks": 0,
        "status_counts": actual_counts,
        "recovered_file": str(output),
        "recovery_report": str(report_path),
    }


def _one_case(
    *, case: str, category: str, scientific_source: Path, dataset: str,
    source_sha256: str, changes: list[tuple[int, bytes, bytes]], directory: Path,
    python: str, expect_refusal: bool, rejected: set[int] | None = None,
    detached: set[int] | None = None, reconstructed: int = 0, reason: str,
) -> dict[str, Any]:
    directory.mkdir()
    damaged = directory / "input.h5"
    damaged_hash = _mutated_copy(scientific_source, damaged, changes)
    if damaged_hash == source_sha256 and changes:
        raise CatalogError("controlled damage failed to change the copy")
    result, output, report = _call_recovery(damaged, dataset, directory, python)
    if _hash(scientific_source) != source_sha256 or _hash(damaged) != damaged_hash:
        raise CatalogError("scientific original or damaged copy changed during recovery")
    if expect_refusal:
        if result.returncode != 2 or output.exists() or report.exists():
            raise CatalogError(f"{case} should refuse without publishing artifacts: {result.stderr[-1200:]}")
        observed: dict[str, Any] = {
            "decision": "refused", "exit_code": 2,
            "reason": (result.stderr or result.stdout).strip()[-500:],
            "output_published": False,
        }
    else:
        if result.returncode != 0 or not output.is_file() or not report.is_file():
            raise CatalogError(f"{case} unexpectedly failed: {result.stderr[-1200:]}")
        observed = _evaluate_output(
            scientific_source, damaged, output, report, rejected=rejected or set(),
            detached=detached or set(), expected_reconstructed=reconstructed,
        )
    return {
        "case": case, "damage_class": category, "source_id": scientific_source.name,
        "dataset": dataset, "original_sha256": source_sha256,
        "trial_input_sha256": damaged_hash, "trial_input": str(damaged),
        "mutation_byte_offsets": [offset for offset, _before, _after in changes],
        "expected_behavior": reason, "observed": observed, "passed": True,
    }


def run_catalog(work_dir: Path, *, python: str = sys.executable) -> dict[str, Any]:
    """Verify all four authentic sources, then run deterministic damage classes."""
    if work_dir.exists() and (not work_dir.is_dir() or any(work_dir.iterdir())):
        raise CatalogError(f"work directory must be a new or empty directory: {work_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)
    inventory = survey_corpus()
    if not inventory["baseline_matches_manifest"] or inventory["baseline_unpinned"]:
        raise CatalogError("real scientific corpus no longer matches its pinned support baseline")
    entries = json.loads((ROOT / "corpus" / "manifest.json").read_text(encoding="utf-8"))["entries"]
    if len(entries) != 4 or inventory["original_files_verified"] != 4:
        raise CatalogError("this catalog requires the four pinned authentic originals")
    indexed = {entry["id"]: entry for entry in entries}
    id_16khz = "gwosc_gw150914_h1_strain_16khz"
    healthy = ROOT / "corpus" / indexed[id_16khz]["path"]
    if healthy.name != FILENAME:
        raise CatalogError("16 kHz benchmark selected an unexpected scientific original")
    details = _inspect_original(healthy)
    pointer_changes, _edge_offset = _pointer_changes(healthy, details)
    detached_count = len(details["affected_offsets"])
    if not detached_count:
        raise CatalogError("verified detached leaf has no chunks")
    payload_unaffected = _payload_change(healthy, 0)
    payload_detached = _payload_change(healthy, details["affected_offsets"][0])
    if payload_unaffected[0] == payload_detached[0]:
        raise CatalogError("mixed case targeted the same chunk twice")
    expected_chunks = details["chunk_count"]
    detached = {coordinate // details["chunk_shape"][0] for coordinate in details["affected_offsets"]}
    damaged_detached_index = details["affected_offsets"][0] // details["chunk_shape"][0]
    cases: list[dict[str, Any]] = []
    scenarios = (
        ("one_index_link", "single lost index pointer", pointer_changes[:1], False, set(), detached, detached_count,
         "full exact recovery of bytes reachable through reciprocal index neighbors"),
        ("corrupt_payload", "compressed payload byte changed", [payload_unaffected], False, {0}, set(), 0,
         "partial output; one failed payload is marked, not presented as a measurement"),
        ("index_and_payload", "lost index pointer plus compressed payload corruption",
         [pointer_changes[0], payload_detached], False,
         {damaged_detached_index}, detached,
         detached_count - 1, "partial recovery with the compromised detached payload marked failed"),
        ("two_index_links", "two lost root child pointers", pointer_changes, True, set(), set(), 0,
         "refusal with two missing index links and no claim of reconstructed measurements"),
        ("selected_object_header", "selected dataset header byte changed", [_header_change(healthy, details)],
         True, set(), set(), 0, "refusal when the selected dataset metadata cannot be trusted"),
    )
    for name, category, changes, refuse, rejected, detached_expected, reconstructed, reason in scenarios:
        cases.append(_one_case(
            case=name, category=category, scientific_source=healthy,
            dataset=DATASET, source_sha256=indexed[id_16khz]["sha256"],
            changes=changes, directory=work_dir / name, python=python,
            expect_refusal=refuse, rejected=rejected, detached=detached_expected,
            reconstructed=reconstructed,
            reason=reason,
        ))

    id_4khz = "gwosc_gw150914_h1_strain"
    source_4khz = ROOT / "corpus" / indexed[id_4khz]["path"]
    with h5py.File(source_4khz, "r") as handle, H5File(source_4khz) as raw:
        strain = handle[DATASET]
        object_address = int(h5py.h5o.get_info(strain.id).addr)
        root_address = raw.read_dataset_layout(object_address, rank=1).root_address
        direct_root = raw.read_tree(root_address, rank=1, element_size=8)
        if (
            direct_root.level != 0 or len(direct_root.entries) != 64
            or strain.chunks != (2048,) or strain.id.get_num_chunks() != 64
        ):
            raise CatalogError("pinned 4 kHz GWOSC direct index layout changed")
        direct = direct_root.entries[0]
        if direct.address is None:
            raise CatalogError("4 kHz direct chunk pointer is undefined before mutation")
        pointer_offset = direct.pointer_offset
        width = raw.superblock.offset_size
        pointer_before = direct.address.to_bytes(width, "little")
        for i, entry in enumerate(direct_root.entries):
            info = strain.id.get_chunk_info(i)
            if (
                entry.key.offsets != (i * 2048, 0)
                or (raw.absolute(entry.address), entry.key.stored_size, entry.key.filter_mask)
                    != (info.byte_offset, info.size, info.filter_mask)
            ):
                raise CatalogError("4 kHz raw chunk record differs from HDF5's chunk inventory")
    cases.append(_one_case(
        case="gwosc_4khz_intact", category="intact direct-index baseline",
        scientific_source=source_4khz, dataset=DATASET,
        source_sha256=indexed[id_4khz]["sha256"], changes=[],
        directory=work_dir / "gwosc_4khz_intact", python=python,
        expect_refusal=False, reason="64 indexed chunks are exported exactly; no index path reconstructed",
    ))
    cases.append(_one_case(
        case="gwosc_4khz_corrupt_payload", category="compressed payload byte changed in direct index",
        scientific_source=source_4khz, dataset=DATASET,
        source_sha256=indexed[id_4khz]["sha256"], changes=[_payload_change(source_4khz, 0)],
        directory=work_dir / "gwosc_4khz_corrupt_payload", python=python,
        expect_refusal=False, rejected={0},
        reason="63 exact chunks and one failed payload marked, with no false reconstruction",
    ))
    cases.append(_one_case(
        case="gwosc_4khz_missing_payload_pointer",
        category="direct root-to-payload pointer changed to undefined",
        scientific_source=source_4khz, dataset=DATASET,
        source_sha256=indexed[id_4khz]["sha256"],
        changes=[(pointer_offset, pointer_before, b"\xff" * width)],
        directory=work_dir / "gwosc_4khz_missing_payload_pointer", python=python,
        expect_refusal=True,
        reason="no invented chunk location when a direct payload address is lost",
    ))

    for identifier in (
        "zenodo_qubit_feedback", "zenodo_pallas_cloud_aircraft",
    ):
        entry = indexed[identifier]
        cases.append(_one_case(
            case=identifier, category="different intact scientific layout",
            scientific_source=ROOT / "corpus" / entry["path"],
            dataset=entry["representative_dataset"], source_sha256=entry["sha256"],
            changes=[], directory=work_dir / identifier, python=python,
            expect_refusal=True, reason="unsupported representative dataset is refused without output",
        ))
    if _hash(healthy) != indexed[id_16khz]["sha256"]:
        raise CatalogError("original GWOSC scientific source changed")
    return {
        "schema_version": 1,
        "experiment": "controlled damage and refusal catalog on authentic scientific layouts",
        "four_originals_verified": 4,
        "survey_candidate_count": inventory["dataset_support_counts"].get("candidate", 0),
        "all_cases_passed": all(case["passed"] for case in cases),
        "case_count": len(cases),
        "reference_gwosc_chunks": {"16khz": expected_chunks, "4khz": 64},
        "cases": cases,
        "limits": (
            "These are deterministic controlled changes to verified original files. "
            "Only recovered-status chunks are compared with independent healthy truth. "
            "Refusals are safe behavior, not recovered data. No naturally damaged file, "
            "other damage distribution, or population success rate is established."
        ),
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "H5Reclaim | Real-data controlled damage catalog",
        "=" * 48,
        f"RESULT: {'PASS' if report['all_cases_passed'] else 'FAIL'} | "
        f"{report['case_count']} cases | {report['four_originals_verified']} authentic sources verified",
        f"Corpus survey candidates: {report['survey_candidate_count']} "
        "(the fault catalog exercises only the selected strain layouts)",
        "",
    ]
    for case in report["cases"]:
        observation = case["observed"]
        if observation["decision"] == "refused":
            outcome = "refused safely, no output"
        else:
            outcome = (
                f"{observation['decision']}: {observation['exact_verified_chunks']} "
                f"exact chunks; {observation['rejected_corrupt_chunks']} marked corrupt; "
                f"{observation['reconstructed_link_chunks']} reconstructed"
            )
        lines.append(f"  {case['case']}: {outcome}")
    lines.extend(["", "Refusals and missing chunks are not counted as recovered measurements.",
                  "No success percentage for arbitrary real-world damage follows from these cases.",
                  "For machine-readable evidence, rerun with --json."])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, help="new or empty directory for retained trial evidence")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter for recovery subprocesses")
    parser.add_argument("--json", action="store_true", help="print complete machine-readable trial results")
    args = parser.parse_args()
    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="h5reclaim-damage-catalog-"))
    try:
        report = run_catalog(work_dir, python=args.python)
    except (CatalogError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(2, f"damage catalog failed: {exc}\nwork directory: {work_dir}\n")
    (work_dir / "catalog.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render_text(report))
    print(f"Full evaluation: {work_dir / 'catalog.json'}", file=sys.stderr if args.json else sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
