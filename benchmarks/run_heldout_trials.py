"""Blinded-input, fault-stratified evaluation on independently supplied HDF5 files.

The evaluator owns pristine files and mutation manifests. Each recovery child
receives only a disposable damaged copy, dataset path, and fresh destinations.
Recovery and scoring use separate program inputs on a shared filesystem. Scores
describe the independently supplied panel and its declared controlled faults.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
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
FAULTS = ("intact", "signature", "object_header", "payload_bit",
          "payload_burst", "truncate_payload")
MAX_ENTRIES = 128
MAX_SOURCE = 64 << 20
MAX_ELEMENTS = 1_048_576
MAX_REPORT = 16 << 20


class PanelError(RuntimeError):
    """The panel, mutation, or evaluation protocol is invalid."""


@dataclass(frozen=True)
class Entry:
    identifier: str
    source: Path
    sha256: str
    size: int
    dataset: str
    provenance: str
    shape: tuple[int, ...]
    dtype: str
    chunks: tuple[int, ...] | None
    elements: int
    object_address: int
    signature_offset: int
    payload: tuple[tuple[int, int, tuple[int, ...] | None], ...]
    payload_exclusion: str | None


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            result.update(block)
    return result.hexdigest()


def _signature_offset(path: Path) -> int:
    size = path.stat().st_size
    with path.open("rb") as stream:
        offset = 0
        while offset + 8 <= size:
            stream.seek(offset)
            if stream.read(8) == b"\x89HDF\r\n\x1a\n":
                return offset
            offset = 512 if offset == 0 else 2 * offset
    raise PanelError("source has no signature at a permitted HDF5 offset")


def _entry(manifest_dir: Path, raw: dict[str, Any]) -> Entry:
    if not isinstance(raw, dict):
        raise PanelError("each panel entry must be an object")
    identifier = raw.get("id")
    relative = raw.get("path")
    sha = raw.get("sha256")
    size = raw.get("size_bytes")
    dataset = raw.get("dataset")
    provenance = raw.get("provenance")
    if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", identifier)
        or not isinstance(relative, str) or not relative
        or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
        or type(size) is not int or not 0 < size <= MAX_SOURCE
        or not isinstance(dataset, str) or not dataset.startswith("/")
        or dataset in ("/", "/_h5reclaim") or dataset.startswith("/_h5reclaim/")
        or not isinstance(provenance, str) or not provenance.strip() or len(provenance) > 500):
        raise PanelError("each entry needs bounded id, relative path, hash, size, dataset, provenance")
    path_parts = PurePosixPath(relative)
    if (path_parts.is_absolute() or path_parts.as_posix() != relative
        or any(p in ("", ".", "..") for p in path_parts.parts)):
        raise PanelError("source path must be a safe relative POSIX path")
    source = manifest_dir.joinpath(*path_parts.parts)
    if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(manifest_dir):
        raise PanelError(f"source is absent, a symlink, or escapes manifest directory: {identifier}")
    if source.stat().st_size != size or digest(source) != sha:
        raise PanelError(f"source differs from declared size or SHA-256: {identifier}")
    sig = _signature_offset(source)
    with h5py.File(source, "r") as file:
        selected = file[dataset]
        if not isinstance(selected, h5py.Dataset):
            raise PanelError(f"selection is not a dataset: {identifier}")
        dtype = selected.dtype
        shape = tuple(int(n) for n in selected.shape)
        elements = math.prod(shape)
        if (len(shape) > 4 or elements < 1 or elements > MAX_ELEMENTS
            or dtype.hasobject or dtype.kind not in "biufc"
            or elements * dtype.itemsize > 16 << 20):
            raise PanelError(f"selected dataset exceeds fixed-numeric truth-scoring bounds: {identifier}")
        chunks = tuple(selected.chunks) if selected.chunks else None
        address = int(h5py.h5o.get_info(selected.id).addr)
        payload: list[tuple[int, int, tuple[int, ...] | None]] = []
        reason = None
        creation = selected.id.get_create_plist()
        if creation.get_layout() in (h5py.h5d.COMPACT, h5py.h5d.VIRTUAL):
            reason = "compact or virtual storage has no independent local payload extent"
        elif chunks is not None:
            for n in range(selected.id.get_num_chunks()):
                info = selected.id.get_chunk_info(n)
                start, length = int(info.byte_offset), int(info.size)
                if 0 <= start < size and 0 < length <= size-start:
                    payload.append((start, length, tuple(int(x) for x in info.chunk_offset)))
            if not payload:
                reason = "no locally allocated chunk with a complete physical extent"
        elif creation.get_external_count():
            reason = "external storage needs a separately scored dependency bundle"
        else:
            start = selected.id.get_offset()
            length = elements * dtype.itemsize
            if start is None or start < 0 or length < 1 or start + length > size:
                reason = "contiguous payload has no complete local extent"
            else:
                payload.append((int(start), length, None))
    return Entry(identifier, source, sha, size, dataset, provenance, shape,
                 dtype.str, chunks, elements, address, sig, tuple(payload), reason)


def load_panel(manifest: Path) -> tuple[str, str, tuple[Entry, ...]]:
    manifest = manifest.resolve(strict=True)
    if manifest.stat().st_size > 1 << 20:
        raise PanelError("manifest exceeds 1 MiB")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if (not isinstance(document, dict) or document.get("schema_version") != 1
        or not isinstance(document.get("cohort"), str)
        or not 1 <= len(document["cohort"]) <= 120
        or not isinstance(document.get("entries"), list)
        or not 1 <= len(document["entries"]) <= MAX_ENTRIES):
        raise PanelError("manifest needs schema_version 1, cohort, and 1..128 entries")
    entries = tuple(_entry(manifest.parent, raw) for raw in document["entries"])
    if len({entry.identifier for entry in entries}) != len(entries):
        raise PanelError("duplicate panel source id")
    return digest(manifest), document["cohort"], entries


def _eligible(entry: Entry, fault: str) -> str | None:
    if fault == "object_header" and not 0 <= entry.object_address < entry.size:
        return "selected object header address is outside source"
    if fault in ("payload_bit", "payload_burst", "truncate_payload"):
        if fault == "truncate_payload" and not entry.payload_exclusion:
            start, size, _origin = max(entry.payload, key=lambda item: item[0]+item[1])
            if size < 2 or not start < start + max(1, size//2) < entry.size:
                return "no interior payload truncation point before physical EOF"
        return entry.payload_exclusion
    return None


def _mutate(entry: Entry, target: Path, fault: str, seed: int) -> dict[str, Any]:
    shutil.copyfile(entry.source, target)
    if fault == "intact":
        return {"class": fault, "offset": None, "length": 0}
    rng = random.Random(seed)
    if fault == "signature":
        start, length = entry.signature_offset, 1
    elif fault == "object_header":
        start, length = entry.object_address, 1
    elif fault == "truncate_payload":
        start, size, origin = max(entry.payload, key=lambda item: item[0] + item[1])
        cut = start + max(1, size // 2)
        if cut >= entry.size:
            cut = start + size - 1
        if not start < cut < entry.size:
            raise PanelError("payload has no valid interior truncation point")
        with target.open("r+b") as stream:
            stream.truncate(cut)
        return {"class": fault, "offset": cut, "length": entry.size-cut,
                "selected_chunk_origin": origin}
    else:
        start, size, origin = rng.choice(entry.payload)
        length = 1 if fault == "payload_bit" else min(8, size)
        start += rng.randrange(size-length+1)
    with target.open("r+b") as stream:
        stream.seek(start)
        previous = stream.read(length)
        if len(previous) != length:
            raise PanelError("mutation site changed before copied input could be edited")
        if fault in ("signature", "object_header", "payload_bit"):
            changed = bytes((previous[0] ^ (1 << rng.randrange(8)),))
        else:
            changed = bytes(b ^ rng.randrange(1, 256) for b in previous)
        stream.seek(start)
        stream.write(changed)
    return {"class": fault, "offset": start, "length": length,
            "selected_chunk_origin": origin if fault.startswith("payload_") else None}


def _validity(output: h5py.File, report: dict[str, Any], entry: Entry) -> np.ndarray:
    shape, chunks = entry.shape, entry.chunks
    accepted = np.zeros(shape, dtype=bool)
    if chunks is None:
        if "/_h5reclaim/element_status" in output:
            codes = output["/_h5reclaim/element_status"][...]
            if codes.shape != shape or not np.isin(codes, (0, 1)).all():
                raise PanelError("element validity map has incorrect shape or code")
            return np.asarray(codes == 1)
        if report.get("mode") == "readable_export" and report.get("validity_map") is None:
            return np.ones(shape, dtype=bool)
        raise PanelError("nonchunked output lacks an explicit element validity decision")
    address = ("/_h5reclaim/chunk_status" if "/_h5reclaim/chunk_status" in output else
               "/_h5reclaim/validity" if "/_h5reclaim/validity" in output else None)
    if address is None:
        raise PanelError("chunked output lacks chunk validity map")
    codes = output[address][...]
    grid = tuple((extent+width-1)//width for extent, width in zip(shape, chunks))
    if codes.shape != grid or not np.isin(codes, tuple(range(1, 8)) if address.endswith("chunk_status")
                                             else (0, 1)).all():
        raise PanelError("chunk validity map has incorrect shape or code")
    for index in product(*(range(n) for n in grid)):
        slices = tuple(slice(pos*width, min((pos+1)*width, extent))
                       for pos, width, extent in zip(index, chunks, shape))
        accepted[slices] = int(codes[index]) == 1
    return accepted


def _score(entry: Entry, damaged: Path, output: Path, report_path: Path) -> dict[str, Any]:
    errors: list[str] = []
    if report_path.stat().st_size > MAX_REPORT:
        raise PanelError("published report exceeds 16 MiB evaluation limit")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (not isinstance(report, dict) or not isinstance(report.get("source"), dict)
        or not isinstance(report.get("dataset"), dict)):
        raise PanelError("published report lacks source or selected dataset record")
    damaged_hash = digest(damaged)
    if (report.get("source", {}).get("sha256_before") != damaged_hash
        or report.get("source", {}).get("sha256_after") != damaged_hash
        or report.get("dataset", {}).get("path") != entry.dataset):
        errors.append("source hash or selected dataset in report disagrees with trial input")
    with h5py.File(entry.source, "r") as pristine, h5py.File(output, "r") as recovered:
        truth, selected = pristine[entry.dataset], recovered[entry.dataset]
        if (truth.shape != selected.shape or truth.dtype != selected.dtype
            or not truth.id.get_type().equal(selected.id.get_type())):
            raise PanelError("output changed exact HDF5 datatype or selected shape")
        accepted = _validity(recovered, report, entry)
        before = np.ascontiguousarray(np.asarray(truth[...]).reshape(-1))
        after = np.ascontiguousarray(np.asarray(selected[...]).reshape(-1))
        exact_bits = np.all(before.view("u1").reshape(-1, truth.dtype.itemsize)
                            == after.view("u1").reshape(-1, truth.dtype.itemsize), axis=1)
        mask = np.asarray(accepted).reshape(-1)
        exact = int(np.count_nonzero(mask & exact_bits))
        wrong = int(np.count_nonzero(mask & ~exact_bits))
        unknown = entry.elements - exact - wrong
        embedded = recovered["/_h5reclaim/report_json"][()]
        if isinstance(embedded, bytes):
            embedded = embedded.decode("utf-8")
        if json.loads(embedded) != report:
            errors.append("embedded and external reports disagree")
    return {"decision": "output", "exact_elements": exact, "wrong_accepted_elements": wrong,
            "unknown_elements": unknown, "refused_elements": 0, "protocol_errors": errors,
            "reported_mode": report.get("mode", report.get("operation", "structural_chunked"))}


def _case(entry: Entry, fault: str, trial: int, workspace: Path,
          seed: int, python: str, timeout: int) -> dict[str, Any]:
    exclusion = _eligible(entry, fault)
    name = f"{entry.identifier}_{fault}_{trial:03}"
    record: dict[str, Any] = {"case": name, "source_id": entry.identifier,
                              "fault_class": fault, "trial": trial,
                              "logical_elements": entry.elements,
                              "eligible": exclusion is None}
    if exclusion is not None:
        record["exclusion_reason"] = exclusion
        return record
    folder = workspace / "cases" / name
    folder.mkdir(parents=True)
    damaged, output, report_path = (folder / item for item in ("damaged.h5", "output.h5", "report.json"))
    case_seed = int.from_bytes(hashlib.sha256(f"{seed}:{name}".encode()).digest()[:8], "big")
    try:
        mutation = _mutate(entry, damaged, fault, case_seed)
    except (OSError, ValueError, PanelError) as exc:
        record.update({"mutation": None, "input_sha256": None,
                       "observation": {"decision": "protocol_failure", "outcome": "protocol_failure",
                                       "exact_elements": 0, "wrong_accepted_elements": 0,
                                       "unknown_elements": 0, "refused_elements": 0,
                                       "protocol_errors": [f"mutation could not be applied: {exc}"]}})
        return record
    input_hash = digest(damaged)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    try:
        result = subprocess.run([python, "-m", "h5reclaim", "rescue", str(damaged),
                                 "--dataset", entry.dataset, "--output", str(output),
                                 "--report", str(report_path)], cwd=folder, env=env,
                                capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode == 2 and not output.exists() and not report_path.exists():
            observation = {"decision": "safe_refusal", "exact_elements": 0,
                           "wrong_accepted_elements": 0, "unknown_elements": 0,
                           "refused_elements": entry.elements, "protocol_errors": [],
                           "reason": (result.stderr or result.stdout)[-300:]}
        elif result.returncode == 0 and output.is_file() and report_path.is_file():
            observation = _score(entry, damaged, output, report_path)
        else:
            observation = {"decision": "protocol_failure", "exact_elements": 0,
                           "wrong_accepted_elements": 0, "unknown_elements": 0,
                           "refused_elements": 0,
                           "protocol_errors": [f"exit {result.returncode}; output={output.exists()}; "
                                               f"report={report_path.exists()}; "
                                               f"{(result.stderr or result.stdout)[-300:]}"]}
    except subprocess.TimeoutExpired:
        observation = {"decision": "timeout", "exact_elements": 0,
                       "wrong_accepted_elements": 0, "unknown_elements": 0,
                       "refused_elements": 0, "protocol_errors": ["child deadline exceeded"]}
    except (OSError, ValueError, KeyError, TypeError, PanelError, json.JSONDecodeError) as exc:
        observation = {"decision": "protocol_failure", "exact_elements": 0,
                       "wrong_accepted_elements": 0, "unknown_elements": 0,
                       "refused_elements": 0,
                       "protocol_errors": [f"{type(exc).__name__}: {str(exc)[:300]}"]}
    if digest(entry.source) != entry.sha256 or digest(damaged) != input_hash:
        observation["protocol_errors"].append("original or damaged input changed during recovery")
    if observation["protocol_errors"]:
        observation["outcome"] = "protocol_failure"
    elif observation["wrong_accepted_elements"]:
        observation["outcome"] = "false_accept"
    elif observation["decision"] == "safe_refusal":
        observation["outcome"] = "safe_refusal"
    elif observation["exact_elements"] == entry.elements:
        observation["outcome"] = "all_exact"
    elif observation["exact_elements"]:
        observation["outcome"] = "partial_exact"
    else:
        observation["outcome"] = "zero_accepted"
    record.update({"mutation": mutation, "input_sha256": input_hash,
                   "observation": observation})
    return record


def run_panel(manifest: Path, workspace: Path, *, faults: tuple[str, ...] = FAULTS,
              trials: int = 1, seed: int = 20260927, python: str = sys.executable,
              timeout: int = 90) -> dict[str, Any]:
    if (not 1 <= trials <= 20 or not 0 <= seed < 2**64 or not 5 <= timeout <= 900
        or not faults or len(set(faults)) != len(faults) or set(faults) - set(FAULTS)):
        raise PanelError("invalid trials, unsigned seed, 5..900 second deadline, or fault classes")
    manifest_hash, cohort, entries = load_panel(manifest)
    workspace = workspace.resolve()
    if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
        raise PanelError("work directory must be new or empty")
    if any(workspace == entry.source or workspace in entry.source.parents for entry in entries):
        raise PanelError("work directory must not contain pristine source files")
    workspace.mkdir(parents=True, exist_ok=True)
    cases = [_case(entry, fault, trial, workspace, seed, python, timeout)
             for entry in entries for fault in faults for trial in range(trials)]
    strata = {}
    for fault in faults:
        selected = [case for case in cases if case["fault_class"] == fault]
        scored = [case["observation"] for case in selected if case["eligible"]]
        outcomes = {kind: sum(case["outcome"] == kind for case in scored)
                    for kind in ("all_exact", "partial_exact", "zero_accepted", "safe_refusal",
                                 "false_accept", "protocol_failure")}
        strata[fault] = {"planned_cases": len(selected), "eligible_cases": len(scored),
                         "excluded_cases": len(selected)-len(scored), "outcomes": outcomes,
                         "logical_elements_in_eligible_cases": sum(case["logical_elements"]
                                                                   for case in selected if case["eligible"]),
                         "exact_accepted_elements": sum(case["exact_elements"] for case in scored),
                         "wrong_accepted_elements": sum(case["wrong_accepted_elements"] for case in scored),
                         "unknown_elements": sum(case["unknown_elements"] for case in scored),
                         "refused_elements": sum(case["refused_elements"] for case in scored)}
        strata[fault]["unscored_elements"] = (strata[fault]["logical_elements_in_eligible_cases"]
                                             - sum(strata[fault][field] for field in
                                                   ("exact_accepted_elements", "wrong_accepted_elements",
                                                    "unknown_elements", "refused_elements")))
    return {"schema_version": 1, "experiment": "controlled faults on a self-declared held-out panel",
            "cohort": cohort, "manifest_sha256": manifest_hash, "seed": seed,
            "trials_per_fault_per_source": trials, "fault_classes": list(faults),
            "source_count": len(entries),
            "sources": [{"id": e.identifier, "original_sha256": e.sha256, "dataset": e.dataset,
                         "provenance": e.provenance, "elements": e.elements,
                         "shape": list(e.shape), "dtype": e.dtype,
                         "chunks": list(e.chunks) if e.chunks else None} for e in entries],
            "planned_cases": len(cases), "eligible_cases": sum(c["eligible"] for c in cases),
            "excluded_cases": sum(not c["eligible"] for c in cases),
            "strata": strata, "cases": cases,
            "no_false_accept_or_protocol_failure": all(
                not case["eligible"] or case["observation"]["outcome"] not in
                ("false_accept", "protocol_failure") for case in cases),
            "limits": ("The report retains operator-declared file provenance and selected fault classes. "
                       "Generated mutations reuse the supplied panel files. Pristine truth is separate "
                       "from recovery subprocess arguments on a shared filesystem. Refusals contribute "
                       "zero recovered values. Exact values, wrong accepted values, unknown values and "
                       "excluded pairs are reported separately. Eligible-case scores retain their "
                       "eligible denominator; excluded pairs have separate counts.")}


def render_text(report: dict[str, Any]) -> str:
    lines = [f"H5Reclaim | Controlled held-out panel: {report['cohort']}",
             f"{report['source_count']} originals | {report['planned_cases']} planned | "
             f"{report['eligible_cases']} eligible | {report['excluded_cases']} excluded",
             "Fault class           Eligible/Planned  Exact  Partial  Refused  Zero  False accepted  Invalid"]
    for fault, stratum in report["strata"].items():
        o = stratum["outcomes"]
        lines.append(f"{fault:<21} {stratum['eligible_cases']:>3}/{stratum['planned_cases']:<3}"
                     f"          {o['all_exact']:>3}      {o['partial_exact']:>3}"
                     f"      {o['safe_refusal']:>3}   {o['zero_accepted']:>3}"
                     f"             {o['false_accept']:>3}"
                     f"      {o['protocol_failure']:>3}")
    lines.extend(["", "These fractions describe only the declared panel and fault injections.",
                  "The JSON report records every exclusion, accepted exact value, unknown, and refusal."])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path, help="hash-pinned, previously unused source panel")
    parser.add_argument("--work-dir", type=Path, help="new or empty output directory")
    parser.add_argument("--faults", default=",".join(FAULTS), help="comma-separated chosen fault classes")
    parser.add_argument("--trials", type=int, default=1, help="1..20 sites per eligible class and source")
    parser.add_argument("--seed", type=int, default=20260927, help="unsigned 64-bit deterministic seed")
    parser.add_argument("--timeout", type=int, default=90, help="5..900 second recovery deadline per case")
    parser.add_argument("--python", default=sys.executable, help="recovery interpreter")
    parser.add_argument("--json", action="store_true", help="print full machine-readable score")
    args = parser.parse_args()
    workspace = args.work_dir or Path(tempfile.mkdtemp(prefix="h5reclaim-heldout-"))
    try:
        report = run_panel(args.manifest, workspace, faults=tuple(args.faults.split(",")),
                           trials=args.trials, seed=args.seed, timeout=args.timeout, python=args.python)
    except (PanelError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"held-out panel could not run: {exc}\nwork directory: {workspace}\n")
    destination = workspace / "evaluation.json"
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render_text(report))
    print(f"Full evaluation: {destination}", file=sys.stderr if args.json else sys.stdout)
    return 0 if report["no_false_accept_or_protocol_failure"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
