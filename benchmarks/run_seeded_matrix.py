"""Seeded, independently scored damage matrix on four pinned scientific files.

The evaluator alone reads the pristine files to choose verified mutation sites
and compare measurements. Recovery is invoked as a subprocess with only its
damaged input, dataset path, and output paths. The reproducible matrix covers
declared seeded fault classes and retains recovery, unknown and refusal outcomes.
Recovery and scoring use separate program inputs on a shared filesystem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
sys.path.insert(0, str(ROOT / "src"))

from run_damage_catalog import (  # noqa: E402
    DATASET, STATUS_NAMES, CatalogError, _call_recovery, _hash,
    _header_change, _inspect_original, _mutated_copy, _pointer_changes,
)
from run_real_corpus import run as survey_corpus  # noqa: E402
from h5reclaim.format import H5File  # noqa: E402


class MatrixError(RuntimeError):
    """Invalid experimental preconditions, rather than a product failure."""


@dataclass(frozen=True)
class Mutation:
    offset: int
    before: bytes
    after: bytes


@dataclass(frozen=True)
class Scenario:
    name: str
    source_id: str
    damage_class: str
    expectation: str  # full, partial_one, refusal
    mutations: tuple[Mutation, ...] = ()
    truncate_after: int | None = None
    target_chunk: int | None = None


def _byte_flip(source: Path, index: int, rng: random.Random) -> Mutation:
    """Pick a seeded bit flip that demonstrably damages the zlib stream."""
    with h5py.File(source, "r") as handle:
        dataset = handle[DATASET]
        info = dataset.id.get_chunk_info(index)
        if info.chunk_offset != (index * dataset.chunks[0],) or info.filter_mask != 0:
            raise MatrixError("the chosen original chunk differs from its verified index")
        if info.size < 16 or info.size > 1_048_576:
            raise MatrixError("compressed chunk is outside the bounded trial size")
    with source.open("rb") as stream:
        stream.seek(info.byte_offset)
        raw = stream.read(info.size)
    if len(raw) != info.size or raw[:1] != b"\x78":
        raise MatrixError("the pinned chunk lacks its complete zlib stream")
    try:
        zlib.decompress(raw)
    except zlib.error as exc:
        raise MatrixError("healthy original chunk cannot be inflated") from exc
    for _attempt in range(32):
        position = rng.randrange(2, info.size - 4)
        bit = 1 << rng.randrange(8)
        candidate = bytearray(raw)
        candidate[position] ^= bit
        try:
            zlib.decompress(candidate)
        except zlib.error:
            return Mutation(info.byte_offset + position, raw[position:position + 1], bytes((candidate[position],)))
    # A one-bit change in CMF invalidates the zlib header check; this fallback
    # makes every seeded case an actual decode failure without guessing values.
    bit = 1 << rng.randrange(8)
    candidate = bytearray(raw)
    candidate[0] ^= bit
    try:
        zlib.decompress(candidate)
    except zlib.error:
        return Mutation(info.byte_offset, raw[:1], bytes(candidate[:1]))
    raise MatrixError("no controlled bit flip caused a decoder failure")


def _direct_index(source: Path) -> tuple[list[tuple[int, bytes]], list[tuple[int, int]]]:
    """Cross-check each direct-index pointer with independent h5py chunk info."""
    with h5py.File(source, "r") as handle, H5File(source) as reader:
        dataset = handle[DATASET]
        if dataset.shape != (131072,) or dataset.chunks != (2048,) or dataset.dtype.str != "<f8":
            raise MatrixError("the pinned 4 kHz scientific dataset layout changed")
        object_address = int(h5py.h5o.get_info(dataset.id).addr)
        layout = reader.read_dataset_layout(object_address, rank=1)
        root = reader.read_tree(layout.root_address, rank=1, element_size=8)
        if root.level != 0 or len(root.entries) != dataset.id.get_num_chunks() or len(root.entries) != 64:
            raise MatrixError("the pinned 4 kHz index is no longer a 64-chunk direct v1 root")
        width = reader.superblock.offset_size
        pointers = []
        payloads = []
        for index, entry in enumerate(root.entries):
            info = dataset.id.get_chunk_info(index)
            if (
                entry.address is None or entry.key.offsets != (index * 2048, 0)
                or (reader.absolute(entry.address), entry.key.stored_size, entry.key.filter_mask)
                    != (info.byte_offset, info.size, info.filter_mask)
            ):
                raise MatrixError("direct index and library disagree about source ownership")
            pointers.append((entry.pointer_offset, entry.address.to_bytes(width, "little")))
            payloads.append((info.byte_offset, info.size))
    return pointers, payloads


def _source_records(source: Path) -> tuple[int, int, dict[int, tuple[int, int]], list[np.ndarray]]:
    """Reference values and byte ranges stay in the evaluator, never the child CLI."""
    with h5py.File(source, "r") as handle:
        dataset = handle[DATASET]
        if dataset.ndim != 1 or dataset.dtype.str != "<f8" or dataset.chunks is None:
            raise MatrixError("the scoring source is not the pinned rank-one float64 dataset")
        chunk_len = dataset.chunks[0]
        if dataset.shape[0] % chunk_len:
            raise MatrixError("the pinned scoring source has an edge chunk")
        count = dataset.shape[0] // chunk_len
        records = {}
        for index in range(dataset.id.get_num_chunks()):
            info = dataset.id.get_chunk_info(index)
            coordinate = info.chunk_offset[0]
            if coordinate % chunk_len or coordinate // chunk_len in records:
                raise MatrixError("original chunk coordinates overlap")
            records[coordinate // chunk_len] = (info.byte_offset, info.size)
        if set(records) != set(range(count)):
            raise MatrixError("the healthy reference has missing allocations")
        values = [np.asarray(dataset[i * chunk_len:(i + 1) * chunk_len], dtype="<f8")
                  for i in range(count)]
    return count, chunk_len, records, values


def _native_inaccessible(damaged: Path, reference: list[np.ndarray], chunk_len: int) -> list[int]:
    """A native failure, fill, or wrong value counts as inaccessible."""
    try:
        with h5py.File(damaged, "r") as handle:
            dataset = handle[DATASET]
            if dataset.shape != (len(reference) * chunk_len,) or dataset.dtype.str != "<f8":
                return list(range(len(reference)))
            unavailable = []
            for index, expected in enumerate(reference):
                try:
                    observed = np.asarray(dataset[index * chunk_len:(index + 1) * chunk_len], dtype="<f8")
                    if not np.array_equal(observed.view("<u8"), expected.view("<u8")):
                        unavailable.append(index)
                except (OSError, RuntimeError, ValueError, KeyError):
                    unavailable.append(index)
            return unavailable
    except (OSError, RuntimeError, ValueError, KeyError):
        return list(range(len(reference)))


def score_output(
    original: Path, damaged: Path, output: Path, report_path: Path,
) -> dict[str, Any]:
    """Score every accepted coordinate against evaluator-only authentic truth.

    A malformed or contradictory result is retained as an evaluation error.
    Wrong accepted values or addresses are counted rather than raising and
    losing the trial report.
    """
    count, chunk_len, records, reference = _source_records(original)
    native_unavailable = _native_inaccessible(damaged, reference, chunk_len)
    errors: list[str] = []
    accepted: set[int] = set()
    exact: set[int] = set()
    wrong_values: set[int] = set()
    unproven: set[int] = set()
    unresolved: set[int] = set()
    reconstructed = 0
    result: dict[str, Any] = {
        "total_chunks": count,
        "native_unavailable_or_wrong_chunks": len(native_unavailable),
        "native_unavailable_indices": native_unavailable,
    }
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        with h5py.File(output, "r") as handle:
            dataset = handle[DATASET]
            if dataset.shape != (count * chunk_len,) or dataset.dtype.str != "<f8" or dataset.chunks != (chunk_len,):
                errors.append("output scientific shape, datatype, or chunking differs from the original")
            status = handle["/_h5reclaim/chunk_status"][...]
            if status.shape != (count,) or status.dtype != np.dtype("u1"):
                errors.append("output validity map is absent or has incorrect type or shape")
                status = np.full(count, 255, dtype="u1")
            if not set(int(value) for value in status) <= STATUS_NAMES.keys():
                errors.append("output validity map contains unknown status codes")
            embedded = handle["/_h5reclaim/report_json"][()]
            if isinstance(embedded, bytes):
                embedded = embedded.decode("utf-8")
            if json.loads(embedded) != report:
                errors.append("embedded and external evidence reports differ")
            accepted = {i for i, code in enumerate(status) if int(code) == 1}
            unresolved = set(range(count)) - accepted
            for index in accepted:
                try:
                    observed = np.asarray(dataset[index * chunk_len:(index + 1) * chunk_len], dtype="<f8")
                    if not np.array_equal(observed.view("<u8"), reference[index].view("<u8")):
                        wrong_values.add(index)
                except (OSError, RuntimeError, ValueError, KeyError):
                    wrong_values.add(index)
        reported_source = report.get("source", {})
        damaged_hash = _hash(damaged)
        if (reported_source.get("sha256_before") != damaged_hash
                or reported_source.get("sha256_after") != damaged_hash
                or reported_source.get("size_bytes") != damaged.stat().st_size):
            errors.append("report source identity differs from the unchanged damaged copy")
        if report.get("dataset", {}).get("path") != DATASET:
            errors.append("report selected a different scientific dataset")
        if report.get("execution_state") != "finished":
            errors.append("report is not marked finished")
        if report.get("complete") is not (not unresolved):
            errors.append("report completeness contradicts its validity map")
        if report.get("outcome") != ("complete" if not unresolved else "partial"):
            errors.append("report outcome contradicts its validity map")
        mappings = report.get("mappings")
        if not isinstance(mappings, list):
            mappings = []
            errors.append("report lacks per-chunk mappings")
        mapped: set[int] = set()
        for mapping in mappings:
            position = mapping.get("chunk_index") if isinstance(mapping, dict) else None
            if not isinstance(position, list) or len(position) != 1 or type(position[0]) is not int:
                errors.append("mapping has no valid chunk index")
                continue
            index = position[0]
            if index not in accepted or index in mapped:
                errors.append("mapping is duplicate or asserts an unaccepted chunk")
                continue
            mapped.add(index)
            if (
                mapping.get("coordinate") != [index * chunk_len]
                or (mapping.get("source_absolute_offset"), mapping.get("size_bytes")) != records[index]
                or mapping.get("integrity") != "fletcher32_verified"
                or mapping.get("route") not in ("intact_tree", "reconstructed_link")
            ):
                unproven.add(index)
            reconstructed += mapping.get("route") == "reconstructed_link"
        unproven.update(accepted - mapped)
        if report.get("reconstructed_chunks") != reconstructed:
            errors.append("reconstructed-link count disagrees with mappings")
        counts = report.get("counts", {})
        for code, name in STATUS_NAMES.items():
            if counts.get(name, 0) != int(np.count_nonzero(status == code)):
                errors.append(f"reported {name} count contradicts the validity map")
        exact = accepted - wrong_values - unproven
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError,
            json.JSONDecodeError) as exc:
        errors.append(f"cannot fully evaluate published output: {type(exc).__name__}: {exc}")
    false_accepted = wrong_values | unproven
    result.update({
        "decision": "output",
        "exact_verified_chunks": len(exact),
        "newly_accessible_exact_chunks": len(exact & set(native_unavailable)),
        "unresolved_chunks": len(unresolved),
        "wrong_value_chunks": len(wrong_values),
        "unproven_accepted_chunks": len(unproven),
        "false_accepted_chunks": len(false_accepted),
        "false_accepted_indices": sorted(false_accepted),
        "reconstructed_link_chunks": reconstructed,
        "evaluation_errors": errors,
        "safety_passed": not false_accepted and not errors,
    })
    return result


def _verify_manifest() -> dict[str, dict[str, Any]]:
    manifest = json.loads((ROOT / "corpus" / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest.get("entries", [])
    if len(entries) != 4:
        raise MatrixError("matrix requires exactly four pinned authentic scientific files")
    indexed = {entry["id"]: entry for entry in entries}
    for entry in entries:
        source = ROOT / "corpus" / entry["path"]
        if not source.is_file() or source.stat().st_size != entry["size_bytes"] or _hash(source) != entry["sha256"]:
            raise MatrixError(f"scientific original differs from pinned hash: {entry['id']}")
    inventory = survey_corpus()
    if not inventory["baseline_matches_manifest"] or inventory["baseline_unpinned"]:
        raise MatrixError("scientific corpus format inventory differs from the pinned baseline")
    return indexed


def _scenarios(indexed: dict[str, dict[str, Any]], seed: int, trials: int) -> list[tuple[int | None, Scenario]]:
    id16 = "gwosc_gw150914_h1_strain_16khz"
    id4 = "gwosc_gw150914_h1_strain"
    original16 = ROOT / "corpus" / indexed[id16]["path"]
    original4 = ROOT / "corpus" / indexed[id4]["path"]
    details = _inspect_original(original16)
    pointer_changes, _ = _pointer_changes(original16, details)
    direct_pointers, direct_payloads = _direct_index(original4)
    with H5File(original16) as reader:
        root = reader.read_tree(details["btree_root_address"], rank=1, element_size=8)
        neighbor = root.entries[details["child_index"] - 1]
        if neighbor.address is None:
            raise MatrixError("redirect target is not a verified sibling leaf")
        redirect = Mutation(details["pointer_offset"], pointer_changes[0][1], neighbor.address.to_bytes(details["pointer_size"], "little"))
    header_change = _header_change(original16, details)
    with original16.open("rb") as stream:
        signature = stream.read(8)
    if signature != b"\x89HDF\r\n\x1a\n":
        raise MatrixError("the pinned HDF5 signature differs")
    scenarios: list[tuple[int | None, Scenario]] = [
        (None, Scenario("gwosc_4khz_intact", id4, "direct v1 index: intact", "full")),
        (None, Scenario("zenodo_qubit_unsupported", "zenodo_qubit_feedback", "different scientific layout", "refusal")),
        (None, Scenario("zenodo_aircraft_unsupported", "zenodo_pallas_cloud_aircraft", "compound scientific table", "refusal")),
    ]
    for trial in range(trials):
        rng = random.Random(int.from_bytes(hashlib.sha256(f"{seed}:{trial}".encode("ascii")).digest(), "big"))
        chunk16 = rng.randrange(128)
        detached = [coordinate // details["chunk_shape"][0] for coordinate in details["affected_offsets"]]
        detached_chunk = rng.choice(detached)
        payload = _byte_flip(original16, chunk16, rng)
        mixed_payload = _byte_flip(original16, detached_chunk, rng)
        chunk4 = rng.randrange(64)
        direct_payload = _byte_flip(original4, chunk4, rng)
        lost_index = Mutation(*pointer_changes[0])
        left_index = Mutation(*pointer_changes[1])
        direct_offset, direct_before = direct_pointers[chunk4]
        direct_loss = Mutation(direct_offset, direct_before, b"\xff" * len(direct_before))
        last_start, last_size = max(direct_payloads, key=lambda pair: pair[0] + pair[1])
        truncation = last_start + last_size // 2
        if truncation >= original4.stat().st_size or truncation <= 0:
            raise MatrixError("truncation position does not cut into the last stored payload")
        scenarios.extend((
            (trial, Scenario("index_link", id16, "lost level-one root-to-leaf link", "full", (lost_index,))),
            (trial, Scenario("payload", id16, "compressed payload bit flip", "partial_one", (payload,), target_chunk=chunk16)),
            (trial, Scenario("link_and_payload", id16, "index and detached payload faults", "partial_one", (lost_index, mixed_payload), target_chunk=detached_chunk)),
            (trial, Scenario("two_links", id16, "two lost root-to-leaf links", "refusal", (lost_index, left_index))),
            (trial, Scenario("redirect_link", id16, "misleading pointer to reachable neighbor", "refusal", (redirect,))),
            (trial, Scenario("object_header", id16, "selected dataset object header damage", "refusal", (Mutation(*header_change),))),
            (trial, Scenario("superblock", id16, "HDF5 signature damage", "refusal", (Mutation(0, signature[:1], bytes((signature[0] ^ (1 << rng.randrange(8)),))),))),
            (trial, Scenario("direct_payload", id4, "direct-index compressed payload bit flip", "partial_one", (direct_payload,), target_chunk=chunk4)),
            (trial, Scenario("direct_pointer", id4, "lost direct payload pointer", "refusal", (direct_loss,), target_chunk=chunk4)),
            (trial, Scenario("truncated_payload", id4, "file truncated inside stored payload", "refusal", truncate_after=truncation)),
        ))
    return scenarios


def _run_case(
    indexed: dict[str, dict[str, Any]], scenario: Scenario, trial: int | None,
    work_dir: Path, python: str,
) -> dict[str, Any]:
    entry = indexed[scenario.source_id]
    original = ROOT / "corpus" / entry["path"]
    name = f"trial_{trial:03d}_{scenario.name}" if trial is not None else scenario.name
    case_dir = work_dir / name
    case_dir.mkdir()
    damaged = case_dir / "input.h5"
    if scenario.truncate_after is not None:
        with original.open("rb") as source, damaged.open("xb") as destination:
            remaining = scenario.truncate_after
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    raise MatrixError("truncation exceeded source length")
                destination.write(block)
                remaining -= len(block)
    else:
        _mutated_copy(original, damaged, [(m.offset, m.before, m.after) for m in scenario.mutations])
    input_hash = _hash(damaged)
    result, output, report_path = _call_recovery(damaged, entry["representative_dataset"], case_dir, python)
    if _hash(original) != entry["sha256"] or _hash(damaged) != input_hash:
        raise MatrixError("original or damaged input changed during the trial")
    if result.returncode == 2 and not output.exists() and not report_path.exists():
        observation: dict[str, Any] = {
            "decision": "refused", "exit_code": 2, "output_published": False,
            "reason": (result.stderr or result.stdout).strip()[-400:],
            "false_accepted_chunks": 0, "exact_verified_chunks": 0,
            "safety_passed": True,
        }
    elif result.returncode == 0 and output.is_file() and report_path.is_file() and scenario.source_id.startswith("gwosc_"):
        observation = score_output(original, damaged, output, report_path)
        observation["exit_code"] = 0
    else:
        observation = {
            "decision": "invalid", "exit_code": result.returncode,
            "output_published": output.exists(), "report_published": report_path.exists(),
            "reason": (result.stderr or result.stdout).strip()[-400:],
            "false_accepted_chunks": 0, "exact_verified_chunks": 0,
            "safety_passed": False,
        }
    expected = scenario.expectation
    if expected == "refusal":
        expectation_met = observation["decision"] == "refused"
    elif expected == "full":
        expectation_met = (observation["decision"] == "output" and observation.get("unresolved_chunks") == 0
                           and observation.get("exact_verified_chunks") == observation.get("total_chunks"))
    else:
        expectation_met = (observation["decision"] == "output" and observation.get("unresolved_chunks") == 1
                           and observation.get("exact_verified_chunks") == observation.get("total_chunks", 0) - 1
                           and scenario.target_chunk in observation.get("native_unavailable_indices", []))
    return {
        "case": name, "trial": trial, "damage_class": scenario.damage_class,
        "source_id": scenario.source_id, "dataset": entry["representative_dataset"],
        "original_sha256": entry["sha256"], "damaged_sha256": input_hash,
        "input": str(damaged), "mutation_byte_offsets": [m.offset for m in scenario.mutations],
        "truncated_at_byte": scenario.truncate_after, "target_chunk": scenario.target_chunk,
        "expected_behavior": expected, "expectation_met": expectation_met,
        "observed": observation,
        "passed": expectation_met and observation["safety_passed"],
    }


def run_matrix(work_dir: Path, *, seed: int = 20260927, trials: int = 2,
               python: str = sys.executable) -> dict[str, Any]:
    if trials < 1 or trials > 20 or seed < 0 or seed >= 2 ** 64:
        raise MatrixError("use 1..20 trials and an unsigned 64-bit seed")
    if work_dir.exists() and (not work_dir.is_dir() or any(work_dir.iterdir())):
        raise MatrixError(f"work directory must be new or empty: {work_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)
    indexed = _verify_manifest()
    scenarios = _scenarios(indexed, seed, trials)
    cases = [_run_case(indexed, scenario, trial, work_dir, python) for trial, scenario in scenarios]
    decisions = {decision: sum(case["observed"]["decision"] == decision for case in cases)
                 for decision in ("output", "refused", "invalid")}
    return {
        "schema_version": 1,
        "experiment": "seeded controlled damage on pinned scientific sources",
        "seed": seed, "trials": trials, "original_files_verified": len(indexed),
        "survey_candidate_count": sum(
            int(entry["expected_observation"]["candidate_count"])
            for entry in indexed.values()
        ),
        "case_count": len(cases), "decisions": decisions,
        "exact_verified_chunks": sum(case["observed"]["exact_verified_chunks"] for case in cases),
        "newly_accessible_exact_chunks": sum(case["observed"].get("newly_accessible_exact_chunks", 0)
                                             for case in cases),
        "false_accepted_chunks": sum(case["observed"]["false_accepted_chunks"] for case in cases),
        "safe_refusals": sum(case["observed"]["decision"] == "refused" for case in cases),
        "all_cases_passed": all(case["passed"] for case in cases),
        "cases": cases,
        "limits": (
            "Four authentic originals, two selected GWOSC strain layouts and declared seeded "
            "fault classes. The report records exact accepted chunks, newly accessible chunks, "
            "unknown chunks and refusals for every case. Repeated mutations reuse the same files. "
            "The evaluator alone receives the original truth through program inputs; recovery "
            "and scoring run on a shared filesystem."
        ),
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "H5Reclaim | Seeded scientific damage matrix",
        "=" * 49,
        f"Seed {report['seed']} | {report['trials']} trial(s) | {report['case_count']} cases | "
        f"{'PASS' if report['all_cases_passed'] else 'FAIL'}",
        f"Exact accepted chunks: {report['exact_verified_chunks']} "
        f"({report['newly_accessible_exact_chunks']} inaccessible to native reader) | "
        f"false accepted: {report['false_accepted_chunks']} | safe refusals: {report['safe_refusals']}",
        "",
    ]
    for case in report["cases"]:
        observed = case["observed"]
        if observed["decision"] == "output":
            outcome = (f"{observed['exact_verified_chunks']} exact, "
                       f"{observed['unresolved_chunks']} unresolved, "
                       f"{observed['false_accepted_chunks']} false accepted")
        else:
            outcome = observed["decision"]
        lines.append(f"  {'PASS' if case['passed'] else 'FAIL'} {case['case']}: {outcome}")
    lines.extend(["", "Reproducible controlled faults on pinned scientific sources, independently scored per chunk.",
                  "Use --json for the complete per-case evidence."])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260927, help="reproducible unsigned 64-bit seed")
    parser.add_argument("--trials", type=int, default=2, help="seeded choices per fault class, 1..20")
    parser.add_argument("--work-dir", type=Path, help="new or empty directory containing all trial evidence")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter for recovery subprocesses")
    parser.add_argument("--json", action="store_true", help="print full machine-readable evaluation")
    args = parser.parse_args()
    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="h5reclaim-seeded-matrix-"))
    try:
        report = run_matrix(work_dir, seed=args.seed, trials=args.trials, python=args.python)
    except (MatrixError, CatalogError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(2, f"seeded matrix failed to run: {exc}\nwork directory: {work_dir}\n")
    evidence_path = work_dir / "matrix.json"
    evidence_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render_text(report))
    print(f"Full evaluation: {evidence_path}", file=sys.stderr if args.json else sys.stdout)
    return 0 if report["all_cases_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
