"""Independently score redundant metadata and cache-image recovery.

The recovery CLI receives only a damaged file and output paths. Generated
values and original schemas are retained by this evaluator until it exits.
"""

import argparse
import ctypes
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.cache_image import inspect_cache_image
from h5reclaim.filter_metadata import pipeline_fields
from h5reclaim.format import H5File
from h5reclaim.metadata_fallback import _messages, _old_root_address
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.native_bindings import public_function
from h5reclaim.recovery import VERSION
from h5reclaim.repair_plan import _object_header

CASES = (
    "legacy_root_count",
    "legacy_dataset_count",
    "legacy_continuation_count",
    "legacy_symbol_understatement",
    "legacy_symbol_overflow_userblock",
    "nbit_count",
    "nbit_width",
    "nbit_pair_userblock",
    "nbit_resealed_pair",
    "scaleoffset_count",
    "scaleoffset_width",
    "scaleoffset_pair_userblock",
    "scaleoffset_resealed_pair",
    "cache_pending_metadata",
    "cache_corrupt_flushed",
    "cache_corrupt_pending_refused",
)


class CacheConfig(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_int),
        ("generate", ctypes.c_bool),
        ("resize", ctypes.c_bool),
        ("ageout", ctypes.c_int),
    ]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(source, case, values):
    userblock = 512 if "userblock" in case else 0
    if case.startswith("cache_"):
        config = CacheConfig(1, True, False, -1)
        configure = public_function(
            "H5Pset_mdc_image_config", [ctypes.c_longlong, ctypes.POINTER(CacheConfig)]
        )
        access = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
        access.set_libver_bounds(h5py.h5f.LIBVER_LATEST, h5py.h5f.LIBVER_LATEST)
        if configure(access.id, ctypes.byref(config)) < 0:
            raise RuntimeError("HDF5 cache-image configuration failed")
        opened = h5py.File(h5py.h5f.create(bytes(source), h5py.h5f.ACC_TRUNC, fapl=access))
    else:
        opened = h5py.File(
            source,
            "w",
            userblock_size=userblock,
            libver="earliest" if case.startswith("legacy_") else "latest",
        )
    with opened as handle:
        if case.startswith("nbit_"):
            typ = h5py.h5t.STD_I32LE.copy()
            typ.set_precision(16)
            creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            creation.set_chunk((4, 5))
            creation.set_filter(5, 0, ())
            selected = h5py.Dataset(
                h5py.h5d.create(
                    handle.id, b"data", typ, h5py.h5s.create_simple(values.shape), dcpl=creation
                )
            )
            selected[:] = values
        else:
            options = (
                {"scaleoffset": 0} if case.startswith("scaleoffset_") else {"compression": "gzip"}
            )
            selected = handle.create_dataset("data", data=values, chunks=(4, 5), **options)
        if case == "legacy_continuation_count":
            for index in range(35):
                selected.attrs[f"attribute-{index}"] = "bounded attribute value" * 5
        if "symbol" in case:
            handle.create_dataset("second", data=values + 1000)
        if case == "cache_corrupt_flushed":
            handle.flush()
        schema = selected.id.get_type().encode()
        address = int(h5py.h5o.get_info(selected.id).addr)
    raw = bytearray(source.read_bytes())
    if case.startswith("legacy_"):
        if "root_count" in case:
            with H5File(source) as reader:
                root, _ = _old_root_address(reader)
                at = reader.absolute(root) + 2
        elif "symbol" in case:
            at = raw.index(b"SNOD", userblock) + 6
        else:
            at = userblock + address + 2
        replacement = 1 if "understatement" in case else 65535
        raw[at : at + 2] = replacement.to_bytes(2, "little")
    elif case.startswith("cache_"):
        if "corrupt" in case:
            image = inspect_cache_image(source)
            raw[image["offset"] + image["bytes"] - 1] ^= 1
    else:
        with ModernH5File(source) as reader:
            start, header = _object_header(reader, address)
            message = next(m for m in _messages(reader, address) if m.kind == 11)
            _, params = pipeline_fields(message.data, 4)[0]
        fields = (2, 4) if "pair" in case else (4,) if "width" in case else (2,)
        for field in fields:
            at = message.absolute_offset + params + 4 * field
            raw[at : at + 4] = (12345).to_bytes(4, "little")
        if "resealed" in case:
            end = start + len(header)
            raw[end - 4 : end] = lookup3(raw[start : end - 4]).to_bytes(4, "little")
    source.write_bytes(raw)
    return schema


