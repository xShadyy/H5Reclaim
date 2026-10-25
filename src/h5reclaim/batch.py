"""Bounded file-batch recovery with hash-checked resumption and a saved index."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .large_streaming import LargeBudget
from .result_integrity import file_digest


def _save(path, record):
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-batch-", dir=path.parent) as directory:
        staged = Path(directory) / "summary.json"
        staged.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if path.is_symlink():
            raise ValueError("batch summary cannot be a symlink")
        os.replace(staged, path)


def batch_rescue(sources, output_dir, summary_path, *, jobs=1, resume=False,
                 budget=None, html=False, fail_on_partial=False):
    budget = budget or LargeBudget()
    from dataclasses import asdict
    from .recovery import VERSION
    options = {"budget": asdict(budget), "tool_version": VERSION}
    if type(jobs) is not int or not 1 <= jobs <= 4:
        raise ValueError("batch jobs must be in 1..4")
    sources = [Path(p).absolute() for p in sources]
    if not 1 <= len(sources) <= 1000 or len(set(sources)) != len(sources):
        raise ValueError("batch needs 1..1000 distinct source paths")
    output_dir, summary_path = Path(output_dir).absolute(), Path(summary_path).absolute()
    if output_dir.is_symlink() or summary_path.is_symlink() or summary_path in sources:
        raise ValueError("batch destinations must be separate nonsymlink paths")
    if any(not path.is_file() or path.is_symlink() for path in sources):
        raise ValueError("batch inputs must be existing regular nonsymlink files")
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    if summary_path.exists():
        if not resume or summary_path.stat().st_size > 16 << 20:
            raise FileExistsError("batch summary exists; use --resume to verify and reuse completed results")
        record = json.loads(summary_path.read_text())
        if record.get("kind") != "batch_recovery" or record.get("schema_version") != 1:
            raise ValueError("expected a version-1 batch summary")
        if record.get("options") != options:
            raise ValueError("batch belongs to a different tool version or recovery budget")
        if (not isinstance(record.get("results"), list) or len(record["results"]) > 1000
                or any(not isinstance(r, dict) or not isinstance(r.get("source"), str)
                       for r in record["results"])):
            raise ValueError("saved batch results have an invalid schema")
    else:
        record = {"schema_version": 1, "tool": "h5reclaim", "kind": "batch_recovery", "options": options, "results": []}
    previous = {r["source"]: r for r in record["results"]}
    results, pending = {}, []
    for source in sources:
        key = str(source)
        source_digest = file_digest(source, max_bytes=budget.max_source_bytes)
        old = previous.get(key)
        if resume and old and old.get("outcome") in ("complete", "partial"):
            if old.get("source_fingerprint") != source_digest:
                raise ValueError(f"source changed since the saved batch: {source.name}")
            for field in ("output", "report"):
                saved = Path(old[field]).absolute()
                if not saved.is_relative_to(output_dir) or saved.is_symlink():
                    raise ValueError("saved batch artifact is outside its output directory")
                if file_digest(saved, max_bytes=budget.max_output_bytes) != old[field + "_fingerprint"]:
                    raise ValueError("saved batch artifact changed; refusing cached recovery")
            results[key] = {**old, "reused": True}
        else:
            pending.append((source, source_digest))
    def run_one(source, source_digest):
        identifier = hashlib.sha256(str(source).encode()).hexdigest()[:16]
        folder = output_dir / identifier
        if folder.is_symlink():
            raise ValueError("batch case directory cannot be a symlink")
        folder.mkdir(exist_ok=True)
        output, report = folder / "recovered.h5", folder / "report.json"
        if output.exists() or report.exists():
            raise FileExistsError("untracked batch outputs already exist; choose a new directory")
        from dataclasses import asdict
        config = folder / "budget.json"
        if config.is_symlink():
            raise ValueError("batch configuration cannot be a symlink")
        config.write_text(json.dumps(asdict(budget)))
        args = [sys.executable, "-m", "h5reclaim", "rescue", str(source), "--output", str(output),
                "--report", str(report), "--streaming-budget", str(config)]
        item = {"source": str(source), "source_fingerprint": source_digest,
                "output": str(output), "report": str(report), "reused": False}
        try:
            with tempfile.TemporaryFile() as error, tempfile.TemporaryFile() as standard:
                process = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=standard, stderr=error,
                    timeout=budget.max_seconds + 30, check=False)
                if process.returncode != 0 or not report.is_file():
                    error.seek(0)
                    item.update(outcome="failed", reason=error.read(4000).decode("utf-8", errors="replace"))
                    return item
            if report.stat().st_size > 32 << 20:
                raise ValueError("batch evidence report exceeds its byte budget")
            evidence = json.loads(report.read_text(encoding="utf-8"))
            if evidence["source"]["sha256_before"] != source_digest["sha256"]:
                raise ValueError("batch input changed between intake and recovery")
            item["outcome"] = evidence["outcome"]
            item["output_fingerprint"] = file_digest(output, max_bytes=budget.max_output_bytes)
            item["report_fingerprint"] = file_digest(report, max_bytes=32 << 20)
            if html:
                from .html_report import write_html_report
                write_html_report(report, folder / "report.html")
        except Exception as exc:
            item.update(outcome="failed", reason=f"{type(exc).__name__}: {exc}"[:4000])
        return item
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(run_one, path, digest): str(path) for path, digest in pending}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                results[key] = {"source": key, "outcome": "failed", "reason": str(exc)[:4000]}
            record["results"] = [results[str(path)] for path in sources if str(path) in results]
            _save(summary_path, record)
    record["results"] = [results[str(path)] for path in sources]
    record["counts"] = {name: sum(r["outcome"] == name for r in record["results"])
                        for name in ("complete", "partial", "failed")}
    record["passed"] = record["counts"]["failed"] == 0 and (not fail_on_partial or record["counts"]["partial"] == 0)
    _save(summary_path, record)
    return record
