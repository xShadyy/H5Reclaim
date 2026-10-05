"""Independently compare every dataset and attribute in the intact corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024**2):
            result.update(block)
    return result.hexdigest()


def equal_values(expected, observed, source, output):
    """Compare fields and logical referents independently of recovery tokens."""
    if isinstance(expected, h5py.Reference):
        if bool(expected) != bool(observed):
            return False
        if not expected:
            return True
        if source[expected].name != output[observed].name:
            return False
        if isinstance(expected, h5py.RegionReference):
            old = h5py.h5r.get_region(expected, source.id)
            new = h5py.h5r.get_region(observed, output.id)
            if (old.get_simple_extent_dims() != new.get_simple_extent_dims()
                    or old.get_select_type() != new.get_select_type()
                    or old.get_select_npoints() != new.get_select_npoints()):
                return False
            selection = old.get_select_type()
            if selection == h5py.h5s.SEL_POINTS:
                return np.array_equal(old.get_select_elem_pointlist(), new.get_select_elem_pointlist())
            if selection == h5py.h5s.SEL_HYPERSLABS:
                return np.array_equal(old.get_select_hyper_blocklist(), new.get_select_hyper_blocklist())
            return selection in (h5py.h5s.SEL_ALL, h5py.h5s.SEL_NONE)
        return True
    if isinstance(expected, h5py.Empty):
        return isinstance(observed, h5py.Empty) and expected.dtype == observed.dtype
    a, b = np.asarray(expected), np.asarray(observed)
    if a.shape != b.shape or a.dtype != b.dtype:
        return False
    if a.dtype.names:
        return all(equal_values(a[name], b[name], source, output) for name in a.dtype.names)
    if a.dtype.hasobject:
        return all(equal_values(old, new, source, output) for old, new in zip(a.flat, b.flat))
    return a.tobytes() == b.tobytes()


def score(original, output_path, report_path):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    issues, datasets, attributes = [], 0, 0
    with h5py.File(original, "r") as source, h5py.File(output_path, "r") as output:
        paths = ["/"]
        source.visit(paths.append)
        for path in paths:
            old = source[path]
            if path not in output:
                issues.append(f"missing object: {path}")
                continue
            new = output[path]
            for name in old.attrs:
                attributes += 1
                if (name not in new.attrs or not old.attrs.get_id(name).get_type().equal(new.attrs.get_id(name).get_type())
                        or not equal_values(old.attrs[name], new.attrs[name], source, output)):
                    issues.append(f"attribute differs: {path}:{name}")
            if not isinstance(old, h5py.Dataset):
                continue
            datasets += 1
            if (not isinstance(new, h5py.Dataset) or old.shape != new.shape or old.maxshape != new.maxshape
                    or not old.id.get_type().equal(new.id.get_type())):
                issues.append(f"dataset schema differs: {path}")
                continue
            if old.shape is None or not old.shape:
                matches = equal_values(old[()], new[()], source, output)
            elif old.size == 0:
                matches = True
            else:
                step = max(1, 65536 // max(1, int(np.prod(old.shape[1:])) * old.dtype.itemsize))
                matches = all(equal_values(old[first:first + step], new[first:first + step], source, output)
                              for first in range(0, old.shape[0], step))
            if not matches:
                issues.append(f"dataset values differ: {path}")
        if json.loads(output[report.get('metadata_group', '/_h5reclaim') + "/report_json"][()]) != report:
            issues.append("embedded report differs")
    if report.get("outcome") != "complete" or report.get("datasets_exported") != datasets:
        issues.append("whole-file report is incomplete")
    return {"datasets_checked": datasets, "attributes_checked": attributes,
            "exact_datatypes_and_logical_values": not issues, "issues": issues}


def run(manifest_path=ROOT / "corpus" / "manifest.json"):
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    results = []
    with tempfile.TemporaryDirectory(prefix="h5reclaim-whole-corpus-") as directory:
        base = Path(directory)
        for item in document["entries"]:
            original = manifest_path.parent / item["path"]
            if digest(original) != item["sha256"]:
                raise ValueError("original corpus hash changed: " + item["id"])
            source, output, report = (base / (item["id"] + suffix) for suffix in (".h5", "-out.h5", ".json"))
            shutil.copyfile(original, source)
            process = subprocess.run([sys.executable, "-m", "h5reclaim", "rescue", str(source),
                "--output", str(output), "--report", str(report)], capture_output=True, text=True, timeout=900)
            if process.returncode:
                raise ValueError("whole-file recovery failed: " + process.stderr[:1200])
            result = score(original, output, report)
            result.update({"id": item["id"], "source_unchanged": digest(original) == digest(source) == item["sha256"]})
            results.append(result)
    return {"category": "intact-file export, with independent original comparison",
            "files": results, "datasets_checked": sum(item["datasets_checked"] for item in results),
            "attributes_checked": sum(item["attributes_checked"] for item in results),
            "passed": all(item["exact_datatypes_and_logical_values"] and item["source_unchanged"] for item in results)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Whole-file scientific corpus: {'PASS' if result['passed'] else 'FAIL'}")
        print(f"Checked {result['datasets_checked']} datasets and {result['attributes_checked']} attributes.")
        for item in result["files"]:
            print(f"{item['id']}: {item['datasets_checked']} datasets, {len(item['issues'])} issues")
        print("Intact scientific-file exports independently checked for exact datasets and attributes.")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
