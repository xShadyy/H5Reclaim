"""Score the public rescue command against independent generated HDF5 truth.

The denominator is the declared matrix below, not an estimate of field-wide
recovery probability. Pristine sources are never passed to the recovery CLI.
Refusals and empty outputs contribute zero useful damaged-file recoveries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
from run_whole_file_corpus import equal_values  # noqa: E402


FAMILIES = (
    "contiguous", "fixed_array", "extensible_array", "v2_btree", "rank5",
    "sparse_fill", "big_endian", "complex", "fixed_compound", "variable_compound",
    "fixed_string", "variable_string", "ragged", "enum", "scalar", "empty",
    "null", "object_reference", "region_reference", "compact", "lzf", "scaleoffset",
    "legacy_chunked",
)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def source_fingerprint() -> dict[str, Any]:
    """Identify the tested code even before the release changes are committed."""
    paths = sorted((ROOT / "src/h5reclaim").glob("*.py")) + [
        ROOT / "pyproject.toml", Path(__file__), ROOT / "benchmarks/run_whole_file_corpus.py"]
    manifest = "".join(f"{path.relative_to(ROOT).as_posix()} {digest(path)}\n" for path in sorted(paths))
    return {"sha256": hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
        "files": len(paths), "method": "SHA-256 of sorted relative-path plus file-SHA-256 lines for src/h5reclaim/*.py, pyproject and both evaluator modules"}


def metadata_checksum(data: bytes) -> int:
    """Jenkins lookup3 for assembling a closed-writer status fixture.

    The original checksum written independently by HDF5 is checked before this
    checksum is used. This utility never decides whether recovered data is exact.
    """
    mask = 0xFFFFFFFF
    def rotate(value, width):
        return ((value << width) | (value >> (32 - width))) & mask
    a = b = c = (0xDEADBEEF + len(data)) & mask
    position = 0
    while len(data) - position > 12:
        a = (a + int.from_bytes(data[position:position + 4], "little")) & mask
        b = (b + int.from_bytes(data[position + 4:position + 8], "little")) & mask
        c = (c + int.from_bytes(data[position + 8:position + 12], "little")) & mask
        a = ((a - c) & mask) ^ rotate(c, 4)
        c = (c + b) & mask
        b = ((b - a) & mask) ^ rotate(a, 6)
        a = (a + c) & mask
        c = ((c - b) & mask) ^ rotate(b, 8)
        b = (b + a) & mask
        a = ((a - c) & mask) ^ rotate(c, 16)
        c = (c + b) & mask
        b = ((b - a) & mask) ^ rotate(a, 19)
        a = (a + c) & mask
        c = ((c - b) & mask) ^ rotate(b, 4)
        b = (b + a) & mask
        position += 12
    tail = data[position:]
    if not tail:
        return c
    padded = tail.ljust(12, b"\0")
    a = (a + int.from_bytes(padded[:4], "little")) & mask
    b = (b + int.from_bytes(padded[4:8], "little")) & mask
    c = (c + int.from_bytes(padded[8:], "little")) & mask
    c = ((c ^ b) - rotate(b, 14)) & mask
    a = ((a ^ c) - rotate(c, 11)) & mask
    b = ((b ^ a) - rotate(a, 25)) & mask
    c = ((c ^ b) - rotate(b, 16)) & mask
    a = ((a ^ c) - rotate(c, 4)) & mask
    b = ((b ^ a) - rotate(a, 14)) & mask
    return ((c ^ b) - rotate(b, 24)) & mask


def generate(path: Path, family: str, seed: int) -> None:
    """Write fixtures using h5py only, without H5Reclaim recovery code."""
    rng = np.random.default_rng(seed)
    with h5py.File(path, "x", libver="earliest" if family == "legacy_chunked" else "latest") as handle:
        handle.attrs["experiment"] = "H5Reclaim independently scored release panel"
        options: dict[str, Any] = {}
        values: Any = rng.integers(-1000, 1000, size=(11, 7), dtype="i4")
        if family in ("fixed_array", "extensible_array", "v2_btree", "legacy_chunked"):
            options.update(chunks=(4, 3), compression="gzip", fletcher32=True)
            if family == "extensible_array":
                options["maxshape"] = (None, 7)
            elif family == "v2_btree":
                options["maxshape"] = (None, None)
        elif family == "rank5":
            values = rng.standard_normal((2, 2, 2, 2, 4))
            options.update(chunks=(1, 1, 1, 1, 4), compression="gzip", fletcher32=True)
        elif family == "sparse_fill":
            dataset = handle.create_dataset("science", shape=(11, 7), chunks=(4, 3),
                dtype="i4", fillvalue=-99, compression="gzip", fletcher32=True)
            dataset[:4, :3] = values[:4, :3]
            dataset[8:, 6:] = values[8:, 6:]
            dataset.attrs["units"] = "arbitrary"
            return
        elif family == "big_endian":
            values = values.astype(">i4")
            options.update(chunks=(4, 3), compression="gzip", fletcher32=True)
        elif family == "complex":
            values = rng.standard_normal(19) + 1j * rng.standard_normal(19)
            options.update(chunks=(4,), compression="gzip", fletcher32=True)
        elif family in ("fixed_compound", "variable_compound"):
            label = "S12" if family == "fixed_compound" else h5py.string_dtype("utf-8")
            dtype = np.dtype([("id", ">i4"), ("signal", "f8", (2,)), ("label", label)])
            values = np.empty(13, dtype=dtype)
            values["id"] = np.arange(13)
            values["signal"] = rng.standard_normal((13, 2))
            values["label"] = [f"sample-{i}".encode() for i in range(13)]
            options.update(chunks=(4,))
            if family == "fixed_compound":
                options.update(compression="gzip", fletcher32=True)
        elif family == "fixed_string":
            values = np.array([f"row-{i}".encode() for i in range(13)], dtype="S12")
            options.update(chunks=(4,), compression="gzip", fletcher32=True)
        elif family == "variable_string":
            values = np.array(["alpha", "βeta", "", "science", "測定"], dtype=object)
            options.update(dtype=h5py.string_dtype("utf-8"), chunks=(2,))
        elif family == "ragged":
            values = np.empty(6, dtype=object)
            for index in range(6):
                values[index] = np.arange(index, dtype="f8")
            options.update(dtype=h5py.vlen_dtype(np.dtype("f8")), chunks=(2,))
        elif family == "enum":
            values = np.array([0, 1, 2, 2, 0, 1, 2], dtype=h5py.enum_dtype(
                {"idle": 0, "running": 1, "stopped": 2}, basetype="u1"))
            options.update(chunks=(3,), fletcher32=True)
        elif family == "scalar":
            values = np.array(3.141592653589793)
        elif family == "empty":
            values = np.array([], dtype="f8")
            options.update(chunks=(8,), maxshape=(None,))
        elif family == "null":
            values = h5py.Empty("f8")
        elif family in ("object_reference", "region_reference"):
            target = handle.create_dataset("target", data=np.arange(12).reshape(3, 4))
            dtype = h5py.ref_dtype if family == "object_reference" else h5py.regionref_dtype
            values = np.empty(3, dtype=dtype)
            if family == "object_reference":
                values[:] = [target.ref, handle["/"].ref, h5py.Reference()]
            else:
                values[:] = [target.regionref[1:3, :2], target.regionref[0:1, :], h5py.RegionReference()]
            options.update(dtype=dtype)
        elif family == "compact":
            space = h5py.h5s.create_simple(values.shape)
            creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            creation.set_layout(h5py.h5d.COMPACT)
            identifier = h5py.h5d.create(handle.id, b"science", h5py.h5t.py_create(values.dtype), space, dcpl=creation)
            identifier.write(h5py.h5s.ALL, h5py.h5s.ALL, values)
            dataset = h5py.Dataset(identifier)
            dataset.attrs["units"] = "arbitrary"
            return
        elif family == "lzf":
            options.update(chunks=(4, 3), compression="lzf", shuffle=True, fletcher32=True)
        elif family == "scaleoffset":
            options.update(chunks=(4, 3), scaleoffset=0)
        elif family != "contiguous":
            raise ValueError(f"unknown family: {family}")
        dataset = handle.create_dataset("science", data=values, **options)
        dataset.attrs["units"] = "arbitrary"


def chunk_dimension_byte(source: Path, address: int) -> int | None:
    """Locate a modern chunk-dimension byte independently for declared faults."""
    data = source.read_bytes()
    prefix = data[address:address + 6]
    if prefix[:5] != b"OHDR\x02" or prefix[5] & 0xC4:
        return None
    flags = prefix[5]
    size_width = 1 << (flags & 3)
    size_at = address + 6 + (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0)
    size = int.from_bytes(data[size_at:size_at + size_width], "little")
    cursor, end = size_at + size_width, size_at + size_width + size
    while cursor + 4 <= end:
        kind, length = data[cursor], int.from_bytes(data[cursor + 1:cursor + 3], "little")
        start = cursor + 4
        body = data[start:start + length]
        if kind == 8 and len(body) >= 6 and body[0] in (4, 5) and body[1] == 2:
            return start + 5
        cursor = start + length
    return None


def fault_sites(source: Path) -> dict[str, int]:
    """Declared mutation sites come from pristine evaluator metadata only."""
    sites: dict[str, int] = {}
    data = source.read_bytes()
    if data[:8] != b"\x89HDF\r\n\x1a\n":
        raise ValueError("fixture has no HDF5 signature")
    sites["signature_bitflip"] = 0
    if data[8] in (2, 3):
        sites["root_pointer_bitflip"] = 12 + 3 * data[9]
        sites["interrupted_write_status"] = 11
        # Retain explicitly unrecoverable cases: the original stored metadata
        # checksum is lost, so single-byte data correction has no valid oracle.
        if source.stem in ("contiguous", "fixed_array", "variable_string"):
            sites["superblock_checksum_bitflip"] = 12 + 4 * data[9]
    with h5py.File(source, "r") as handle:
        selected = handle["science"]
        address = int(h5py.h5o.get_info(selected.id).addr)
        dimension = chunk_dimension_byte(source, address)
        if dimension is not None and selected.size:
            sites["chunk_dimension_bitflip"] = dimension
        if selected.chunks and selected.size and selected.fletcher32:
            records = [selected.id.get_chunk_info(i) for i in range(selected.id.get_num_chunks())]
            if records:
                first = min(records, key=lambda info: info.byte_offset)
                last = max(records, key=lambda info: info.byte_offset + info.size)
                sites["checksummed_payload_bitflip"] = int(first.byte_offset + first.size // 2)
                sites["tail_truncation"] = int(last.byte_offset + last.size // 2)
    return sites


def _selection(dataset: h5py.Dataset, unit: tuple[int, ...]) -> tuple[slice, ...] | tuple[()]:
    if not dataset.shape:
        return ()
    if dataset.chunks is None:
        return tuple(slice(0, size) for size in dataset.shape)
    return tuple(slice(index * width, min((index + 1) * width, size))
                 for index, width, size in zip(unit, dataset.chunks, dataset.shape))


def attribute_reported_omitted(name: str, reasons: list[str]) -> bool:
    """A named omission does not justify discarding unrelated attributes."""
    blanket_reasons = {"attributes not inspected", "attributes were not read from the padded inventory view"}
    return name in reasons or bool(blanket_reasons.intersection(reasons))


def sparse_allocation_matches(source: Path, output: Path, report_path: Path) -> bool:
    """Independently check intact sparse acceptance against physical allocation."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    selected = next(entry["report"] for entry in report["datasets"] if entry["path"] == "/science")
    status_path = selected.get("validity_map") or selected["whole_file_metadata_group"] + "/chunk_status"
    with h5py.File(source, "r") as original, h5py.File(output, "r") as exported:
        dataset = original["science"]
        allocated = {tuple(int(origin) // width for origin, width in zip(
            dataset.id.get_chunk_info(index).chunk_offset, dataset.chunks))
            for index in range(dataset.id.get_num_chunks())}
        status = exported[status_path][()]
        accepted = {index for index in np.ndindex(status.shape) if int(status[index]) == 1}
    return accepted == allocated


