"""Exercise a built installation without importing code from the checkout."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sysconfig
import tempfile
from pathlib import Path

import h5py
import numpy as np


def run() -> None:
    checkout = Path(__file__).resolve().parents[1]
    package = importlib.util.find_spec("h5reclaim")
    if package is None or package.origin is None:
        raise RuntimeError("h5reclaim is not installed")
    if Path(package.origin).resolve().is_relative_to(checkout):
        raise RuntimeError("this check requires a wheel installation, not the source checkout")
    command = Path(sysconfig.get_path("scripts")) / (
        "h5reclaim.exe" if os.name == "nt" else "h5reclaim"
    )
    if not command.is_file():
        raise RuntimeError(f"installed console command is missing: {command}")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="h5reclaim-installed-") as directory:
        folder = Path(directory)
        version = importlib.metadata.version("h5reclaim")
        version_result = subprocess.run(
            [str(command), "--version"], cwd=folder, env=environment,
            capture_output=True, text=True, timeout=30,
        )
        if version_result.returncode or version_result.stdout.strip() != f"h5reclaim {version}":
            raise RuntimeError(f"console version differs from installed metadata: {version_result}")
        source, output, report = (folder / name for name in (
            "source with spaces.h5", "recovered with spaces.h5", "evidence.json"
        ))
        values = np.arange(60, dtype="<i4").reshape(12, 5)
        labels = np.array(["detector A", "detector B", "μ sample"], dtype=object)
        with h5py.File(source, "w", libver="latest") as handle:
            handle.attrs["experiment"] = "installed package check"
            group = handle.create_group("experiment")
            group.attrs["units"] = "counts"
            group.create_dataset("readings", data=values, chunks=(4, 5),
                                 compression="gzip", shuffle=True, fletcher32=True)
            group.create_dataset("labels", data=labels, dtype=h5py.string_dtype())
        original_hash = hashlib.sha256(source.read_bytes()).digest()
        result = subprocess.run(
            [str(command), "rescue", str(source), "--all", "--output", str(output),
             "--report", str(report)],
            cwd=folder, env=environment, capture_output=True, text=True, timeout=90,
        )
        if result.returncode:
            raise RuntimeError(f"installed command failed:\n{result.stdout}\n{result.stderr}")
        if hashlib.sha256(source.read_bytes()).digest() != original_hash:
            raise RuntimeError("installed command changed its source")
        document = json.loads(report.read_text(encoding="utf-8"))
        if document.get("tool_version") != version:
            raise RuntimeError(f"report version differs from installed metadata: {document}")
        if document.get("outcome") != "complete" or document.get("datasets_exported") != 2:
            raise RuntimeError(f"installed recovery was incomplete: {document}")
        with h5py.File(output, "r") as handle:
            np.testing.assert_array_equal(handle["experiment/readings"][:], values)
            np.testing.assert_array_equal(handle["experiment/labels"].asstr()[:], labels)
            if handle.attrs.get("experiment") != "installed package check":
                raise RuntimeError("file attributes were not preserved")
            if handle["experiment"].attrs.get("units") != "counts":
                raise RuntimeError("group attributes were not preserved")
            embedded = handle[document.get("metadata_group", "/_h5reclaim") + "/report_json"][()]
            if json.loads(embedded) != document:
                raise RuntimeError("embedded evidence differs from the separate report")
        print("Installed wheel: exact numeric and UTF-8 values, context, report, and source preservation passed.")


if __name__ == "__main__":
    run()
