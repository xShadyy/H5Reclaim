"""Independently score diverse HDF5 layouts and controlled adversarial faults.

The generated pristine sources are evaluator-only references. The public CLI
receives just a separate current/damaged file, selected path, and destinations.
The benchmark measures exact values and coordinates plus safe refusals. Its
cases are constructed and correlated, so its counts are not a population
success rate or evidence for a 99% claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from itertools import product
from pathlib import Path, PurePosixPath
from typing import Any

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmarks"))

from h5reclaim.modern_indexes import ModernH5File, lookup3  # noqa: E402
class EvaluationError(RuntimeError):
    """The experiment setup or a mandatory scored invariant failed."""


@dataclass(frozen=True)
class Case:
    name: str
    family: str
    route: str
    fault: str
    expectation: str  # exact, partial, unavailable, refuse, safe, unchecked_payload
    dataset: str = "/science"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _verify_corpus_files() -> dict[str, dict[str, Any]]:
    """Verify pinned original bytes without freezing coverage classifications.

    Parser support is expected to change during this project. A healthy-file
    survey candidate count is not a precondition for independent fault scoring.
    """
    manifest = json.loads((ROOT / "corpus" / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest.get("entries")
    if manifest.get("schema_version") != 1 or not isinstance(entries, list) or not entries:
        raise EvaluationError("missing pinned scientific corpus manifest")
    verified: dict[str, dict[str, Any]] = {}
    corpus_root = (ROOT / "corpus").resolve()
    for entry in entries:
        identifier, name = entry["id"], entry["path"]
        relative = PurePosixPath(name)
        if (identifier in verified or relative.is_absolute() or not relative.parts
                or ".." in relative.parts):
            raise EvaluationError("duplicate scientific id or unsafe corpus path")
        source = ROOT / "corpus" / Path(*relative.parts)
        if (source.is_symlink() or not source.resolve().is_relative_to(corpus_root)
                or not source.is_file()):
            raise EvaluationError(f"scientific original is absent or escapes the corpus: {identifier}")
        if source.stat().st_size != entry["size_bytes"] or digest(source) != entry["sha256"]:
            raise EvaluationError(f"scientific original fails pinned size or SHA-256: {identifier}")
        verified[identifier] = entry
    return verified


def _low_level_file(path: Path) -> tuple[h5py.h5f.FileID, h5py.h5p.PropFAID]:
    access = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
    access.set_libver_bounds(h5py.h5f.LIBVER_LATEST, h5py.h5f.LIBVER_LATEST)
    return h5py.h5f.create(bytes(path), h5py.h5f.ACC_TRUNC, fapl=access), access


def _generate(path: Path, family: str, rng: np.random.Generator) -> None:
    """Make independent shape and content strata without recovery imports."""
    if family in ("implicit", "filtered", "compact"):
        file_id, _access = _low_level_file(path)
        if family == "implicit":
            shape, dtype, chunks = (8, 12), np.dtype("<u4"), (2, 3)
            values = rng.integers(0, 2**32, size=shape, dtype=dtype)
        elif family == "filtered":
            shape, dtype, chunks = (32,), np.dtype("<f8"), (32,)
            values = rng.standard_normal(shape).astype(dtype)
        else:
            shape, dtype, chunks = (7,), np.dtype("<u4"), None
            values = rng.integers(0, 2**32, size=shape, dtype=dtype)
        space = h5py.h5s.create_simple(shape, shape)
        creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        if family == "compact":
            creation.set_layout(h5py.h5d.COMPACT)
        else:
            creation.set_chunk(chunks)
            if family == "implicit":
                creation.set_alloc_time(h5py.h5d.ALLOC_TIME_EARLY)
            else:
                creation.set_fletcher32()
                creation.set_deflate(6)
        dataset = h5py.h5d.create(file_id, b"science", h5py.h5t.py_create(dtype),
                                  space, dcpl=creation)
        dataset.write(h5py.h5s.ALL, h5py.h5s.ALL, values)
        dataset.close()
        file_id.close()
        return
    with h5py.File(path, "x", libver="latest") as handle:
        if family == "single":
            values = rng.integers(0, 2**32, size=(4, 4), dtype="<u4")
            handle.create_dataset("science", data=values, chunks=(4, 4))
        elif family == "fixed_array":
            values = rng.integers(0, 2**32, size=(8, 12), dtype="<u4")
            handle.create_dataset("science", data=values, chunks=(2, 3))
        elif family == "paged_fixed_array":
            values = rng.integers(0, 2**32, size=(33, 33), dtype="<u4")
            handle.create_dataset("science", data=values, chunks=(1, 1))
        elif family == "extensible_array":
            values = rng.integers(0, 2**32, size=(20, 5), dtype="<u4")
            handle.create_dataset("science", data=values, chunks=(4, 5), maxshape=(None, 5))
        elif family == "v2_btree":
            values = rng.integers(0, 2**32, size=(12, 15), dtype="<u4")
            handle.create_dataset("science", data=values, chunks=(3, 3), maxshape=(None, None))
        elif family == "sparse_edge":
            selected = handle.create_dataset("science", shape=(11, 7), chunks=(4, 3),
                                             maxshape=(None, 7), dtype="<u4", fillvalue=999)
            selected[:4, :3] = rng.integers(0, 2**32, size=(4, 3), dtype="<u4")
            selected[8:11, 6:7] = rng.integers(0, 2**32, size=(3, 1), dtype="<u4")
        elif family == "contiguous":
            handle.create_dataset("science", data=rng.standard_normal((5, 13)).astype("<f8"))
        elif family == "compound":
            dtype = np.dtype([("detector", "<i4"), ("signal", "<f8"), ("unit", "S6")])
            values = np.empty(17, dtype=dtype)
            values["detector"] = rng.integers(1, 300, size=17)
            values["signal"] = rng.standard_normal(17)
            values["unit"] = b"strain"
            handle.create_dataset("science", data=values)
        elif family == "fixed_string":
            labels = [f"run-{i:04d}".encode("ascii") for i in range(11)]
            handle.create_dataset("science", data=np.asarray(labels, dtype="S12"), chunks=(4,))
        elif family == "vlen":
            values = np.asarray(["alpha", "beta", "gamma"], dtype=object)
            handle.create_dataset("science", data=values, dtype=h5py.string_dtype("utf-8"))
        elif family == "external_raw":
            raw = path.parent / "external.raw"
            raw.write_bytes(rng.integers(0, 256, size=32, dtype="u1").tobytes())
            handle.create_dataset("science", shape=(8,), dtype="<u4",
                                  external=[(raw.name, 0, raw.stat().st_size)])
        elif family == "virtual":
            child = path.parent / "virtual_source.h5"
            with h5py.File(child, "x") as other:
                other.create_dataset("signal", data=rng.integers(0, 100, 12, dtype="<u4"))
            layout = h5py.VirtualLayout(shape=(12,), dtype="<u4")
            layout[:] = h5py.VirtualSource(child.name, "signal", shape=(12,))
            handle.create_virtual_dataset("science", layout, fillvalue=999)
        else:
            raise EvaluationError(f"unknown generated family: {family}")


def _index(source: Path):
    with h5py.File(source, "r") as handle, ModernH5File(source) as reader:
        selected = handle["/science"]
        index = reader.read_index(
            int(h5py.h5o.get_info(selected.id).addr), selected.shape, selected.chunks,
            selected.dtype.itemsize, maxshape=selected.maxshape,
            filters=tuple(selected.id.get_create_plist().get_filter(i)[0]
                          for i in range(selected.id.get_create_plist().get_nfilters())),
        )
        return index, reader.metadata_ranges


def _change_bytes(path: Path, offset: int, previous: bytes, replacement: bytes) -> None:
    if len(previous) != len(replacement) or not previous or previous == replacement:
        raise EvaluationError("mutation must be a changed same-length byte extent")
    with path.open("r+b") as stream:
        stream.seek(offset)
        if stream.read(len(previous)) != previous:
            raise EvaluationError("mutation precondition failed on copied input")
        stream.seek(offset)
        stream.write(replacement)


def _fault(source: Path, damaged: Path, fault: str, seed: int) -> dict[str, Any]:
    """Only the evaluator uses the pristine source to choose grounded sites."""
    shutil.copyfile(source, damaged)
    if fault == "intact":
        return {"kind": fault, "offsets": []}
    rng = random.Random(seed)
    length = source.stat().st_size
    if fault == "signature":
        previous = source.read_bytes()[:1]
        _change_bytes(damaged, 0, previous, bytes((previous[0] ^ (1 << rng.randrange(8)),)))
        return {"kind": fault, "offsets": [0]}
    with h5py.File(source, "r") as handle:
        selected = handle["/science"]
        object_address = int(h5py.h5o.get_info(selected.id).addr)
        info = selected.id.get_chunk_info(rng.randrange(selected.id.get_num_chunks()))
    if fault == "object_checksum":
        offset = object_address + 30
    elif fault == "superblock_checksum":
        offset = 15
    elif fault in ("filtered_payload", "unchecked_payload"):
        offset = int(info.byte_offset) + (0 if fault == "filtered_payload" else rng.randrange(info.size))
    elif fault == "truncated_payload":
        cut = int(info.byte_offset) + info.size // 2
        if not 0 < cut < length:
            raise EvaluationError("truncation does not cut a payload")
        with damaged.open("r+b") as stream:
            stream.truncate(cut)
        return {"kind": fault, "offsets": [cut], "truncated_at": cut}
    elif fault in ("array_checksum", "competing_pointer"):
        index, ranges = _index(source)
        block_start, block_end, _ = next(
            entry for entry in ranges if entry[2] == "fixed-array data block")
        if fault == "array_checksum":
            offset = block_start + 20
        else:
            first, second = index.chunks[:2]
            offset = second.pointer_offset
            old = first.address.to_bytes(8, "little")
            with source.open("rb") as stream:
                stream.seek(offset)
                before = stream.read(8)
            _change_bytes(damaged, offset, before, old)
            with damaged.open("r+b") as stream:
                stream.seek(block_start)
                block = stream.read(block_end - block_start)
                stream.seek(block_end - 4)
                stream.write(lookup3(block[:-4]).to_bytes(4, "little"))
            return {"kind": fault, "offsets": [offset, block_end - 4],
                    "note": "valid array checksum with two coordinates pointing to one payload"}
    else:
        raise EvaluationError(f"unsupported fault: {fault}")
    with source.open("rb") as stream:
        stream.seek(offset)
        previous = stream.read(1)
    _change_bytes(damaged, offset, previous, bytes((previous[0] ^ (1 << rng.randrange(8)),)))
    return {"kind": fault, "offsets": [offset]}


def _grid(shape: tuple[int, ...], chunks: tuple[int, ...] | None):
    if chunks is None:
        return [((0,) * len(shape), tuple(slice(0, x) for x in shape))]
    origins = product(*(range(0, size, width) for size, width in zip(shape, chunks)))
    return [(origin, tuple(slice(p, min(p + step, size))
                           for p, step, size in zip(origin, chunks, shape)))
            for origin in origins]


def _score(source: Path, damaged: Path, output: Path, report_path: Path,
           dataset: str, route: str) -> dict[str, Any]:
    """The oracle checks all accepted regions, including partial edge chunks."""
    errors: list[str] = []
    report = json.loads(report_path.read_text(encoding="utf-8"))
    damaged_hash = digest(damaged)
    if report.get("source", {}).get("sha256_before") != damaged_hash or report["source"].get("sha256_after") != damaged_hash:
        errors.append("report source hash is inconsistent with the unchanged damaged input")
    if report.get("dataset", {}).get("path") != dataset:
        errors.append("report changed the selected dataset")
    exact = wrong = unknown = unchecked = 0
    with h5py.File(source, "r") as original, h5py.File(output, "r") as exported:
        before, after = original[dataset], exported[dataset]
        if (before.shape != after.shape or before.dtype != after.dtype or before.chunks != after.chunks
                or not before.id.get_type().equal(after.id.get_type())):
            errors.append("output shape, datatype, chunks or exact HDF5 datatype changed")
        old_creation, new_creation = before.id.get_create_plist(), after.id.get_create_plist()
        if (route == "export-readable" and
                (old_creation.get_nfilters() != new_creation.get_nfilters()
                or any(old_creation.get_filter(i) != new_creation.get_filter(i)
                       for i in range(old_creation.get_nfilters())))):
            errors.append("output changed the selected filter pipeline")
        native_ranges = {}
        if before.chunks:
            for i in range(before.id.get_num_chunks()):
                info = before.id.get_chunk_info(i)
                native_ranges[tuple(int(x) for x in info.chunk_offset)] = (
                    int(info.byte_offset), int(info.size), int(info.filter_mask)
                )
        if route == "recover":
            if report.get("mode") == "readable_export":
                errors.append("structural route returned a native-readable export")
            status = exported["/_h5reclaim/chunk_status"][...]
        else:
            if report.get("mode") != "readable_export":
                errors.append("native route did not report readable export")
            status = exported["/_h5reclaim/validity"][...] if before.chunks else None
        grid = _grid(before.shape, before.chunks)
        if status is not None and status.size != len(grid):
            errors.append("validity map size differs from the logical chunk grid")
        if status is not None and route == "recover" and not set(status.flat) <= set(range(1, 8)):
            errors.append("structural validity map contains an unknown code")
        accepted_origins: set[tuple[int, ...]] = set()
        for origin, selection in grid:
            key = tuple(p // step for p, step in zip(origin, before.chunks)) if before.chunks else None
            accepted = (status is None or int(status[key]) == 1)
            if not accepted:
                unknown += 1
                continue
            accepted_origins.add(origin)
            if before[selection].tobytes() == after[selection].tobytes():
                exact += 1
            else:
                wrong += 1
                if route == "recover":
                    decisions = report.get("evidence_ledger", {}).get("decisions", [])
                    if any(item.get("integrity") == "not_independently_verified"
                           for item in decisions):
                        unchecked += 1
        if route == "recover":
            seen: set[tuple[int, ...]] = set()
            for mapping in report.get("mappings", []):
                origin = tuple(mapping.get("coordinate", ()))
                if origin in seen or origin not in accepted_origins or origin not in native_ranges:
                    errors.append("recovery mapping is duplicate, unaccepted, or unallocated in reference")
                    continue
                seen.add(origin)
                expected_address, expected_size, _expected_mask = native_ranges[origin]
                if (mapping.get("source_absolute_offset"), mapping.get("size_bytes")) != (
                        expected_address, expected_size):
                    errors.append("accepted mapping asserts an incorrect physical source extent")
            if seen != accepted_origins:
                errors.append("accepted coordinate is missing an index-to-source mapping")
        else:
            records = report.get("dataset", {}).get("source_chunk_records", [])
            if before.chunks:
                found: set[tuple[int, ...]] = set()
                with damaged.open("rb") as stream:
                    for record in records:
                        origin = tuple(record.get("origin", ()))
                        if origin in found or origin not in native_ranges:
                            errors.append("source chunk record is duplicate or unallocated")
                            continue
                        found.add(origin)
                        addr, size, mask = native_ranges[origin]
                        if (record.get("address"), record.get("stored_bytes"), record.get("filter_mask")) != (
                                addr, size, mask):
                            errors.append("readable export claims an incorrect physical chunk extent or mask")
                        stream.seek(addr)
                        if hashlib.sha256(stream.read(size)).hexdigest() != record.get("raw_sha256"):
                            errors.append("readable export raw chunk hash does not match input bytes")
                if found != accepted_origins:
                    errors.append("readable export allocation records disagree with validity map")
            elif old_creation.get_layout() == h5py.h5d.CONTIGUOUS:
                start = before.id.get_offset()
                expected = {"start": start, "end_exclusive": start + before.nbytes}
                if report.get("dataset", {}).get("source_contiguous_byte_range") != expected:
                    errors.append("contiguous source extent differs from independent HDF5 address")
        embedded = exported["/_h5reclaim/report_json"][()]
        if isinstance(embedded, bytes):
            embedded = embedded.decode("utf-8")
        if json.loads(embedded) != report:
            errors.append("embedded report differs from separately published report")
    return {"decision": "output", "exact_regions": exact, "wrong_historical_regions": wrong,
            "unknown_regions": unknown, "wrong_with_unverified_integrity": unchecked,
            "evaluation_errors": errors}


def _cases(trials: int) -> list[Case]:
    fixed = [
        Case("real_gwosc_4khz", "real_4khz", "recover", "intact", "exact", "/strain/Strain"),
        Case("single", "single", "recover", "intact", "exact"),
        Case("filtered_single", "filtered", "recover", "intact", "exact"),
        Case("implicit", "implicit", "recover", "intact", "exact"),
        Case("fixed_array", "fixed_array", "recover", "intact", "exact"),
        Case("paged_fixed_array", "paged_fixed_array", "recover", "intact", "safe"),
        Case("extensible_array", "extensible_array", "recover", "intact", "safe"),
        Case("v2_btree", "v2_btree", "recover", "intact", "safe"),
        Case("sparse_edge_structural", "sparse_edge", "recover", "intact", "safe"),
        Case("sparse_edge_readable", "sparse_edge", "export-readable", "intact", "partial"),
        Case("paged_readable", "paged_fixed_array", "export-readable", "intact", "exact"),
        Case("extensible_readable", "extensible_array", "export-readable", "intact", "exact"),
        Case("v2_btree_readable", "v2_btree", "export-readable", "intact", "exact"),
        Case("compact", "compact", "export-readable", "intact", "exact"),
        Case("contiguous", "contiguous", "export-readable", "intact", "exact"),
        Case("compound", "compound", "export-readable", "intact", "exact"),
        Case("fixed_string", "fixed_string", "export-readable", "intact", "exact"),
        Case("vlen", "vlen", "export-readable", "intact", "refuse"),
        Case("external_raw", "external_raw", "export-readable", "intact", "refuse"),
        Case("virtual", "virtual", "export-readable", "intact", "refuse"),
    ]
    variations = [
        ("single", "signature", "refuse"),
        ("single", "superblock_checksum", "refuse"),
        ("single", "object_checksum", "refuse"),
        ("fixed_array", "array_checksum", "refuse"),
        ("fixed_array", "competing_pointer", "refuse"),
        ("filtered", "filtered_payload", "unavailable"),
        ("single", "unchecked_payload", "unchecked_payload"),
        ("single", "truncated_payload", "refuse"),
    ]
    for trial in range(trials):
        for family, fault, expectation in variations:
            fixed.append(Case(f"trial_{trial:03d}_{family}_{fault}", family, "recover", fault,
                              expectation))
    return fixed


def _run_case(case: Case, sources: dict[str, Path], workspace: Path, seed: int,
              python: str) -> dict[str, Any]:
    source = sources[case.family]
    folder = workspace / "cases" / case.name
    folder.mkdir(parents=True)
    damaged, output, report_path = (folder / name for name in ("input.h5", "output.h5", "report.json"))
    case_seed = int.from_bytes(hashlib.sha256(f"{seed}:{case.name}".encode()).digest()[:8], "big")
    mutation = _fault(source, damaged, case.fault, case_seed)
    pristine_hash, input_hash = digest(source), digest(damaged)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    try:
        finished = subprocess.run(
            [python, "-m", "h5reclaim", case.route, str(damaged), "--dataset", case.dataset,
             "--output", str(output), "--report", str(report_path)],
            cwd=folder, env=environment, capture_output=True, text=True,
            timeout=90, check=False,
        )
        if finished.returncode == 2 and not output.exists() and not report_path.exists():
            observed: dict[str, Any] = {
                "decision": "refused", "exact_regions": 0, "wrong_historical_regions": 0,
                "unknown_regions": 0, "wrong_with_unverified_integrity": 0,
                "evaluation_errors": [], "reason": (finished.stderr or finished.stdout)[-350:],
            }
        elif finished.returncode == 0 and output.is_file() and report_path.is_file():
            observed = _score(source, damaged, output, report_path, case.dataset, case.route)
        else:
            observed = {
                "decision": "invalid", "exact_regions": 0, "wrong_historical_regions": 0,
                "unknown_regions": 0, "wrong_with_unverified_integrity": 0,
                "evaluation_errors": [f"exit {finished.returncode}; published output={output.exists()}, "
                                      f"report={report_path.exists()}; {(finished.stderr or finished.stdout)[-350:]}"],
            }
    except subprocess.TimeoutExpired:
        observed = {"decision": "timeout", "exact_regions": 0, "wrong_historical_regions": 0,
                    "unknown_regions": 0, "wrong_with_unverified_integrity": 0,
                    "evaluation_errors": ["90-second child process timeout"]}
    if digest(source) != pristine_hash or digest(damaged) != input_hash:
        observed.setdefault("evaluation_errors", []).append("pristine or damaged input changed during trial")
    decision = observed["decision"]
    if case.expectation == "exact":
        expected = decision == "output" and observed["unknown_regions"] == observed["wrong_historical_regions"] == 0
    elif case.expectation == "partial":
        expected = (decision == "output" and observed["unknown_regions"] > 0
                    and observed["wrong_historical_regions"] == 0 and observed["exact_regions"] > 0)
    elif case.expectation == "unavailable":
        expected = (decision == "output" and observed["unknown_regions"] > 0
                    and observed["wrong_historical_regions"] == 0)
    elif case.expectation == "refuse":
        expected = decision == "refused"
    elif case.expectation == "safe":
        expected = decision == "refused" or (decision == "output" and observed["exact_regions"] > 0
                                              and observed["wrong_historical_regions"] == 0)
    else:
        expected = (decision == "output" and observed["wrong_historical_regions"] == 1
                    and observed["wrong_with_unverified_integrity"] == 1)
    return {
        "name": case.name, "family": case.family, "route": case.route,
        "damage": mutation, "expected_behavior": case.expectation,
        "pristine_sha256": pristine_hash, "input_sha256": input_hash,
        "input": str(damaged), "observed": observed,
        "passed": expected and not observed["evaluation_errors"],
    }


def run(workspace: Path, *, seed: int = 20260927, trials: int = 2,
        python: str = sys.executable) -> dict[str, Any]:
    if seed < 0 or seed >= 2**64 or not 1 <= trials <= 10:
        raise EvaluationError("seed must be unsigned 64-bit and trials must be 1..10")
    if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
        raise EvaluationError("work directory must be new or empty")
    workspace.mkdir(parents=True, exist_ok=True)
    originals = _verify_corpus_files()
    source_dir = workspace / "truth"
    source_dir.mkdir()
    generator = np.random.default_rng(seed)
    families = sorted({case.family for case in _cases(trials) if case.family != "real_4khz"})
    sources: dict[str, Path] = {}
    for family in families:
        source = source_dir / f"{family}.h5"
        _generate(source, family, generator)
        sources[family] = source
    real_entry = originals["gwosc_gw150914_h1_strain"]
    sources["real_4khz"] = ROOT / "corpus" / real_entry["path"]
    cases = [_run_case(case, sources, workspace, seed, python) for case in _cases(trials)]
    result = {
        "schema_version": 1, "experiment": "stratified generated layouts and adversarial HDF5 faults",
        "seed": seed, "trials": trials, "verified_authentic_originals": len(originals),
        "generated_layout_families": families, "case_count": len(cases),
        "exact_accepted_regions": sum(c["observed"]["exact_regions"] for c in cases),
        "historical_mismatch_regions": sum(c["observed"]["wrong_historical_regions"] for c in cases),
        "unverified_mismatch_regions": sum(c["observed"]["wrong_with_unverified_integrity"] for c in cases),
        "unresolved_regions": sum(c["observed"]["unknown_regions"] for c in cases),
        "safe_refusals": sum(c["observed"]["decision"] == "refused" for c in cases),
        "all_cases_passed": all(c["passed"] for c in cases), "cases": cases,
        "limits": (
            "Generated current files and controlled faults plus one intact authentic GWOSC file. "
            "The pristine truth is supplied to the evaluator, not to the recovery subprocess. "
            "A changed unfiltered payload remains structurally attributable but is labeled without "
            "independent integrity; its historical value can be wrong. Other generated cases are "
            "correlated and not representative of naturally damaged HDF5 files. This is not a "
            "population success estimate or evidence of a 99% recovery rate."
        ),
    }
    (workspace / "stratified.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def render(report: dict[str, Any]) -> str:
    lines = [
        "H5Reclaim stratified layout and damage evaluation",
        f"Seed {report['seed']} | {report['case_count']} cases | "
        f"{'PASS' if report['all_cases_passed'] else 'FAIL'}",
        f"Exact accepted regions: {report['exact_accepted_regions']} | unresolved: "
        f"{report['unresolved_regions']} | safe refusals: {report['safe_refusals']}",
        f"Historical mismatches in unchecksummed data: {report['unverified_mismatch_regions']} "
        f"(all mismatches: {report['historical_mismatch_regions']})",
        "",
    ]
    for case in report["cases"]:
        observed = case["observed"]
        lines.append(f"  {'PASS' if case['passed'] else 'FAIL'} {case['name']}: "
                     f"{observed['decision']}, {observed['exact_regions']} exact, "
                     f"{observed['unknown_regions']} unknown, "
                     f"{observed['wrong_historical_regions']} historical mismatches")
    lines.extend(["", "These constructed cases do not measure real-world recovery probability.",
                  "The full independent scoring is saved to stratified.json; add --json to print it."])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260927)
    parser.add_argument("--trials", type=int, default=2, help="1..10 seeded variations per fault")
    parser.add_argument("--work-dir", type=Path, help="new or empty retained evaluation directory")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    folder = args.work_dir or Path(tempfile.mkdtemp(prefix="h5reclaim-stratified-"))
    try:
        report = run(folder, seed=args.seed, trials=args.trials, python=args.python)
    except (EvaluationError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        parser.exit(2, f"stratified evaluation failed: {exc}\nwork directory: {folder}\n")
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
    print(f"Work directory: {folder}", file=sys.stderr)
    return 0 if report["all_cases_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