def score(source: Path, output: Path, report_path: Path) -> dict[str, Any]:
    """Check reported accepted coordinates against evaluator-only truth."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    exact = wrong = unknown = total = attributes = 0
    issues: list[str] = []
    context_omissions: list[str] = []
    entries = {entry["path"]: entry["report"] for entry in report.get("datasets", [])}
    context = report.get("scientific_context", {})
    omitted_context = {entry["path"]: entry.get("attributes_omitted", [])
                       for entry in context.get("groups", []) + context.get("datasets", [])}
    with h5py.File(source, "r") as original, h5py.File(output, "r") as exported:
        paths = ["/"]
        original.visit(paths.append)
        for path in paths:
            old = original[path]
            if path not in exported:
                issues.append(f"missing object: {path}")
                if isinstance(old, h5py.Dataset):
                    count = old.size if old.shape is not None else 0
                    total += count
                    unknown += count
                continue
            new = exported[path]
            canonical_path = path if path.startswith("/") else "/" + path
            for name in old.attrs:
                attributes += 1
                if name not in new.attrs and attribute_reported_omitted(name, omitted_context.get(canonical_path, [])):
                    context_omissions.append(f"attribute unavailable: {path}:{name}")
                elif (name not in new.attrs or not old.attrs.get_id(name).get_type().equal(new.attrs.get_id(name).get_type())
                        or not equal_values(old.attrs[name], new.attrs[name], original, exported)):
                    issues.append(f"attribute differs: {path}:{name}")
            if not isinstance(old, h5py.Dataset):
                continue
            count = old.size if old.shape is not None else 0
            total += count
            if (not isinstance(new, h5py.Dataset) or old.shape != new.shape or old.maxshape != new.maxshape
                    or not old.id.get_type().equal(new.id.get_type())):
                issues.append(f"dataset schema differs: {path}")
                unknown += count
                continue
            if old.shape is None:
                if not equal_values(old[()], new[()], original, exported):
                    issues.append(f"null dataset differs: {path}")
                continue
            if count == 0:
                continue
            selected_report = entries.get(canonical_path)
            if not selected_report:
                issues.append(f"missing dataset evidence: {path}")
                unknown += count
                continue
            status_path = (selected_report.get("element_status") or selected_report.get("validity_map")
                or selected_report.get("validity", {}).get("dataset")
                or selected_report["whole_file_metadata_group"] + "/chunk_status")
            status = np.asarray(exported[status_path][()])
            if not set(int(value) for value in status.flat) <= set(range(8)):
                issues.append(f"unknown validity code: {path}")
            historical = selected_report.get("historical_integrity", {})
            historical_path = historical.get("prior_capture_match_status_dataset")
            if historical_path and np.any(exported[historical_path][()]):
                issues.append(f"historical equality claimed without an input capture: {path}")
            element_mask = (bool(selected_report.get("element_status"))
                            or selected_report.get("validity", {}).get("granularity") == "one code per selected dataset element")
            accepted_before, unknown_before = exact + wrong, unknown
            if element_mask:
                if status.shape != old.shape:
                    raise ValueError(f"element status shape differs: {path}")
                for index in np.ndindex(old.shape):
                    if int(status[index]) != 1:
                        unknown += 1
                    elif equal_values(old[index], new[index], original, exported):
                        exact += 1
                    else:
                        wrong += 1
            else:
                expected_shape = (tuple((size + width - 1) // width for size, width in zip(old.shape, old.chunks))
                                  if old.chunks else (1,))
                if status.shape != expected_shape:
                    raise ValueError(f"chunk status shape differs: {path}")
                for unit in np.ndindex(status.shape):
                    selection = _selection(old, unit)
                    old_values = old[selection]
                    size = int(np.asarray(old_values).size)
                    if int(status[unit]) != 1:
                        unknown += size
                    elif equal_values(old_values, new[selection], original, exported):
                        exact += size
                    else:
                        before_values, after_values = np.asarray(old_values), np.asarray(new[selection])
                        correct = sum(equal_values(before_values[index], after_values[index], original, exported)
                                      for index in np.ndindex(before_values.shape))
                        exact += correct
                        wrong += size - correct
            if ("accepted_elements" in selected_report
                    and selected_report["accepted_elements"] != exact + wrong - accepted_before):
                issues.append(f"accepted element count contradicts validity map: {path}")
            if ("unknown_elements" in selected_report
                    and selected_report["unknown_elements"] != unknown - unknown_before):
                issues.append(f"unknown element count contradicts validity map: {path}")
        embedded = exported[report["metadata_group"] + "/report_json"][()]
        if json.loads(embedded) != report:
            issues.append("embedded report differs from external report")
    if total != exact + wrong + unknown:
        issues.append("coordinate counts do not add to the independent truth size")
    if report.get("outcome") == "complete" and unknown:
        issues.append("complete report contains unresolved coordinates")
    return {"decision": "output", "reported_outcome": report.get("outcome"),
            "total_elements": total, "exact_accepted_elements": exact,
            "wrong_accepted_elements": wrong, "unknown_elements": unknown,
            "attributes_checked": attributes, "context_omissions": context_omissions,
            "evaluation_errors": issues,
            "fully_exact": wrong == unknown == 0 and not issues and not context_omissions,
            "useful_output": (exact > 0 or total == 0) and wrong == 0 and not issues}


def run_case(source: Path, family: str, fault: str, site: int | None,
             workspace: Path, *, timeout: int) -> dict[str, Any]:
    case_dir = workspace / "cases" / f"{family}-{fault}"
    case_dir.mkdir(parents=True)
    damaged, output, report_path = (case_dir / name for name in ("input.h5", "output.h5", "report.json"))
    shutil.copyfile(source, damaged)
    if site is not None:
        with damaged.open("r+b") as stream:
            if fault == "tail_truncation":
                stream.truncate(site)
            elif fault == "interrupted_write_status":
                prefix = stream.read(12)
                size = 12 + 4 * prefix[9] + 4
                stream.seek(0)
                block = bytearray(stream.read(size))
                if metadata_checksum(block[:-4]) != int.from_bytes(block[-4:], "little"):
                    raise ValueError("independently written fixture superblock checksum differs")
                if block[11] != 0:
                    raise ValueError("status fixture writer was not cleanly closed")
                block[11] = 1
                block[-4:] = metadata_checksum(block[:-4]).to_bytes(4, "little")
                stream.seek(0)
                stream.write(block)
            else:
                stream.seek(site)
                previous = stream.read(1)
                stream.seek(site)
                stream.write(bytes((previous[0] ^ 1,)))
    original_digest, damaged_digest = digest(source), digest(damaged)
    with h5py.File(source, "r") as truth:
        total = 0
        def count_dataset(_name, obj):
            nonlocal total
            if isinstance(obj, h5py.Dataset) and obj.shape is not None:
                total += obj.size
        truth.visititems(count_dataset)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    try:
        process = subprocess.run([sys.executable, "-m", "h5reclaim", "rescue", str(damaged),
            "--output", str(output), "--report", str(report_path)],
            env=environment, capture_output=True, text=True, timeout=timeout)
        if process.returncode == 0 and output.is_file() and report_path.is_file():
            observed = score(source, output, report_path)
            if family == "sparse_fill" and fault == "intact":
                observed["accepted_coordinates_match_independent_allocation"] = sparse_allocation_matches(source, output, report_path)
                if not observed["accepted_coordinates_match_independent_allocation"]:
                    observed["evaluation_errors"].append("sparse accepted coordinates differ from independent native allocation")
                    observed["useful_output"] = False
        else:
            refused = process.returncode == 2 and not output.exists() and not report_path.exists()
            observed = {"decision": "refused" if refused else "invalid",
                "total_elements": total, "exact_accepted_elements": 0,
                "wrong_accepted_elements": 0, "unknown_elements": total,
                "evaluation_errors": [] if refused else [f"CLI exit {process.returncode}"],
                "fully_exact": False, "useful_output": False,
                "reason": (process.stderr or process.stdout).strip()[-600:]}
    except (subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError) as exc:
        observed = {"decision": "evaluation_error", "total_elements": total,
            "unknown_elements": total, "exact_accepted_elements": 0,
            "wrong_accepted_elements": 0, "evaluation_errors": [str(exc)],
            "fully_exact": False, "useful_output": False}
    unchanged = digest(source) == original_digest and digest(damaged) == damaged_digest
    if not unchanged:
        observed["evaluation_errors"].append("original or damaged source changed")
        observed["useful_output"] = observed["fully_exact"] = False
    return {"family": family, "fault": fault, "fault_byte_offset": site,
            "original_sha256": original_digest, "damaged_sha256": damaged_digest,
            "source_unchanged": unchanged, "observed": observed}


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(cases)
    useful = sum(case["observed"]["useful_output"] for case in cases)
    exact = sum(case["observed"]["exact_accepted_elements"] for case in cases)
    total = sum(case["observed"].get("total_elements", 0) for case in cases)
    return {"cases": count, "useful_outputs": useful,
        "useful_output_fraction": useful / count if count else None,
        "total_elements": total, "exact_accepted_element_fraction": exact / total if total else None,
        "fully_exact_outputs": sum(case["observed"]["fully_exact"] for case in cases),
        "partial_useful_outputs": sum(case["observed"]["useful_output"] and not case["observed"]["fully_exact"] for case in cases),
        "refusals": sum(case["observed"]["decision"] == "refused" for case in cases),
        "invalid_or_unscorable": sum(case["observed"]["decision"] not in ("output", "refused") for case in cases),
        "exact_accepted_elements": exact,
        "wrong_accepted_elements": sum(case["observed"]["wrong_accepted_elements"] for case in cases),
        "unknown_elements": sum(case["observed"].get("unknown_elements", 0) for case in cases),
        "context_omission_cases": sum(bool(case["observed"].get("context_omissions")) for case in cases),
        "evaluation_error_cases": sum(bool(case["observed"]["evaluation_errors"]) for case in cases)}


def run(workspace: Path, *, seed: int = 20261005, families: tuple[str, ...] = FAMILIES,
        timeout: int = 90) -> dict[str, Any]:
    if not 0 <= seed < 2**64 or not 1 <= timeout <= 3600:
        raise ValueError("seed must be unsigned 64-bit and timeout must be 1..3600 seconds")
    if not families or len(set(families)) != len(families) or not set(families) <= set(FAMILIES):
        raise ValueError("families must be distinct declared matrix families")
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("work directory must be new or empty")
    originals = workspace / "truth"
    originals.mkdir(parents=True)
    tested_before = source_fingerprint()
    cases = []
    for family in families:
        source = originals / f"{family}.h5"
        family_seed = int.from_bytes(hashlib.sha256(f"{seed}:{family}".encode()).digest()[:8], "big")
        generate(source, family, family_seed)
        sites = fault_sites(source)
        for fault, site in [("intact", None), *sites.items()]:
            case = run_case(source, family, fault, site, workspace, timeout=timeout)
            cases.append(case)
            print(f"{family}/{fault}: {case['observed']['decision']}, "
                  f"{case['observed']['exact_accepted_elements']} exact, "
                  f"{case['observed'].get('unknown_elements', '?')} unknown", file=sys.stderr, flush=True)
    damaged = [case for case in cases if case["fault"] != "intact"]
    tested_after = source_fingerprint()
    tool_version = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE).group(1)
    result = {"schema_version": 1, "seed": seed,
        "tool_version": tool_version, "tested_source": tested_after,
        "tested_source_unchanged": tested_before == tested_after,
        "category": "public automatic rescue, generated layouts and declared controlled faults",
        "environment": {"python": platform.python_version(), "platform": platform.system(),
            "h5py": h5py.__version__, "hdf5": h5py.version.hdf5_version, "numpy": np.__version__},
        "families": list(families), "summary": summarize(cases),
        "intact": summarize([case for case in cases if case["fault"] == "intact"]),
        "damaged": summarize(damaged),
        "by_fault": {fault: summarize([case for case in cases if case["fault"] == fault])
            for fault in sorted({case["fault"] for case in cases})},
        "passed": tested_before == tested_after and all(case["source_unchanged"] and not case["observed"]["evaluation_errors"]
            and case["observed"]["wrong_accepted_elements"] == 0
            and (case["fault"] != "intact" or case["observed"]["fully_exact"]
                or case["family"] == "sparse_fill" and case["observed"]["useful_output"]
                    and case["observed"].get("accepted_coordinates_match_independent_allocation")) for case in cases),
        "cases": cases,
        "limits": "Fixed, generated, correlated fixtures and declared faults. Useful means some exact accepted data "
            "or a correctly preserved zero-element schema, with no wrong accepted data or schema discrepancy. "
            "Explicitly reported unavailable attributes are separate context omissions, not recovered context. "
            "A partial export is not full recovery. Refusals score zero useful recoveries. Metadata mutations target "
            "checksummed single-byte root pointers and chunk dimensions, plus signature and stored-checksum "
            "corruption and interrupted-write flags on a checksum-valid closed-writer copy; they do not "
            "represent arbitrary destroyed metadata or a live writer. The legacy fixture has no modern status flag. "
            "Unchecksummed payload corruption is not tested and may be undetectable. No naturally damaged "
            "incident or representative real-world fault distribution is measured; these fractions are not "
            "an 80% field-wide success claim. Truth is separated by CLI inputs, not OS access permissions."}
    (workspace / "coverage.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--work-dir", type=Path, help="new or empty directory retaining originals and evidence")
    parser.add_argument("--families", nargs="+", choices=FAMILIES, default=FAMILIES)
    parser.add_argument("--timeout", type=int, default=90, help="maximum seconds per rescue subprocess")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    workspace = args.work_dir or Path(tempfile.mkdtemp(prefix="h5reclaim-release-panel-"))
    result = run(workspace, seed=args.seed, families=tuple(args.families), timeout=args.timeout)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("H5Reclaim automatic rescue release panel")
        for label in ("intact", "damaged"):
            row = result[label]
            print(f"{label}: {row['useful_outputs']}/{row['cases']} useful, "
                f"{row['fully_exact_outputs']} fully exact, {row['partial_useful_outputs']} partial, "
                f"{row['refusals']} refused, {row['wrong_accepted_elements']} wrong accepted elements")
        print("These generated trials do not estimate real-world recovery probability.")
        print(f"Full evidence: {workspace / 'coverage.json'}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
