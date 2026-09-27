"""Run consented damaged files first, score against separately supplied truth later.

The run manifest contains no healthy file, oracle hash, or recovery sidecar.
Scoring is a separate command after all H5Reclaim subprocesses have exited.
This is input separation, not an OS security boundary for a malicious worker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from itertools import product
from pathlib import Path, PurePosixPath
from typing import Any

import h5py
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
MAX_MANIFEST = 16 << 20
MAX_CASES = 128
MAX_SOURCE = 4 << 30
MAX_REPORT = 16 << 20
MAX_ELEMENTS = 1_048_576
MAX_DECODED = 16 << 20
MAX_HASHES = 100_000
CAUSES = {"unknown", "interrupted_write", "truncation", "metadata", "index",
          "payload", "missing_dependency", "other"}
SHA = re.compile(r"[0-9a-f]{64}\Z")
IDENT = re.compile(r"[A-Za-z0-9_.-]{1,60}\Z")


class IncidentError(RuntimeError):
    """An intake, consent declaration, or evaluation protocol is invalid."""


@dataclass(frozen=True)
class Incident:
    identifier: str
    incident_id: str
    source: Path
    sha256: str
    size: int
    dataset: str
    cause: str


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            value.update(block)
    return value.hexdigest()


def _document(path: Path, limit: int = MAX_MANIFEST) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > limit:
        raise IncidentError(f"manifest is absent or exceeds {limit} bytes")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise IncidentError("manifest must be a JSON object")
    return value


def _safe_file(folder: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise IncidentError("file path must be a safe relative POSIX path")
    parts = PurePosixPath(relative)
    if (parts.is_absolute() or parts.as_posix() != relative
            or any(part in ("", ".", "..") for part in parts.parts)):
        raise IncidentError("file path must be a safe relative POSIX path")
    result = folder.joinpath(*parts.parts)
    if not result.resolve(strict=True).is_relative_to(folder.resolve(strict=True)):
        raise IncidentError("file path escapes its manifest directory")
    if result.is_symlink() or not result.is_file():
        raise IncidentError("file must be regular and not a symlink")
    return result


def _pin_file(path: Path, row: dict[str, Any], *, max_size: int = MAX_SOURCE) -> None:
    sha, size = row.get("sha256"), row.get("size_bytes")
    if (not isinstance(sha, str) or not SHA.fullmatch(sha) or type(size) is not int
            or not 0 < size <= max_size or path.stat().st_size != size or digest(path) != sha):
        raise IncidentError("file differs from its declared size or SHA-256")


def load_incidents(path: Path) -> tuple[str, str, list[Incident]]:
    path = path.resolve(strict=True)
    doc = _document(path, 1 << 20)
    if set(doc) != {"schema_version", "cohort", "cases"} or doc["schema_version"] != 1:
        raise IncidentError("run manifest needs only schema_version 1, cohort, and cases")
    cohort, cases = doc["cohort"], doc["cases"]
    if (not isinstance(cohort, str) or not 1 <= len(cohort) <= 120
            or not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES):
        raise IncidentError("cohort or 1..128 cases are invalid")
    result = []
    for raw in cases:
        if not isinstance(raw, dict) or set(raw) != {
            "id", "incident_id", "path", "sha256", "size_bytes", "dataset",
            "declared_cause", "provenance", "consent",
        }:
            raise IncidentError("each case needs a complete damaged-only provenance and consent record")
        identifier, incident_id = raw["id"], raw["incident_id"]
        consent = raw["consent"]
        if (not isinstance(identifier, str) or not IDENT.fullmatch(identifier)
                or not isinstance(incident_id, str) or not IDENT.fullmatch(incident_id)
                or not isinstance(raw["dataset"], str) or not raw["dataset"].startswith("/")
                or raw["dataset"] == "/" or raw["dataset"].startswith("/_h5reclaim/")
                or not isinstance(raw["declared_cause"], str)
                or raw["declared_cause"] not in CAUSES
                or not isinstance(raw["provenance"], str)
                or not 1 <= len(raw["provenance"].strip()) <= 500
                or not isinstance(consent, dict)
                or set(consent) != {"local_processing", "authorized_by", "recorded_on"}
                or consent["local_processing"] is not True
                or not isinstance(consent["authorized_by"], str)
                or not 1 <= len(consent["authorized_by"].strip()) <= 120
                or not isinstance(consent["recorded_on"], str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", consent["recorded_on"])):
            raise IncidentError("case fields, documented local permission, or declared cause are invalid")
        try:
            date.fromisoformat(consent["recorded_on"])
        except ValueError as exc:
            raise IncidentError("consent date is not a valid calendar date") from exc
        source = _safe_file(path.parent, raw["path"])
        _pin_file(source, raw)
        result.append(Incident(identifier, incident_id, source, raw["sha256"],
                               raw["size_bytes"], raw["dataset"], raw["declared_cause"]))
    if len({item.identifier for item in result}) != len(result):
        raise IncidentError("case IDs must be unique")
    return digest(path), cohort, result


def run_incidents(manifest: Path, workspace: Path, *, python: str = sys.executable,
                  timeout: int = 90) -> dict[str, Any]:
    if not 5 <= timeout <= 900:
        raise IncidentError("subprocess timeout must be 5..900 seconds")
    manifest_hash, cohort, incidents = load_incidents(manifest)
    workspace = workspace.resolve()
    if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
        raise IncidentError("work directory must be new or empty")
    if any(workspace == item.source or workspace in item.source.parents for item in incidents):
        raise IncidentError("work directory must not contain any damaged input")
    workspace.mkdir(parents=True, exist_ok=True)
    results = []
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    for item in incidents:
        folder = workspace / "cases" / item.identifier
        folder.mkdir(parents=True)
        copied, output, report_path = (folder / name for name in
                                       ("damaged.h5", "output.h5", "report.json"))
        shutil.copyfile(item.source, copied)
        if digest(copied) != item.sha256:
            raise IncidentError("damaged input changed while a case was copied")
        try:
            completed = subprocess.run(
                [python, "-m", "h5reclaim", "rescue", str(copied), "--dataset", item.dataset,
                 "--output", str(output), "--report", str(report_path)],
                cwd=folder, env=env, capture_output=True, text=True, timeout=timeout,
                check=False,
            )
            if completed.returncode == 0 and output.is_file() and report_path.is_file():
                decision = "output"
            elif completed.returncode == 2 and not output.exists() and not report_path.exists():
                decision = "safe_refusal"
            else:
                decision = "protocol_failure"
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            decision, exit_code = "timeout", None
        changed = digest(item.source) != item.sha256 or digest(copied) != item.sha256
        if changed:
            decision = "protocol_failure"
        if decision == "output" and (output.stat().st_size > MAX_SOURCE
                                     or report_path.stat().st_size > MAX_REPORT):
            decision = "protocol_failure"
        results.append({"id": item.identifier, "incident_id": item.incident_id,
                        "declared_cause": item.cause, "dataset": item.dataset,
                        "damaged_sha256": item.sha256, "damaged_size_bytes": item.size,
                        "decision": decision, "exit_code": exit_code,
                        "input_unchanged": not changed,
                        "output_sha256": digest(output) if output.is_file() else None,
                        "report_sha256": digest(report_path) if report_path.is_file() else None})
    summary = {key: sum(item["decision"] == key for item in results)
               for key in ("output", "safe_refusal", "timeout", "protocol_failure")}
    result = {"schema_version": 1,
              "experiment": "operator-declared incidents; damaged inputs only; no edits by runner",
              "cohort": cohort, "manifest_sha256": manifest_hash,
              "case_count": len(results), "distinct_incident_ids": len({x.incident_id for x in incidents}),
              "run_denominator_cases": len(results), "run_outcomes": summary,
              "cases": results,
              "truth_status": "not supplied to recovery or run stage; no historical accuracy scored",
              "limits": ("The runner cannot verify that submitted damage occurred naturally. "
                         "Consent and provenance are declarations, not independently audited. "
                         "Case submissions are self-selected, and multiple datasets can share an incident. "
                         "No field success probability can be inferred. The worker shares filesystem "
                         "permissions; do not place private truth on its host until after run completion.")}
    (workspace / "run.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                                        encoding="utf-8")
    return result


def _logical_bytes(dataset: h5py.Dataset, shape: tuple[int, ...]) -> bytes:
    if dataset.dtype.hasobject or dataset.dtype.itemsize < 1:
        raise IncidentError("object or variable-length truth cannot be scored bitwise")
    elements = math.prod(shape)
    if (not 1 <= elements <= MAX_ELEMENTS or elements * dataset.dtype.itemsize > MAX_DECODED
            or len(shape) > 4):
        raise IncidentError("dataset exceeds 1,048,576 elements, 16 MiB, or rank four")
    raw = np.ascontiguousarray(np.asarray(dataset[...])).tobytes(order="C")
    if len(raw) != elements * dataset.dtype.itemsize:
        raise IncidentError("decoded byte length differs from logical HDF5 element length")
    return raw


def capture_truth_hashes(source: Path, dataset: str, identifier: str,
                         provenance: str, destination: Path) -> dict[str, Any]:
    """Capture bounded, exact fixed-size element hashes while a source is healthy.

    This is a prospective tool. The function cannot establish whether the
    scientist actually ran it before any unobserved earlier damage.
    """
    if (not isinstance(identifier, str) or not IDENT.fullmatch(identifier)
            or not isinstance(dataset, str) or not dataset.startswith("/")
            or not isinstance(provenance, str) or not 1 <= len(provenance.strip()) <= 500):
        raise IncidentError("capture needs valid case id, dataset, and provenance")
    if source.is_symlink():
        raise IncidentError("capture source must not be a symlink")
    source = source.resolve(strict=True)
    if (not source.is_file() or source.stat().st_size > MAX_SOURCE
            or destination.exists() or destination.is_symlink()
            or not destination.parent.is_dir()):
        raise IncidentError("capture source or new destination is invalid")
    source_hash = digest(source)
    with h5py.File(source, "r") as file:
        selected = file[dataset]
        if not isinstance(selected, h5py.Dataset):
            raise IncidentError("capture selection is not a dataset")
        creation = selected.id.get_create_plist()
        if (creation.get_layout() == h5py.h5d.VIRTUAL or creation.get_external_count()
                or Path(selected.file.filename).resolve() != source):
            raise IncidentError("capture does not accept virtual, external storage, or external links")
        shape = tuple(selected.shape)
        if not 1 <= len(shape) <= 4 or math.prod(shape) > MAX_HASHES:
            raise IncidentError("capture needs 1..4 axes and at most 100,000 elements")
        values = _logical_bytes(selected, shape)
        width = selected.dtype.itemsize
        type_hash = hashlib.sha256(selected.id.get_type().encode()).hexdigest()
        element_hashes = {str(i): hashlib.sha256(values[i*width:(i+1)*width]).hexdigest()
                          for i in range(math.prod(shape))}
    if digest(source) != source_hash:
        raise IncidentError("source changed during prior-hash capture")
    document = {"schema_version": 1, "cases": [{
        "id": identifier, "kind": "element_hashes", "shape": list(shape),
        "hdf5_type_sha256": type_hash, "element_sha256": element_hashes,
        "provenance": provenance,
    }]}
    encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_MANIFEST:
        raise IncidentError("captured manifest exceeds 16 MiB")
    with destination.open("xb") as stream:
        stream.write(encoded)
    return {"case_id": identifier, "dataset": dataset, "source_sha256": source_hash,
            "element_count": math.prod(shape), "truth_sha256": digest(destination)}


def _acceptance(out: h5py.File, selected: h5py.Dataset, report: dict[str, Any]) -> np.ndarray:
    shape = tuple(selected.shape)
    if "/_h5reclaim/element_status" in out:
        codes = out["/_h5reclaim/element_status"][...]
        if codes.shape != shape or not np.isin(codes, (0, 1)).all():
            raise IncidentError("element validity map has invalid shape or codes")
        return np.asarray(codes == 1).reshape(-1)
    chunks = selected.chunks
    if chunks:
        address = next((key for key in ("/_h5reclaim/chunk_status", "/_h5reclaim/validity")
                        if key in out), None)
        if address is None:
            raise IncidentError("chunked output has no validity map")
        codes = out[address][...]
        grid = tuple((n+c-1)//c for n, c in zip(shape, chunks))
        if codes.shape != grid or not np.isin(codes, tuple(range(8))).all():
            raise IncidentError("chunk validity map has invalid shape or codes")
        mask = np.zeros(shape, dtype=bool)
        for coord in product(*(range(n) for n in grid)):
            slices = tuple(slice(i*c, min((i+1)*c, n))
                           for i, c, n in zip(coord, chunks, shape))
            mask[slices] = int(codes[coord]) == 1
        return mask.reshape(-1)
    if report.get("mode") in ("readable_export", "large_native_readable_export") \
            and report.get("validity_map") is None:
        return np.ones(math.prod(shape), dtype=bool)
    raise IncidentError("output has no explicit element validity decision")


def _truth_cases(path: Path, expected_sha: str, workspace: Path) -> tuple[str, dict[str, dict]]:
    if not SHA.fullmatch(expected_sha) or digest(path) != expected_sha:
        raise IncidentError("truth manifest differs from independently supplied SHA-256")
    doc = _document(path)
    if set(doc) != {"schema_version", "cases"} or doc["schema_version"] != 1 or not isinstance(doc["cases"], list):
        raise IncidentError("truth manifest needs schema_version 1 and cases")
    result = {}
    for row in doc["cases"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) \
                or not IDENT.fullmatch(row["id"]) or row["id"] in result:
            raise IncidentError("truth IDs must be valid and unique")
        kind = row.get("kind")
        if kind == "healthy_file":
            if set(row) != {"id", "kind", "path", "sha256", "size_bytes", "dataset", "provenance"}:
                raise IncidentError("healthy truth requires pinned file, dataset, and provenance")
            target = _safe_file(path.parent, row["path"])
            if target.is_relative_to(workspace) or path.is_relative_to(workspace):
                raise IncidentError("private truth must be outside the run work directory")
            _pin_file(target, row)
            if not isinstance(row["dataset"], str) or not row["dataset"].startswith("/"):
                raise IncidentError("healthy truth needs an absolute dataset path")
            row = dict(row, _path=str(target))
        elif kind == "element_hashes":
            if set(row) != {"id", "kind", "shape", "hdf5_type_sha256", "element_sha256", "provenance"}:
                raise IncidentError("hash truth needs captured type, shape, coordinates, and provenance")
            shape, hashes = row["shape"], row["element_sha256"]
            if (not isinstance(shape, list) or not 1 <= len(shape) <= 4
                or any(type(n) is not int or not 1 <= n for n in shape)
                or not 1 <= math.prod(shape) <= MAX_ELEMENTS
                or not isinstance(row["hdf5_type_sha256"], str)
                or not SHA.fullmatch(row["hdf5_type_sha256"])
                or not isinstance(hashes, dict) or not 1 <= len(hashes) <= MAX_HASHES):
                raise IncidentError("prior element hashes exceed bounds or lack type/shape")
            for ordinal, sha in hashes.items():
                if (not isinstance(ordinal, str) or not re.fullmatch(r"0|[1-9][0-9]*", ordinal)
                    or int(ordinal) >= math.prod(shape) or not isinstance(sha, str)
                    or not SHA.fullmatch(sha)):
                    raise IncidentError("invalid prior hash coordinate or digest")
        else:
            raise IncidentError("truth kind must be healthy_file or element_hashes")
        if not isinstance(row["provenance"], str) or not 1 <= len(row["provenance"].strip()) <= 500:
            raise IncidentError("each truth needs an independent provenance declaration")
        result[row["id"]] = row
    return digest(path), result


def _pin_case_artifacts(row: dict, workspace: Path) -> tuple[Path, Path, Path]:
    if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
            or not IDENT.fullmatch(row["id"]) or not isinstance(row.get("damaged_sha256"), str)
            or not SHA.fullmatch(row["damaged_sha256"])):
        raise IncidentError("invalid run receipt case identifier or damaged hash")
    folder = workspace / "cases" / row["id"]
    copied, output, report_path = (folder / name for name in ("damaged.h5", "output.h5", "report.json"))
    if folder.is_symlink() or copied.is_symlink() or output.is_symlink() or report_path.is_symlink():
        raise IncidentError("case artifacts must not be symlinks")
    if (not copied.is_file() or copied.stat().st_size != row["damaged_size_bytes"]
            or digest(copied) != row["damaged_sha256"]):
        raise IncidentError("case input, output, or report differs from run receipt")
    for path, key in ((output, "output_sha256"), (report_path, "report_sha256")):
        expected = row[key]
        if expected is None:
            if path.exists():
                raise IncidentError("unexpected case artifact after recovery")
        elif (not isinstance(expected, str) or not SHA.fullmatch(expected)
              or not path.is_file() or digest(path) != expected):
            raise IncidentError("case input, output, or report differs from run receipt")
    return copied, output, report_path


def _score_case(row: dict, truth: dict, workspace: Path) -> dict[str, Any]:
    copied, output, report_path = _pin_case_artifacts(row, workspace)
    if row["decision"] != "output":
        if truth["kind"] == "healthy_file":
            with h5py.File(truth["_path"], "r") as reference:
                selected = reference[truth["dataset"]]
                if not isinstance(selected, h5py.Dataset):
                    raise IncidentError("truth selection is not a dataset")
                _logical_bytes(selected, tuple(selected.shape))
                size = math.prod(selected.shape)
        else:
            size = math.prod(truth["shape"])
        return {"id": row["id"], "decision": row["decision"], "scoring": "refused"
                if row["decision"] == "safe_refusal" else "invalid",
                "exact_accepted_elements": 0, "wrong_accepted_elements": 0,
                "unknown_elements": 0, "unverified_accepted_elements": 0,
                "verified_elements": size, "refused_elements": size
                if row["decision"] == "safe_refusal" else 0}
    if report_path.stat().st_size > MAX_REPORT:
        raise IncidentError("published report exceeds evaluation size bound")
    report = _document(report_path, MAX_REPORT)
    if (not isinstance(report.get("source"), dict)
        or not isinstance(report.get("dataset"), dict)
        or report["source"].get("sha256_before") != row["damaged_sha256"]
        or report.get("source", {}).get("sha256_after") != row["damaged_sha256"]
        or report.get("dataset", {}).get("path") != row["dataset"]):
        raise IncidentError("recovery evidence does not bind source or selection")
    with h5py.File(output, "r") as recovered:
        selected = recovered[row["dataset"]]
        if not isinstance(selected, h5py.Dataset):
            raise IncidentError("output selection is not a dataset")
        shape = tuple(selected.shape)
        values = _logical_bytes(selected, shape)
        width = selected.dtype.itemsize
        accept = _acceptance(recovered, selected, report)
        embedded = recovered["/_h5reclaim/report_json"][()]
        if isinstance(embedded, bytes):
            embedded = embedded.decode("utf-8")
        if json.loads(embedded) != report:
            raise IncidentError("embedded and external evidence disagree")
        if truth["kind"] == "healthy_file":
            with h5py.File(truth["_path"], "r") as reference:
                original = reference[truth["dataset"]]
                if (not isinstance(original, h5py.Dataset) or original.shape != selected.shape
                        or not original.id.get_type().equal(selected.id.get_type())):
                    raise IncidentError("reference and output HDF5 schema differ")
                previous = _logical_bytes(original, shape)
            verification = range(accept.size)
            matches = {i: previous[i*width:(i+1)*width] == values[i*width:(i+1)*width]
                       for i in verification}
        else:
            if (list(shape) != truth["shape"]
                    or hashlib.sha256(selected.id.get_type().encode()).hexdigest()
                    != truth["hdf5_type_sha256"]):
                raise IncidentError("prior hashes describe a different HDF5 schema")
            matches = {int(i): hashlib.sha256(values[int(i)*width:(int(i)+1)*width]).hexdigest() == sha
                       for i, sha in truth["element_sha256"].items()}
        exact = sum(bool(accept[i]) and match for i, match in matches.items())
        wrong = sum(bool(accept[i]) and not match for i, match in matches.items())
        unknown = sum(not bool(accept[i]) for i in matches)
        unverified_accepted = int(np.count_nonzero(accept)) - exact - wrong
        scored = {"id": row["id"], "decision": "output", "scoring": "full"
                  if len(matches) == len(accept) else "partial",
                  "logical_elements": int(accept.size), "verified_elements": len(matches),
                  "exact_accepted_elements": exact, "wrong_accepted_elements": wrong,
                  "unknown_elements": unknown,
                  "unverified_accepted_elements": unverified_accepted,
                  "unverified_unknown_elements": int(accept.size)-len(matches)-unverified_accepted,
                  "refused_elements": 0,
                  "truth_kind": truth["kind"]}
    if (digest(copied) != row["damaged_sha256"]
            or digest(output) != row["output_sha256"]
            or digest(report_path) != row["report_sha256"]):
        raise IncidentError("case bytes changed while being evaluated")
    return scored


def score_incidents(run_path: Path, run_sha: str, truth_path: Path, truth_sha: str) -> dict[str, Any]:
    run_path, truth_path = run_path.resolve(strict=True), truth_path.resolve(strict=True)
    if not SHA.fullmatch(run_sha) or digest(run_path) != run_sha:
        raise IncidentError("run receipt differs from independently supplied SHA-256")
    run = _document(run_path, MAX_REPORT)
    if (run.get("schema_version") != 1 or not isinstance(run.get("cases"), list)
            or run.get("case_count") != len(run["cases"])
            or not all(isinstance(row, dict) and isinstance(row.get("id"), str)
                       for row in run["cases"])):
        raise IncidentError("invalid run receipt")
    workspace = run_path.parent
    truth_hash, truth = _truth_cases(truth_path, truth_sha, workspace)
    selected = {row["id"] for row in run["cases"]}
    if set(truth) - selected:
        raise IncidentError("truth has an ID absent from the run receipt")
    scores = []
    for row in run["cases"]:
        if row["id"] not in truth:
            try:
                _pin_case_artifacts(row, workspace)
                scores.append({"id": row["id"], "decision": row["decision"],
                               "scoring": "unscorable",
                               "reason": "no independently retained truth supplied"})
            except (IncidentError, OSError, KeyError, TypeError, ValueError) as exc:
                scores.append({"id": row.get("id"), "decision": row.get("decision"),
                               "scoring": "invalid", "reason": str(exc)[:300]})
        else:
            try:
                scores.append(_score_case(row, truth[row["id"]], workspace))
            except (IncidentError, OSError, KeyError, TypeError, ValueError) as exc:
                scores.append({"id": row["id"], "decision": row["decision"],
                               "scoring": "invalid", "reason": str(exc)[:300]})
    if digest(run_path) != run_sha or digest(truth_path) != truth_hash:
        raise IncidentError("run receipt or truth manifest changed while being evaluated")
    for row in truth.values():
        if row["kind"] == "healthy_file" and digest(Path(row["_path"])) != row["sha256"]:
            raise IncidentError("healthy truth changed while being evaluated")
    counted = [row for row in scores if row["scoring"] in ("full", "partial")]
    result = {"schema_version": 1, "experiment": "independently scored declared incidents",
              "run_sha256": run_sha, "truth_manifest_sha256": truth_hash,
              "case_count": len(scores), "distinct_incident_ids": run["distinct_incident_ids"],
              "supplied_truth_cases": len(truth),
              "scorable_output_cases": len(counted),
              "unscorable_cases": sum(row["scoring"] == "unscorable" for row in scores),
              "invalid_cases": sum(row["scoring"] == "invalid" for row in scores),
              "safe_refusal_cases_with_truth": sum(row["scoring"] == "refused" for row in scores),
              "verified_elements_in_outputs": sum(row["verified_elements"] for row in counted),
              "exact_accepted_elements": sum(row["exact_accepted_elements"] for row in counted),
              "wrong_accepted_elements": sum(row["wrong_accepted_elements"] for row in counted),
              "unknown_elements": sum(row["unknown_elements"] for row in counted),
              "refused_elements": sum(row["refused_elements"] for row in scores
                                      if row["scoring"] == "refused"),
              "unverified_accepted_elements": sum(row["unverified_accepted_elements"] for row in counted),
              "cases": scores,
              "limits": ("Reference match is conditional on the declared independent truth. Missing truth, "
                         "unverified coordinates, invalid cases and refusals are not successes. "
                         "No population denominator, random sampling, or field success estimate is implied.")}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    run = actions.add_parser("run", help="process a damaged-only, hash-pinned consented cohort")
    run.add_argument("--manifest", required=True, type=Path)
    run.add_argument("--work-dir", required=True, type=Path, help="new or empty private directory")
    run.add_argument("--python", default=sys.executable)
    run.add_argument("--timeout", type=int, default=90)
    run.add_argument("--json", action="store_true")
    score = actions.add_parser("score", help="score after recovery using a separate pinned truth manifest")
    score.add_argument("--run", required=True, type=Path)
    score.add_argument("--run-sha256", required=True)
    score.add_argument("--truth", required=True, type=Path)
    score.add_argument("--truth-sha256", required=True)
    score.add_argument("--output", required=True, type=Path, help="new score JSON path")
    score.add_argument("--json", action="store_true")
    capture = actions.add_parser("capture-hashes", help="prospectively pin fixed-size element truth")
    capture.add_argument("--source", required=True, type=Path)
    capture.add_argument("--dataset", required=True)
    capture.add_argument("--id", required=True)
    capture.add_argument("--provenance", required=True)
    capture.add_argument("--output", required=True, type=Path, help="new private truth manifest")
    capture.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "run":
            result = run_incidents(args.manifest, args.work_dir,
                                   python=args.python, timeout=args.timeout)
            destination = args.work_dir / "run.json"
            pin = digest(destination)
            readable = (f"H5Reclaim declared-incident intake: {result['case_count']} cases; "
                        f"{result['run_outcomes']['output']} outputs, "
                        f"{result['run_outcomes']['safe_refusal']} safe refusals, "
                        f"{result['run_outcomes']['protocol_failure']} protocol failures, "
                        f"{result['run_outcomes']['timeout']} timeouts.\n"
                        f"Receipt: {destination}\nReceipt SHA-256: {pin}\n"
                        "No healthy truth was given to recovery or scored in this step.")
        elif args.action == "score":
            if args.output.exists():
                raise IncidentError("score destination already exists")
            result = score_incidents(args.run, args.run_sha256, args.truth, args.truth_sha256)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8")
            readable = (f"H5Reclaim incident score: {result['scorable_output_cases']} scorable "
                        f"outputs / {result['case_count']} cases; "
                        f"{result['exact_accepted_elements']} reference-matching accepted, "
                        f"{result['wrong_accepted_elements']} wrong accepted, "
                        f"{result['unknown_elements']} verified unknown, "
                        f"{result['unverified_accepted_elements']} accepted without truth.\n"
                        f"Score: {args.output}\nThese counts are not a field success rate.")
        else:
            result = capture_truth_hashes(args.source, args.dataset, args.id,
                                          args.provenance, args.output)
            readable = (f"H5Reclaim prior truth capture: {result['element_count']} fixed-size elements.\n"
                        f"Private manifest: {args.output}\n"
                        f"Manifest SHA-256: {result['truth_sha256']}\n"
                        "Store this manifest and its digest independently before any later incident.")
        print(json.dumps(result, indent=2, sort_keys=True) if args.json else readable)
        if args.action == "run":
            return 0 if not result["run_outcomes"]["protocol_failure"] \
                and not result["run_outcomes"]["timeout"] else 1
        if args.action == "score":
            return 0 if not result["wrong_accepted_elements"] and not result["invalid_cases"] else 1
        return 0
    except (IncidentError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(2, f"incident evaluation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
