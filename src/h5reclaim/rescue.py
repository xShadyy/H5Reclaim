"""Choose recovery routes from the source's observed storage condition."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import tempfile
import time

from .dependency_routes import inspect_superblock_status
from .format import FormatError, SIGNATURE
from .metadata import UnsupportedCase
from .recovery import RecoveryError
from .route_worker import run_route


def file_condition(source: Path) -> dict[str, Any]:
    size = source.stat().st_size
    with source.open("rb") as stream:
        offset = 0
        while offset + 12 <= size:
            stream.seek(offset)
            if stream.read(8) == SIGNATURE:
                return inspect_superblock_status(source, offset, size)
            offset = 512 if offset == 0 else offset * 2
    return {"outcome": "unavailable", "end_of_address": {"relation": "unknown"},
            "status_flags": {"interpretation": "unavailable"}}


def _coverage(report):
    """Compare actual element coverage across chunk and element routes."""
    if isinstance(report.get("accepted_elements"), int):
        return report["accepted_elements"]
    from math import prod
    selected = report.get("dataset", {})
    shape, chunks = selected.get("shape"), selected.get("chunks")
    if shape is not None and chunks:
        return sum(prod(min(width, axis - start) for start, width, axis in
                        zip(item["coordinate"], chunks, shape)) for item in report.get("mappings", []))
    return 0


def auto_rescue(source: str | Path, dataset: str, output: str | Path,
                report: str | Path, *, strict_history: bool = False,
                audit_science_context: bool = True,
                streaming_budget: dict[str, Any] | None = None,
                hints: str | Path | None = None,
                prefer_streaming: bool = False, resume_dir: str | Path | None = None) -> dict[str, Any]:
    """Recover a selected dataset, including automatic tail and streaming routes.

    A raw superblock observation chooses a candidate, but the chosen recovery
    implementation must independently validate it. A contradictory structural
    result stops the attempt instead of falling through to an ordinary read.
    """
    from .large_streaming import LargeBudget, sparse_snapshot
    from .recovery import _validate_paths, _verify_source, sha256_file
    from .source_session import reused_image, share_image

    source, output, report = Path(source).absolute(), Path(output), Path(report)
    _validate_paths(source, output, report)
    budget = LargeBudget(**(streaming_budget or {}))
    if reused_image(source) is not None:
        return _auto_image(source, dataset, output, report, budget=budget, hints=hints,
                           strict_history=strict_history, audit_science_context=audit_science_context,
                           prefer_streaming=prefer_streaming or streaming_budget is not None, resume_dir=resume_dir)
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, copied):
        with share_image(image, digest, size, copied, budget):
            result = _auto_image(image, dataset, output, report, budget=budget, hints=hints,
                                 strict_history=strict_history, audit_science_context=audit_science_context,
                                 original_source=source, original_identity=identity,
                                 prefer_streaming=prefer_streaming or streaming_budget is not None, resume_dir=resume_dir)
        if sha256_file(image) != digest:
            # _auto_image verifies before publication as well. This catches
            # mutations across leaving the shared-image context.
            raise RecoveryError("shared source image changed during rescue")
        return result


def _auto_image(source, dataset, output, report, *, budget, strict_history,
                audit_science_context, hints=None, original_source=None, original_identity=None,
                prefer_streaming=False, resume_dir=None):
    from dataclasses import asdict
    from .recovery import _validate_paths, _verify_source

    condition = file_condition(source)
    attempts: list[str] = []
    errors = []
    deadline = time.monotonic() + budget.max_seconds
    def attempt(route, directory):
        attempts.append(route)
        args = {"source": str(source), "dataset": dataset}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UnsupportedCase("automatic recovery exceeded its configured elapsed-time budget")
        args["streaming_budget"] = json.dumps({**asdict(budget), "max_seconds": remaining})
        if resume_dir is not None and route == "native_stream":
            args["resume_dir"] = str(Path(resume_dir).absolute())
        if hints is not None and route in ("structural", "native_stream"):
            args["hints"] = str(Path(hints).absolute())
        candidate_output, candidate_report = directory / (route + ".h5"), directory / (route + ".json")
        result = run_route(route, candidate_output, candidate_report, strict_history=strict_history,
                         annotate_history=True, audit_science_context=audit_science_context,
                         **args)
        from .large_streaming import _deadline
        _deadline(deadline)
        if candidate_output.stat().st_size > budget.max_output_bytes:
            raise UnsupportedCase("candidate output exceeds the configured output budget")
        selected = result.get("dataset", {})
        shape = selected.get("shape")
        if shape is not None:
            import h5py
            from math import prod
            with h5py.File(candidate_output, "r") as handle:
                size = handle[dataset].id.get_type().get_size()
            if prod(shape) * size > budget.max_logical_bytes:
                raise UnsupportedCase("candidate records exceed the configured logical byte budget")
            chunks = selected.get("chunks")
            if chunks:
                grid = prod((axis + width - 1) // width for axis, width in zip(shape, chunks))
                if grid > budget.max_grid:
                    raise UnsupportedCase("candidate chunk grid exceeds the configured map budget")
                allocated = len(result.get("mappings", [])) + len(result.get("failed_chunks", []))
                if allocated > budget.max_chunks:
                    raise UnsupportedCase("candidate allocations exceed the configured chunk budget")
        return result, candidate_output, candidate_report

    with tempfile.TemporaryDirectory(prefix=".h5reclaim-auto-", dir=output.parent) as temporary:
        directory = Path(temporary)
        root_trial = False
        try:
            from .metadata_correction import _superblock_raw
            from .modern_indexes import lookup3
            _, raw, _, _ = _superblock_raw(source)
            root_trial = lookup3(raw[:-4]) != int.from_bytes(raw[-4:], 'little')
        except UnsupportedCase:
            pass
        if condition["status_flags"].get("interpretation") == "write_flag_present":
            routes = ["status"]
        elif condition["end_of_address"].get("relation") == "past_physical_eof":
            routes = ["truncated_chunks", "nonchunked"]
        else:
            routes = (["native_stream"] if dataset.split('/')[1] == '_h5reclaim' or resume_dir is not None or root_trial else []) + ["structural", "nonchunked"] + ([] if prefer_streaming else ["readable"]) + ["native_stream", "large_structural"]
        chosen = None
        for route in routes:
            try:
                candidate = attempt(route, directory)
            except FormatError:
                raise
            except (RecoveryError, UnsupportedCase) as exc:
                errors.append({"route": route, "reason": str(exc)[:1200]})
                continue
            if chosen is None:
                chosen = candidate
            elif _coverage(candidate[0]) > _coverage(chosen[0]):
                chosen = candidate
            if candidate[0].get("counts", {}).get("decoder_unavailable", 0):
                # Keep the structural candidate until an available native
                # decoder has produced and verified a better candidate.
                continue
            if candidate[0].get("outcome") == "complete" or route != "structural":
                break
            # A structural decode failure is data evidence. Its partial
            # result is retained rather than silently reinterpreted.
            if not candidate[0].get("counts", {}).get("decoder_unavailable", 0):
                break
        if chosen is None:
            raise UnsupportedCase(f"Automatic routes {', '.join(attempts)} could not export {dataset}: "
                                  + "; ".join(item["reason"] for item in errors))
        result, staged_output, staged_report = chosen
        if hints is not None:
            from .hints import load_hints, compare_hints, require_no_conflicts
            from dataclasses import asdict
            selected = result["dataset"]
            comparisons = compare_hints(load_hints(Path(hints)), observed_fields={
                "path": dataset, "shape": selected.get("shape"), "chunks": selected.get("chunks"),
                "dtype": selected.get("dtype"), "filters": selected.get("filters", selected.get("filters_in_order"))},
                input_sha256=result["source"]["sha256_before"])
            require_no_conflicts(comparisons)
            result["operator_hints"] = {"trust_level": "unverified_operator_assertion",
                                         "comparisons": [asdict(item) for item in comparisons]}
        if original_source is not None:
            from .whole_file import _rebase
            from .output_annotations import report_metadata_group
            root = report_metadata_group(result)
            result = _rebase(result, {str(source): str(original_source)}, root, root)
        result["output_path"] = str(output)
        result["automatic_routing"] = {"attempted": attempts, "unavailable": errors,
                                        "selected": staged_output.stem}
        serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
        import h5py
        with h5py.File(staged_output, "r+") as handle:
            from .output_annotations import report_metadata_group
            handle[report_metadata_group(result) + "/report_json"][()] = serialized
        staged_report.write_text(serialized, encoding="utf-8")
        if staged_output.stat().st_size > budget.max_output_bytes:
            raise UnsupportedCase("annotated output exceeds the configured output budget")
        if original_source is not None:
            _verify_source(original_source, original_identity, result["source"]["sha256_before"])
            # Full image hashing is required before the first public output;
            # inner routes can reuse its checked identity and cached hash.
            import hashlib
            check = hashlib.sha256()
            with source.open("rb") as stream:
                while block := stream.read(budget.block_bytes):
                    check.update(block)
            if check.hexdigest() != result["source"]["sha256_before"]:
                raise RecoveryError("shared private image bytes changed")
        _validate_paths(original_source or source, output, report)
        from .large_streaming import _deadline
        _deadline(deadline)
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-auto-report-", dir=report.parent) as repdir:
            report_copy = Path(repdir) / "report.json"
            report_copy.write_text(serialized, encoding="utf-8")
            os.link(report_copy, report)
            try:
                os.link(staged_output, output)
            except Exception:
                report.unlink(missing_ok=True)
                raise
        return result
