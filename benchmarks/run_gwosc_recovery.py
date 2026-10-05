"""Controlled broken-link recovery on an unmodified, real GWOSC HDF5 layout.

The bundled GW150914 Hanford strain file is the independent reference. This
trial changes one verified B-tree pointer in a private byte-for-byte copy,
passes only that damaged copy to the recovery subprocess, and compares every
recovered float64 bit pattern at its original sample coordinate. The trial uses
the authentic scientific layout with one declared controlled index-link mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import h5py
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
# This bundled trial can run from a source checkout without an editable install.
sys.path.insert(0, str(ROOT / "src"))
FILENAME = "H-H1_GWOSC_16KHZ_R1-1126259447-32.hdf5"
SOURCE_SHA256 = "81040e1ecfaf40ffe15a5efc59dbc3a888653162f1613425f1e68d0828dd1b97"
DATASET = "/strain/Strain"
STATUS = {
    1: "recovered",
    2: "allocation_unknown",
    3: "ambiguous",
    4: "unavailable",
    5: "unsupported",
    6: "decode_failed",
}


class TrialError(RuntimeError):
    """The evidence or result is inconsistent with a valid controlled trial."""


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bits(values: np.ndarray) -> np.ndarray:
    """Compare IEEE-754 payloads, including NaNs and signed zero, exactly."""
    return np.ascontiguousarray(values, dtype="<f8").view("<u8")


def _source_path() -> Path:
    return ROOT / "corpus" / "files" / FILENAME


def _inspect_original(source: Path) -> dict[str, Any]:
    from h5reclaim.format import H5File

    if not source.is_file() or _hash(source) != SOURCE_SHA256:
        raise TrialError("the pinned, authentic GWOSC source is missing or differs from its SHA-256")
    with h5py.File(source, "r") as handle, H5File(source) as raw:
        dataset = handle[DATASET]
        if (
            dataset.ndim != 1 or dataset.dtype.str != "<f8"
            or dataset.shape != (524288,) or dataset.chunks != (4096,)
            or dataset.maxshape != dataset.shape or dataset.id.get_num_chunks() != 128
        ):
            raise TrialError("GWOSC strain dataset differs from the pinned scientific layout")
        filters = dataset.id.get_create_plist()
        filter_ids = [filters.get_filter(i)[0] for i in range(filters.get_nfilters())]
        if filter_ids != [h5py.h5z.FILTER_FLETCHER32, h5py.h5z.FILTER_DEFLATE]:
            raise TrialError(f"unexpected GWOSC filter pipeline: {filter_ids}")
        selected_address = int(h5py.h5o.get_info(dataset.id).addr)
        layout = raw.read_dataset_layout(selected_address, rank=1)
        if layout.chunk_shape != dataset.chunks or layout.element_size != 8:
            raise TrialError("selected object's raw layout differs from HDF5 metadata")
        root = raw.read_tree(layout.root_address, rank=1, element_size=8)
        if root.level != 1 or len(root.entries) < 3:
            raise TrialError("selected dataset has no recoverable interior level-one B-tree link")
        walk = raw.walk_tree(layout.root_address, rank=1, element_size=8)
        if walk.broken_links or len(walk.nodes) != len(root.entries) + 1:
            raise TrialError("original chunk index is damaged or incomplete")
        if any(entry.address is None for entry in root.entries):
            raise TrialError("original root has an undefined child pointer")
        leaves = [
            raw.read_tree(entry.address, rank=1, element_size=8)
            for entry in root.entries
        ]
        if any(leaf.level != 0 for leaf in leaves):
            raise TrialError("root has a child other than a leaf")

        # The selected object's root owns these chunk records. Compare all of
        # them with the independent HDF5 library before altering any bytes.
        parsed: dict[int, tuple[int, int, int]] = {}
        for leaf in leaves:
            for entry in leaf.entries:
                if entry.address is None or len(entry.key.offsets) != 2:
                    raise TrialError("malformed raw leaf entry")
                coordinate, element_offset = entry.key.offsets
                if (
                    element_offset != 0 or coordinate < 0
                    or coordinate >= dataset.shape[0]
                    or coordinate % dataset.chunks[0]
                    or coordinate in parsed
                ):
                    raise TrialError("duplicate, unaligned, or out-of-range raw chunk")
                parsed[coordinate] = (
                    raw.absolute(entry.address), entry.key.stored_size,
                    entry.key.filter_mask,
                )
        if len(parsed) != dataset.id.get_num_chunks():
            raise TrialError("raw index does not contain every allocated scientific chunk")
        for i in range(dataset.id.get_num_chunks()):
            info = dataset.id.get_chunk_info(i)
            if len(info.chunk_offset) != 1:
                raise TrialError("HDF5 returned an unexpected chunk coordinate")
            coordinate = info.chunk_offset[0]
            if parsed.get(coordinate) != (info.byte_offset, info.size, info.filter_mask):
                raise TrialError(f"raw index and HDF5 disagree at sample {coordinate}")
        ranges = sorted((addr, addr + size) for addr, size, _mask in parsed.values())
        if any(left_end > right_start for (_left, left_end), (right_start, _right)
               in zip(ranges, ranges[1:])):
            raise TrialError("two scientific chunk payloads overlap")

        chosen = None
        for index in range(1, len(leaves) - 1):
            left, middle, right = leaves[index - 1:index + 2]
            if (
                middle.entries and left.right_sibling == middle.address
                and middle.left_sibling == left.address
                and middle.right_sibling == right.address
                and right.left_sibling == middle.address
            ):
                chosen = index
                break
        if chosen is None:
            raise TrialError("no interior leaf has two reciprocal surviving neighbors")
        selected = root.entries[chosen]
        affected = sorted(entry.key.offsets[0] for entry in leaves[chosen].entries)
        return {
            "source_object_address": selected_address,
            "btree_root_address": root.address,
            "root_child_count": len(root.entries),
            "child_index": chosen,
            "leaf_address": leaves[chosen].address,
            "left_leaf_address": leaves[chosen - 1].address,
            "right_leaf_address": leaves[chosen + 1].address,
            "pointer_offset": selected.pointer_offset,
            "pointer_size": raw.superblock.offset_size,
            "pointer_value": selected.address,
            "affected_offsets": affected,
            "chunk_shape": list(dataset.chunks),
            "shape": list(dataset.shape),
            "chunk_count": len(parsed),
        }


def _damage_copy(source: Path, damaged: Path, details: dict[str, Any]) -> dict[str, Any]:
    original = source.read_bytes()
    offset, width = details["pointer_offset"], details["pointer_size"]
    pointer = details["pointer_value"].to_bytes(width, "little")
    if original[offset:offset + width] != pointer:
        raise TrialError("selected pointer bytes differ from parsed root entry")
    undefined = b"\xff" * width
    if pointer == undefined:
        raise TrialError("selected pointer is already undefined")
    changed = original[:offset] + undefined + original[offset + width:]
    if len(changed) != len(original):
        raise TrialError("controlled mutation unexpectedly changed file length")
    with damaged.open("xb") as output:
        output.write(changed)
    if _hash(source) != SOURCE_SHA256:
        raise TrialError("the original evidence changed during controlled damage")
    return {
        "origin": "one verified root-to-interior-leaf pointer changed in a copy",
        "modified_byte_offsets": [offset + i for i in range(width) if pointer[i] != 0xff],
        "pointer_absolute_byte_offset": offset,
        "pointer_before_hex": pointer.hex(),
        "pointer_after_hex": undefined.hex(),
        "source_sha256": SOURCE_SHA256,
        "damaged_sha256": _hash(damaged),
        "affected_chunk_offsets": details["affected_offsets"],
    }


def _native_check(source: Path, damaged: Path, details: dict[str, Any]) -> dict[str, int]:
    affected = set(details["affected_offsets"])
    count = details["chunk_count"]
    size = details["chunk_shape"][0]
    failed = 0
    affected_exact = 0
    unaffected_exact = 0
    with h5py.File(source, "r") as good_file, h5py.File(damaged, "r") as bad_file:
        good = good_file[DATASET]
        bad = bad_file[DATASET]
        for i in range(count):
            start = i * size
            truth = _bits(good[start:start + size])
            try:
                actual = _bits(bad[start:start + size])
            except (OSError, RuntimeError, ValueError):
                if start not in affected:
                    raise TrialError(f"normally reachable chunk {i} became unreadable")
                failed += 1
            else:
                if np.array_equal(actual, truth):
                    if start in affected:
                        affected_exact += 1
                    else:
                        unaffected_exact += 1
                elif start in affected:
                    failed += 1
                else:
                    raise TrialError(f"normally reachable chunk {i} changed bits")
    if failed == 0 or affected_exact or unaffected_exact + failed != count:
        raise TrialError("controlled damage did not isolate an unreadable/incorrect leaf")
    return {
        "native_failed_or_incorrect_chunks": failed,
        "native_affected_exact_chunks": affected_exact,
        "native_unaffected_exact_chunks": unaffected_exact,
    }


def _run_recovery(damaged: Path, output: Path, report: Path, *, python: str) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    command = [
        python, "-m", "h5reclaim", "recover", str(damaged),
        "--dataset", DATASET, "--output", str(output), "--report", str(report),
    ]
    try:
        completed = subprocess.run(
            command, cwd=output.parent, env=environment,
            capture_output=True, text=True, timeout=180, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TrialError("recovery subprocess timed out after 180 seconds") from exc
    if completed.returncode:
        raise TrialError(
            f"recovery subprocess exited {completed.returncode}: "
            f"{completed.stderr[-2000:]} {completed.stdout[-2000:]}"
        )


def _evaluate(
    source: Path, damaged: Path, output: Path, report_path: Path,
    details: dict[str, Any], native: dict[str, int],
) -> dict[str, Any]:
    expected_count = details["chunk_count"]
    chunk_size = details["chunk_shape"][0]
    affected = set(details["affected_offsets"])
    with h5py.File(source, "r") as good_file, h5py.File(output, "r") as result_file:
        clean = good_file[DATASET]
        recovered = result_file[DATASET]
        statuses = result_file["/_h5reclaim/chunk_status"]
        if (
            recovered.shape != clean.shape or recovered.dtype != clean.dtype
            or statuses.shape != (expected_count,) or statuses.dtype != np.dtype("u1")
        ):
            raise TrialError("output dtype, shape, or embedded status map differs from original")
        status = statuses[...]
        source_attributes = set(clean.attrs)
        safe_attributes = {
            name for name in source_attributes
            if not (
                clean.attrs.get_id(name).get_type().get_class() == h5py.h5t.STRING
                and clean.attrs.get_id(name).get_type().is_variable_str()
            )
        }
        omitted_attributes = source_attributes - safe_attributes
        if not safe_attributes <= set(recovered.attrs) or omitted_attributes & set(recovered.attrs):
            raise TrialError("output attribute selection differs from bounded-copy policy")
        for name in safe_attributes:
            if not np.array_equal(np.asarray(clean.attrs[name]), np.asarray(recovered.attrs[name])):
                raise TrialError(f"output changed scientific attribute {name!r}")
        embedded = result_file["/_h5reclaim/report_json"][()]
        if isinstance(embedded, bytes):
            embedded = embedded.decode("utf-8")
        if not isinstance(embedded, str):
            raise TrialError("embedded report is not UTF-8 JSON")
        codes = set(int(value) for value in np.unique(status))
        if not codes <= STATUS.keys():
            raise TrialError(f"unknown output status codes: {sorted(codes - STATUS.keys())}")
        counts = Counter(STATUS[int(value)] for value in status)
        library_info = {
            i: clean.id.get_chunk_info(i) for i in range(expected_count)
        }
        wrong_coordinates: list[int] = []
        for i in range(expected_count):
            if int(status[i]) != 1:
                continue
            start = i * chunk_size
            original_bits = _bits(clean[start:start + chunk_size])
            recovered_bits = _bits(recovered[start:start + chunk_size])
            if not np.array_equal(original_bits, recovered_bits):
                wrong_coordinates.append(start)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    if json.loads(embedded) != report:
        raise TrialError("embedded and external recovery reports differ")
    source_hash = _hash(source)
    damaged_hash = _hash(damaged)
    reported_source = report.get("source", {})
    if (
        source_hash != SOURCE_SHA256
        or reported_source.get("sha256_before") != damaged_hash
        or reported_source.get("sha256_after") != damaged_hash
        or reported_source.get("size_bytes") != damaged.stat().st_size
    ):
        raise TrialError("source preservation or recovery report hash did not verify")
    selected = report.get("dataset", {})
    if (
        selected.get("path") != DATASET
        or selected.get("shape") != details["shape"]
        or selected.get("chunks") != details["chunk_shape"]
        or selected.get("chunk_grid") != [expected_count]
        or selected.get("dtype") != "<f8"
        or selected.get("filters") != [h5py.h5z.FILTER_FLETCHER32, h5py.h5z.FILTER_DEFLATE]
        or selected.get("object_address") != details["source_object_address"]
        or set(selected.get("attributes_copied", [])) != safe_attributes
        or set(selected.get("attributes_omitted", [])) != omitted_attributes
    ):
        raise TrialError("recovery report selected different dataset metadata")
    index = report.get("index", {})
    if (
        report.get("execution_state") != "finished"
        or report.get("outcome") != "complete"
        or index.get("root_address") != details["btree_root_address"]
        or index.get("root_level") != 1
        or index.get("broken_links") != 1
        or index.get("reachable_leaves") != details["root_child_count"] - 1
        or not report.get("metadata_note")
        or not report.get("integrity_note")
        or report.get("failed_chunks") != []
        or report.get("unresolved_links") != []
    ):
        raise TrialError("recovery report omitted or contradicted scientific evidence")
    for label in STATUS.values():
        if report.get("counts", {}).get(label) != counts[label]:
            raise TrialError(f"report count disagrees with status map for {label}")
    if report.get("complete") is not (counts["recovered"] == expected_count):
        raise TrialError("report completeness disagrees with status map")
    mappings = report.get("mappings", [])
    seen: set[int] = set()
    reconstructed = 0
    for mapping in mappings:
        index = mapping.get("chunk_index")
        if (
            not isinstance(index, list) or len(index) != 1
            or type(index[0]) is not int
            or index[0] < 0 or index[0] >= expected_count
            or index[0] in seen
            or mapping.get("coordinate") != [index[0] * chunk_size]
            or int(status[index[0]]) != 1
            or mapping.get("integrity") != "fletcher32_verified"
            or mapping.get("source_absolute_offset") != library_info[index[0]].byte_offset
            or mapping.get("size_bytes") != library_info[index[0]].size
        ):
            raise TrialError("invalid, duplicated, or unverified recovered chunk mapping")
        seen.add(index[0])
        is_detached = index[0] * chunk_size in affected
        route = mapping.get("route")
        if route != ("reconstructed_link" if is_detached else "intact_tree"):
            raise TrialError("recovered chunk has an inconsistent evidence route")
        reconstructed += is_detached
    if seen != {i for i in range(expected_count) if int(status[i]) == 1}:
        raise TrialError("mapping coordinates and status map disagree")
    if wrong_coordinates:
        raise TrialError(f"recovery made wrong-bit claims at sample offsets {wrong_coordinates[:10]}")
    if counts["recovered"] != expected_count or reconstructed != len(affected):
        raise TrialError("trial did not recover every original chunk and detached coordinate")
    if report.get("reconstructed_chunks") != reconstructed:
        raise TrialError("reported reconstructed count disagrees with mappings")
    return {
        "schema_version": 1,
        "experiment": "real GWOSC H1 strain, controlled broken root child pointer",
        "authentic_source_controlled_damage_recovery": True,
        "derived_layout": False,
        "controlled_damage": True,
        "dataset": DATASET,
        "original_file_sha256_before": SOURCE_SHA256,
        "original_file_sha256_after": source_hash,
        "damaged_file_sha256_after_recovery": damaged_hash,
        "original_file_preserved": True,
        "damaged_copy_preserved": True,
        "total_chunks": expected_count,
        "recovered_chunks": counts["recovered"],
        "reconstructed_link_chunks": reconstructed,
        "native_failed_or_incorrect_chunks": native["native_failed_or_incorrect_chunks"],
        "wrong_bit_chunks": len(wrong_coordinates),
        "missing_region_chunks": expected_count - counts["recovered"],
        "every_original_coordinate_exact": True,
        "scientific_dataset_attributes_preserved": sorted(safe_attributes),
        "scientific_dataset_attributes_omitted": sorted(omitted_attributes),
        "status_counts": {label: counts[label] for label in STATUS.values()},
        "limits": "One controlled pointer mutation in the authentic GWOSC H1 strain file, independently scored for exact sample bits and coordinates.",
    }


def run_trial(work_dir: Path, *, python: str = sys.executable) -> dict[str, Any]:
    source = _source_path().resolve()
    work_dir = work_dir.resolve()
    if work_dir.exists() and any(work_dir.iterdir()):
        raise TrialError(f"work directory must be new or empty: {work_dir}")
    truth = work_dir / "truth"
    inputs = work_dir / "inputs"
    results = work_dir / "results"
    for folder in (truth, inputs, results):
        folder.mkdir(parents=True, exist_ok=False)
    details = _inspect_original(source)
    damaged = inputs / "damaged.hdf5"
    output = results / "recovered.hdf5"
    report_path = results / "recovery.json"
    mutation = _damage_copy(source, damaged, details)
    native = _native_check(source, damaged, details)
    (truth / "mutation.json").write_text(
        json.dumps({**mutation, **native, "original_metadata": details}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    before_recovery = _hash(damaged)
    _run_recovery(damaged, output, report_path, python=python)
    if _hash(damaged) != before_recovery or _hash(source) != SOURCE_SHA256:
        raise TrialError("recovery changed the original scientific file or damaged evidence")
    summary = _evaluate(source, damaged, output, report_path, details, native)
    summary["work_dir"] = str(work_dir)
    (truth / "evaluation.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def _human_summary(summary: dict[str, Any]) -> str:
    """Present the scored result without making readers interpret JSON keys."""
    total = summary["total_chunks"]
    native = summary["native_failed_or_incorrect_chunks"]
    reconstructed = summary["reconstructed_link_chunks"]
    directory = Path(summary["work_dir"])
    attributes = summary["scientific_dataset_attributes_preserved"]
    omitted = summary["scientific_dataset_attributes_omitted"]
    return "\n".join([
        "H5Reclaim | GWOSC controlled recovery trial",
        "=" * 43,
        "RESULT: PASS",
        "",
        "Source: original GW150914 Hanford H1 strain, 16 kHz",
        "Damage: one index pointer changed in a separate copy; original untouched",
        "",
        f"Normal HDF5 reads:  {native}/{total} chunks unreadable or incorrect",
        f"H5Reclaim output:   {summary['recovered_chunks']}/{total} chunks recovered",
        f"Detached branch:    {reconstructed}/{native} affected chunks reconstructed",
        f"Value comparison:   {total - summary['wrong_bit_chunks']}/{total} exact float64 bit matches",
        f"Unknown regions:    {summary['missing_region_chunks']}",
        f"Science attributes: {len(attributes)} preserved and checked; {len(omitted)} heap-backed strings omitted",
        "Chunk checksums:    Fletcher32 verified for every exported chunk",
        "Source files:       original and damaged copy unchanged during recovery",
        "",
        "Trial files:",
        f"  Recovered data: {directory / 'results' / 'recovered.hdf5'}",
        f"  Chunk status:   /_h5reclaim/chunk_status in recovered data",
        f"  Recovery report: {directory / 'results' / 'recovery.json'}",
        f"  Full evaluation: {directory / 'truth' / 'evaluation.json'}",
        "",
        "Scope: controlled damage to one authentic file, not proof of general repair.",
        "For machine-readable output, rerun with --json.",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, help="new or empty directory for retained trial files")
    parser.add_argument("--python", default=sys.executable, help="interpreter for the recovery subprocess")
    parser.add_argument("--json", action="store_true", help="print the machine-readable evaluation summary")
    args = parser.parse_args()
    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="h5reclaim-gwosc-"))
    try:
        summary = run_trial(work_dir, python=args.python)
    except (TrialError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(2, f"GWOSC trial failed: {exc}\nwork directory: {work_dir}\n")
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else _human_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
