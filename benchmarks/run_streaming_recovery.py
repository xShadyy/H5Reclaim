"""Independently score compressed modern-index repairs against generated truth.

Original measurements are used only after the rescue process exits. This is
controlled fault injection, not a collection of naturally damaged incidents.
"""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
from math import prod

import h5py
import numpy as np

from h5reclaim.metadata import read_dataset_spec
from h5reclaim.modern_indexes import ModernH5File
from h5reclaim.result_integrity import file_digest
from h5reclaim.recovery import VERSION


CASES = (
    ("fa_2d_4mib_chunks", "fixed_array", (2048, 1024), (1024, 1024), "gzip", False, 0),
    ("ea_3d", "extensible_array", (72, 128, 128), (8, 32, 32), "gzip", False, 0),
    ("btree_2d", "v2_btree", (1100, 1000), (100, 100), "gzip", False, 0),
    ("fa_3d_lzf", "fixed_array", (33, 41, 37), (8, 8, 8), "lzf", False, 0),
    ("fa_2d_bad_payload", "fixed_array", (45, 33), (8, 8), "gzip", True, 0),
    ("fa_sparse_userblock_over_4gib", "fixed_array", (1100, 1000), (256, 250), "gzip", False, 1 << 32),
    ("ea_2d_two_broken_links", "extensible_array", (2, 2), (1, 1), "gzip", False, 0, "ea_two"),
    ("btree_2d_two_internal_links", "v2_btree", (180, 180), (2, 2), "gzip", False, 0, "bt_two"),
)


def run_case(root, case):
    name, kind, shape, chunks, codec, corrupt_payload, userblock = case[:7]
    damage = case[7] if len(case) > 7 else "single"
    folder = root / name
    folder.mkdir()
    source, output, report = (folder / n for n in ("damaged.h5", "recovered.h5", "report.json"))
    truth = np.arange(prod(shape), dtype="<i4").reshape(shape)
    maximum = (shape if kind == "fixed_array" else
               (1_000_000, None) if damage == "ea_two" else
               (None, *shape[1:]) if kind == "extensible_array" else (None,) * len(shape))
    with h5py.File(source, "w", libver="latest", userblock_size=userblock) as handle:
        handle.create_dataset("science", data=truth, maxshape=maximum, chunks=chunks,
                              compression=codec, shuffle=True, fletcher32=True)
    spec = read_dataset_spec(source, "/science", max_elements=16 << 30, max_chunk_bytes=64 << 20)
    with ModernH5File(source, large_sparse_scan=True, max_chunks=65536) as reader:
        index = reader.read_index(spec.object_address, shape, chunks, 4,
                                  maxshape=maximum, filters=spec.filters, max_chunks=65536)
        if index.index_type != kind:
            raise ValueError("HDF5 writer selected an unexpected index")
        if damage == "ea_two":
            secondary = next(step for item in index.chunks
                for step in item.evidence["index_chain"] if step["kind"] == "easb_to_eadb")
            pointers = [index.data_block_pointer_offset, secondary["pointer_offset"]]
        elif damage == "bt_two":
            parents = {}
            for item in index.chunks:
                path = item.evidence["link_path"]
                if len(path) >= 3:
                    parents.setdefault(path[-1]["source_address"], path[-1]["pointer_offset"])
                if len(parents) == 2:
                    break
            if len(parents) != 2:
                raise ValueError("HDF5 writer did not produce two independent internal parents")
            pointers = list(parents.values())
        else:
            pointers = [reader.absolute(index.base_address) + 16 if kind == "v2_btree"
                        else index.data_block_pointer_offset]
        payload = reader.absolute(index.chunks[0].address)
    with source.open("r+b") as stream:
        for offset in [*pointers, *([payload + 2] if corrupt_payload else [])]:
            stream.seek(offset)
            value = stream.read(1)
            stream.seek(offset)
            stream.write(bytes([value[0] ^ 0x40]))
    before = file_digest(source)
    started = time.monotonic()
    process = subprocess.run([sys.executable, "-m", "h5reclaim", "rescue", str(source),
        "--dataset", "/science", "--large-structural", "--output", str(output), "--report", str(report)],
        capture_output=True, text=True, timeout=180)
    if process.returncode:
        raise ValueError("public recovery failed: " + process.stderr[:1000])
    elapsed = time.monotonic() - started
    evidence = json.loads(report.read_text())
    correct, wrong, unknown = 0, 0, 0
    with h5py.File(output, "r") as handle:
        data, status = handle["science"], handle[evidence["validity_map"]]
        if data.shape != shape or data.dtype != truth.dtype:
            raise ValueError("recovered schema differs from independent truth")
        for cell in np.ndindex(status.shape):
            selection = tuple(slice(i * c, min((i + 1) * c, n)) for i, c, n in zip(cell, chunks, shape))
            count = truth[selection].size
            if status[cell] == 1:
                matches = int(np.count_nonzero(data[selection] == truth[selection]))
                correct += matches
                wrong += count - matches
            else:
                unknown += count
    return {"case": name, "index": kind, "shape": list(shape), "chunks": list(chunks),
        "compression": codec, "decoded_chunk_bytes": prod(chunks) * 4,
        "source_bytes": before["size_bytes"], "logical_bytes": truth.nbytes,
        "source_unchanged": before == file_digest(source), "elapsed_seconds": round(elapsed, 4),
        "snapshot_physical_bytes_copied": evidence["source"]["snapshot_physical_data_bytes_copied"],
        "exact_accepted_elements": correct, "wrong_accepted_elements": wrong, "unknown_elements": unknown,
        "fully_exact": correct == truth.size and wrong == 0,
        "partial_expected": corrupt_payload, "broken_index_links": len(pointers),
        "reported_index_repairs": len(evidence.get("index", {}).get("repairs", [])),
        "passed": (wrong == 0 and correct > 0 and
                   len(evidence.get("index", {}).get("repairs", [])) == len(pointers) and
                   (unknown > 0 if corrupt_payload else unknown == 0))}


def run(root, include_large_sparse=True):
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("benchmark work directory must be empty")
    cases = [run_case(root, case) for case in CASES if include_large_sparse or not case[6]]
    return {"schema_version": 1, "tool_version": VERSION,
        "category": "controlled compressed multidimensional index-link recovery through the public CLI",
        "environment": {"python": platform.python_version(), "h5py": h5py.__version__,
            "hdf5": h5py.version.hdf5_version, "numpy": np.__version__, "platform": platform.system()},
        "cases": cases, "passed": all(c["passed"] and c["source_unchanged"] for c in cases),
        "limits": f"{len(cases)} generated layouts and declared faults. Two cases damage distinct index parents. The >4 GiB case, when included, uses a sparse user block and small physically allocated data; it is an address-space check, not a dense multi-gigabyte throughput claim. Accepted coordinates are scored against NumPy truth after recovery exits."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-large-sparse", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="h5reclaim-streaming-panel-") as temporary:
        result = run(args.work_dir or Path(temporary), not args.no_large_sparse)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