def run_case(root, case):
    folder = root / case
    folder.mkdir()
    source, output, report, plan, audit = (
        folder / name for name in ("source.h5", "out.h5", "report.json", "plan.json", "audit.jsonl")
    )
    values = np.arange(120, dtype="i4").reshape(12, 10)
    schema = fixture(source, case, values)
    before = digest(source)
    prefix = [sys.executable, "-m", "h5reclaim"]
    explicit = case.startswith("cache_") or "resealed" in case
    if explicit:
        flags = ["--discard-cache-image"] if case.startswith("cache_") else ["--allow-reseal"]
        args = [*prefix, "plan-repair", str(source), "--output", str(plan), *flags]
    else:
        args = [
            *prefix,
            "rescue",
            str(source),
            "--dataset",
            "/data",
            "--output",
            str(output),
            "--report",
            str(report),
        ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    refused = case.endswith("_refused")
    if refused:
        passed = (
            result.returncode == 1
            and plan.is_file()
            and not json.loads(plan.read_text())["executable"]
        )
        return {
            "case": case,
            "passed": passed and digest(source) == before,
            "decision": "refused",
            "source_unchanged": digest(source) == before,
            "wrong_values": 0,
            "accepted_values": 0,
        }
    if result.returncode:
        raise RuntimeError(f"{case} recovery failed: {result.stderr[-1000:]}")
    if explicit:
        applied = subprocess.run(
            [
                *prefix,
                "apply-repair",
                str(source),
                str(plan),
                "--output",
                str(output),
                "--audit",
                str(audit),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if applied.returncode:
            raise RuntimeError(f"{case} plan application failed: {applied.stderr[-1000:]}")
    with h5py.File(output, "r") as handle:
        selected = handle["data"]
        observed = selected[:]
        exact_schema = selected.shape == values.shape and selected.id.get_type().equal(
            h5py.h5t.decode(schema)
        )
        wrong = int(np.count_nonzero(observed != values))
    unchanged = digest(source) == before
    return {
        "case": case,
        "passed": unchanged and exact_schema and wrong == 0,
        "decision": "output",
        "source_unchanged": unchanged,
        "schema_exact": exact_schema,
        "wrong_values": wrong,
        "accepted_values": int(values.size),
        "output_sha256": digest(output),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "src/h5reclaim").glob("*.py")) + [
        root / "pyproject.toml",
        Path(__file__),
    ]

    def fingerprint():
        manifest = "".join(
            f"{path.relative_to(root).as_posix()} {digest(path)}\n" for path in sorted(paths)
        )
        return hashlib.sha256(manifest.encode("utf-8")).hexdigest()

    before = fingerprint()
    args.work_dir.mkdir(parents=True, exist_ok=False)
    cases = [run_case(args.work_dir, case) for case in CASES]
    after = fingerprint()
    result = {
        "schema_version": 1,
        "tool_version": VERSION,
        "cases": cases,
        "passed": before == after and all(case["passed"] for case in cases),
        "tested_source_sha256": after,
        "tested_source_unchanged": before == after,
        "scope": "generated controlled faults; CLI gets no original values; independently scored after exit",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"Metadata panel: {sum(case['passed'] for case in cases)}/{len(cases)} passed; "
        f"{sum(case['wrong_values'] for case in cases)} wrong values"
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
