"""Bounded child process for native HDF5 bundle and recovery routes.

Plugin loading is disabled before importing h5py. The parent publishes only
completed staged output and report files; a crash cannot leave half-published
destinations. This limits crashes and resources, not hostile-code execution.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .worker_limits import run_worker

MAX_REQUEST = 65536
MAX_REPORT = 32 * 1024 * 1024
ROUTES = frozenset({
    "family", "split", "vds", "external_raw", "external_link", "nonchunked",
    "replicas", "parity", "erasure", "status", "metadata_trial", "capsule",
    "truncated_chunks", "large_readable", "large_structural", "structural", "element_baseline", "chunk_baseline", "readable", "protection",
})


def _attach_scientific_context(output: str, report_path: str, args: dict[str, Any]) -> None:
    """Describe source context in both staged reports before publication."""
    from .scientific_context import audit_context
    from .recovery import RecoveryError

    report_file = Path(report_path)
    parsed = json.loads(report_file.read_text(encoding="utf-8"))
    selected = parsed.get("dataset", {})
    source = parsed.get("source", {})
    if not isinstance(selected, dict):
        raise RecoveryError("context audit needs a selected dataset record")
    if not isinstance(source, dict):
        source = {}
    requested = args.get("dataset", selected.get("source_path", selected.get("path", "")))
    copied = selected.get("attributes_copied", ())
    omitted = selected.get("attributes_omitted", ())
    if not isinstance(copied, list) or not isinstance(omitted, list):
        copied, omitted = [], ["selected attribute copy status was not recorded"]
    parsed["scientific_context"] = audit_context(
        args.get("source", source.get("path", "")), requested,
        source.get("sha256_before", ""), copied_attributes=copied,
        omitted_attributes=omitted,
    )
    serialized = json.dumps(parsed, indent=2, sort_keys=True) + "\n"
    if len(serialized.encode("utf-8")) > MAX_REPORT:
        raise RecoveryError("context audit exceeds the report publication limit")
    import h5py
    with h5py.File(output, "r+") as handle:
        metadata = handle.require_group("/_h5reclaim")
        if "report_json" in metadata:
            metadata["report_json"][()] = serialized
        else:
            metadata.create_dataset("report_json", data=serialized,
                                    dtype=h5py.string_dtype(encoding="utf-8"))
        handle.flush()
    report_file.write_text(serialized, encoding="utf-8")


def _child(request_path: Path, response_path: Path) -> int:
    try:
        raw = request_path.read_bytes()
        if len(raw) > MAX_REQUEST:
            raise ValueError("oversized route request")
        request = json.loads(raw)
        route = request["route"]
        if route not in ROUTES:
            raise ValueError("unsupported route")
        os.environ["HDF5_PLUGIN_PRELOAD"] = "::"
        os.environ.pop("HDF5_PLUGIN_PATH", None)
        os.environ.pop("HDF5_EXTFILE_PREFIX", None)
        from .native_worker import _apply_memory_limit
        _apply_memory_limit(int(request["memory_bytes"]))
        args: dict[str, Any] = request["args"]
        output, report = request["output"], request["report"]
        if route == "family":
            from .family_bundle import export_family
            export_family(args["manifest"], args["dataset"], output, report)
        elif route == "split":
            from .split_bundle import export_split
            export_split(args["manifest"], args["dataset"], output, report)
        elif route == "vds":
            from .vds_export import export_vds
            export_vds(args["source"], args["dataset"], output, report, args["manifest"],
                       published_output=request["published_output"])
        elif route == "external_raw":
            from .external_raw_export import export_external_raw
            export_external_raw(args["source"], args["dataset"], args["manifest"], output, report,
                                published_output=request["published_output"])
        elif route == "external_link":
            from .external_link_export import export_external_link
            export_external_link(args["source"], args["dataset"], args["manifest"], output, report,
                                 published_output=request["published_output"])
        elif route == "nonchunked":
            from .nonchunked_recovery import export_nonchunked
            export_nonchunked(args["source"], args["dataset"], output, report)
        elif route == "replicas":
            from .replica_recovery import restore_from_replicas
            restore_from_replicas(args["source"], args["dataset"], args["manifest"], output, report)
        elif route == "status":
            from .status_trial_export import export_status_trial
            export_status_trial(args["source"], args["dataset"], output, report,
                                published_output=request["published_output"])
        elif route == "element_baseline":
            from .payload_integrity import export_verified_nonchunked
            export_verified_nonchunked(args["source"], args["dataset"], args["baseline"],
                                       args["baseline_sha256"], output, report)
        elif route == "chunk_baseline":
            from .chunk_integrity import export_verified_chunks
            export_verified_chunks(args["source"], args["dataset"], args["baseline"],
                                   args["baseline_sha256"], output, report)
        elif route == "erasure":
            from .erasure_sidecar import restore_from_erasure
            restore_from_erasure(args["source"], args["dataset"], args["manifest"], output, report)
        elif route == "metadata_trial":
            from .metadata_trial_export import export_metadata_trial
            export_metadata_trial(args["source"], args["dataset"], output, report,
                                  kind=args["kind"], published_output=request["published_output"])
        elif route == "capsule":
            from .recovery_capsule import restore_from_capsule
            restore_from_capsule(args["source"], args["capsule"], args["capsule_sha256"],
                                 output, report, dataset_path=args["dataset"])
        elif route == "protection":
            from .protection_bundle import restore_from_protection_bundle
            restore_from_protection_bundle(
                args["source"], args["dataset"], args["bundle"],
                args["manifest_sha256"], output, report, method=args["method"],
            )
        elif route == "truncated_chunks":
            from .chunk_truncation import recover_truncated
            recover_truncated(args["source"], args["dataset"], output, report)
        elif route == "large_readable":
            from .large_streaming import export_large_readable
            export_large_readable(args["source"], args["dataset"], output, report,
                                  published_output=request["published_output"])
        elif route == "large_structural":
            from .large_structural import recover_large_fixed_array
            recover_large_fixed_array(args["source"], args["dataset"], output, report,
                                      published_output=Path(request["published_output"]))
        elif route == "structural":
            from .recovery import recover
            if args.get("hints"):
                from .hints import load_hints
                observed_hints = load_hints(Path(args["hints"]))
            else:
                observed_hints = None
            recover(Path(args["source"]), args["dataset"], Path(output), Path(report),
                    hints=observed_hints)
        elif route == "readable":
            # The outer worker is already isolated and has a resource limit;
            # avoid another process and preserve the final published path.
            from .readable_export import _export_readable_local
            _export_readable_local(
                args["source"], args["dataset"], output, report,
                published_output=Path(request["published_output"]),
                worker_budget={"wall_time_seconds": 900,
                               "address_space_cap_bytes": int(request["memory_bytes"]),
                               "dynamic_plugins_disabled": True},
            )
        else:
            from .parity_sidecar import restore_from_parity
            restore_from_parity(args["source"], args["dataset"], args["manifest"], output, report)
        if request.get("annotate_history") or request.get("strict_history"):
            from .historical_integrity import finalize_staged_history
            finalize_staged_history(output, report, strict=request.get("strict_history", False))
        if request.get("audit_science_context"):
            _attach_scientific_context(output, report, args)
        response = {"status": "ok"}
        code = 0
    except Exception as exc:
        response = {"status": "error", "kind": type(exc).__name__, "detail": str(exc)[:300]}
        code = 2
    response_path.write_text(json.dumps(response), encoding="utf-8")
    return code


def run_route(route: str, output: str | Path, report: str | Path, *,
              strict_history: bool = False, annotate_history: bool = False,
              audit_science_context: bool = False, **args: str) -> dict[str, Any]:
    """Run a route in an isolated native worker, then publish both files."""
    from .recovery import RecoveryError

    if route not in ROUTES:
        raise RecoveryError("unsupported export route")
    output, report = Path(output), Path(report)
    if not output.parent.is_dir() or not report.parent.is_dir():
        raise RecoveryError("output and report directories must exist")
    if (output.exists() or output.is_symlink() or report.exists() or report.is_symlink()
            or output.resolve(strict=False) == report.resolve(strict=False)):
        raise RecoveryError("output and report must be distinct new paths")
    if any(not isinstance(value, str) or len(value) > 4096 for value in args.values()):
        raise RecoveryError("invalid route argument")
    if any(type(flag) is not bool for flag in (strict_history, annotate_history, audit_science_context)):
        raise RecoveryError("route annotations must be Boolean flags")
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-route-", dir=output.parent) as outdir:
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-route-", dir=report.parent) as repdir:
            staged_output = Path(outdir) / "output.h5"
            staged_report = Path(repdir) / "report.json"
            request = Path(outdir) / "request.json"
            response = Path(outdir) / "response.json"
            data = json.dumps({"route": route, "args": args,
                               "strict_history": strict_history,
                               "annotate_history": annotate_history,
                               "audit_science_context": audit_science_context,
                               "output": str(staged_output),
                               "published_output": str(output),
                               "report": str(staged_report), "memory_bytes": 3 * 1024**3}).encode()
            if len(data) > MAX_REQUEST:
                raise RecoveryError("route request exceeds 64 KiB")
            request.write_bytes(data)
            environment = os.environ.copy()
            environment["HDF5_PLUGIN_PRELOAD"] = "::"
            environment.pop("HDF5_PLUGIN_PATH", None)
            environment.pop("HDF5_EXTFILE_PREFIX", None)
            try:
                result = run_worker(
                    [sys.executable, "-m", "h5reclaim.route_worker", str(request), str(response)],
                    env=environment, timeout_seconds=900, memory_bytes=3 * 1024**3,
                )
            except subprocess.TimeoutExpired as exc:
                raise RecoveryError("route worker exceeded the 900-second deadline") from exc
            try:
                if response.stat().st_size > 4096:
                    raise ValueError("oversized response")
                message = json.loads(response.read_bytes())
            except (OSError, ValueError) as exc:
                raise RecoveryError(f"route worker exited {result.returncode} without a valid response") from exc
            if result.returncode != 0 or message.get("status") != "ok":
                if message.get("kind") == "UnsupportedCase":
                    from .metadata import UnsupportedCase
                    raise UnsupportedCase(str(message.get("detail", "unsupported structural case"))[:300])
                if message.get("kind") == "FormatError":
                    from .format import FormatError
                    raise FormatError(str(message.get("detail", "contradictory format evidence"))[:300])
                raise RecoveryError(f"route worker failed: {str(message.get('detail', 'unknown error'))[:300]}")
            if not staged_output.is_file() or not staged_report.is_file() or staged_report.stat().st_size > MAX_REPORT:
                raise RecoveryError("route worker did not produce bounded output and report")
            parsed = json.loads(staged_report.read_text(encoding="utf-8"))
            if output.exists() or output.is_symlink() or report.exists() or report.is_symlink():
                raise RecoveryError("route destination appeared during export")
            # There is no filesystem atomic operation spanning two paths.
            # Publish the report first, so a visible output has its report;
            # roll back the report if the output link fails.
            os.link(staged_report, report)
            try:
                os.link(staged_output, output)
            except Exception:
                report.unlink(missing_ok=True)
                raise
            return parsed


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    return _child(Path(sys.argv[1]), Path(sys.argv[2]))


if __name__ == "__main__":
    raise SystemExit(main())
