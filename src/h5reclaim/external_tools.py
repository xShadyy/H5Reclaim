"""Run official HDF5 copy/repack tools only against disposable input copies."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from .large_streaming import LargeBudget, sparse_snapshot
from .recovery import RecoveryError, VERSION, _validate_paths, _verify_source
from .result_integrity import file_digest
from .worker_limits import run_worker


def export_with_tool(source, output, report, *, operation="repack", dataset=None,
                     destination_path=None, compression=None, level=6, budget=None, executable=None):
    if operation not in ("copy", "repack") or compression not in (None, "none", "gzip", "lzf"):
        raise ValueError("unsupported copy/repack operation or filter")
    if not 0 <= level <= 9:
        raise ValueError("gzip level must be in 0..9")
    if operation == "copy" and (not dataset or not dataset.startswith("/") or dataset == "/"):
        raise ValueError("copy needs an absolute selected HDF5 object path")
    if destination_path is not None and (not destination_path.startswith("/") or destination_path == "/"):
        raise ValueError("copy destination path must be absolute")
    source, output, report = Path(source), Path(output), Path(report)
    _validate_paths(source, output, report)
    budget = budget or LargeBudget()
    name = "h5copy" if operation == "copy" else "h5repack"
    executable = executable or shutil.which(name)
    if executable is None:
        raise ValueError(f"{name} is not installed; install your platform's HDF5 command-line tools, then run h5reclaim doctor")
    if compression == "lzf":
        raise ValueError("official h5repack integration supports gzip or none; LZF requires an external filter and is not silently enabled")
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, copied), \
            tempfile.TemporaryDirectory(prefix=".h5reclaim-tool-", dir=output.parent) as outdir, \
            tempfile.TemporaryDirectory(prefix=".h5reclaim-tool-", dir=report.parent) as repdir:
        staged = Path(outdir) / "output.h5"
        if operation == "copy":
            args = [str(executable), "-i", str(image), "-o", str(staged), "-s", dataset,
                    "-d", destination_path or dataset, "-p"]
        else:
            args = [str(executable)]
            if compression is not None:
                args += ["-f", "NONE" if compression == "none" else f"GZIP={level}"]
            args += [str(image), str(staged)]
        request, response = Path(outdir) / "request.json", Path(outdir) / "response.json"
        request.write_text(json.dumps({"args": args, "output": str(staged), "budget": asdict(budget)}))
        env = os.environ.copy()
        env["HDF5_PLUGIN_PRELOAD"] = "::"
        env.pop("HDF5_PLUGIN_PATH", None)
        env.pop("HDF5_EXTFILE_PREFIX", None)
        child = run_worker([sys.executable, "-m", "h5reclaim.external_tools", str(request), str(response)],
            env=env, timeout_seconds=budget.max_seconds, memory_bytes=budget.worker_memory_bytes)
        if not response.is_file() or response.stat().st_size > 65536:
            raise RecoveryError(f"{name} worker stopped without a bounded result (exit {child.returncode})")
        checked = json.loads(response.read_text())
        if checked.get("status") != "completed" or child.returncode:
            raise RecoveryError(checked.get("reason", f"{name} failed"))
        if file_digest(image, max_bytes=budget.max_source_bytes)["sha256"] != digest:
            raise RecoveryError("official tool changed its disposable input copy")
        result = {"schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
            "operation": name, "status": "completed", "source": {"path": str(source), "sha256": digest},
            "output_path": str(output), "output_fingerprint": file_digest(staged, max_bytes=budget.max_output_bytes),
            "output_metadata": checked["output_metadata"],
            "scope": "official HDF5 tool completed on a disposable copy; this is a readable-file operation, not a recovery or historical-value verification"}
        saved = Path(repdir) / "report.json"
        saved.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        _verify_source(source, identity, digest)
        _validate_paths(source, output, report)
        os.link(staged, output)
        try:
            os.link(saved, report)
        except Exception:
            output.unlink(missing_ok=True)
            raise
        return result


def _worker_main():
    request, response = map(Path, sys.argv[1:])
    try:
        if request.stat().st_size > 65536:
            raise ValueError("tool request exceeds 64 KiB")
        document = json.loads(request.read_text())
        budget = LargeBudget(**document["budget"])
        from .native_worker import _apply_memory_limit
        _apply_memory_limit(budget.worker_memory_bytes)
        output = Path(document["output"])
        child = subprocess.Popen(document["args"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + budget.max_seconds
        try:
            while child.poll() is None:
                if (time.monotonic() > deadline or
                        (output.exists() and output.stat().st_size > budget.max_output_bytes)
                        or shutil.disk_usage(output.parent).free < budget.disk_reserve_bytes):
                    raise ValueError("official tool exceeded its elapsed-time or output disk budget")
                time.sleep(0.05)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
        if child.returncode:
            raise ValueError(f"official HDF5 tool returned exit {child.returncode}")
        if not output.is_file() or output.stat().st_size > budget.max_output_bytes:
            raise ValueError("official tool output is absent or exceeds the output quota")
        import h5py
        from .whole_file import _inventory_local
        inventory = _inventory_local(output, budget=budget)
        if not inventory["complete"]:
            raise ValueError("official tool output has an incomplete readable metadata inventory")
        result = {"status": "completed", "output_metadata": {
            "datasets": len(inventory["datasets"]), "groups": len(inventory["groups"])}}
        code = 0
    except Exception as exc:
        result, code = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:1000]}, 2
    response.write_text(json.dumps(result))
    return code


if __name__ == "__main__":
    raise SystemExit(_worker_main())
