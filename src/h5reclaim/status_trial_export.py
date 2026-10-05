"""Export values readable only after a checked status-only disposable trial.

This does not declare an interrupted write safe or repair arbitrary corruption.
The original v3 superblock checksum is checked before a private copy's write
flags and checksum are changed. Every other source byte remains identical.
The resulting native export has the usual allocation, competing-owner, type,
filter, output readback, and resource limits.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import h5py

from .dependency_routes import SIGNATURE, inspect_superblock_status
from .format import FormatError
from .large_streaming import LargeBudget, _copy_sparse, _deadline, _hash_range, sparse_snapshot
from .metadata import UnsupportedCase
from .modern_indexes import lookup3
from .native_stream import export_native_stream
from .output_annotations import report_metadata_group
from .recovery import RecoveryError, _validate_paths, _verify_source


MAX_SIGNATURE_OFFSET = 1 << 20


def _signature_offset(source: Path, size: int) -> int:
    offset = 0
    with source.open("rb") as handle:
        while offset <= MAX_SIGNATURE_OFFSET and offset + len(SIGNATURE) <= size:
            handle.seek(offset)
            if handle.read(len(SIGNATURE)) == SIGNATURE:
                return offset
            offset = 512 if offset == 0 else offset * 2
    raise UnsupportedCase("no HDF5 signature at a bounded documented location")


def _checked_hash(path: Path, size: int, budget: LargeBudget, deadline: float) -> str:
    if path.stat().st_size != size:
        raise RecoveryError("status trial image size changed")
    with path.open("rb") as handle:
        return _hash_range(handle, 0, size, deadline=deadline, block_bytes=budget.block_bytes)


def _changed_trial_bytes(before: Path, after: Path, allowed: set[int], *,
                         budget: LargeBudget, deadline: float) -> tuple[int, list[int]]:
    count, unexpected = 0, []
    with before.open("rb") as original, after.open("rb") as trial:
        offset = 0
        while True:
            _deadline(deadline)
            chunk = original.read(budget.block_bytes)
            if not chunk:
                break
            altered = trial.read(len(chunk))
            if len(altered) != len(chunk):
                return count + 1, [offset + len(altered)]
            if chunk != altered:
                for position, (left, right) in enumerate(zip(chunk, altered)):
                    if left != right:
                        count += 1
                        if offset + position not in allowed and len(unexpected) < 8:
                            unexpected.append(offset + position)
            offset += len(chunk)
        if trial.read(1):
            return count + 1, [offset]
    return count, unexpected


def _status_only_trial(snapshot: Path, trial: Path, size: int, *,
                       budget: LargeBudget | None = None,
                       deadline: float | None = None) -> dict[str, Any]:
    budget = budget or LargeBudget()
    deadline = deadline if deadline is not None else time.monotonic() + budget.max_seconds
    _deadline(deadline)
    if size > budget.max_source_bytes:
        raise UnsupportedCase("status trial source exceeds the configured source byte budget")
    offset = _signature_offset(snapshot, size)
    observed = inspect_superblock_status(snapshot, offset, size)
    flags = observed["status_flags"]
    if (observed["outcome"] != "observed" or observed["superblock_version"] != 3
            or flags["interpretation"] != "write_flag_present"
            or flags["reserved_bits_present"]
            or observed["end_of_address"]["relation"] not in ("equal", "before_physical_eof")):
        raise UnsupportedCase("status trial requires v3 write flags, no reserved bits, and EOA within physical EOF")
    osize = observed["offset_size"]
    if osize not in (2, 4, 8):
        raise UnsupportedCase("status trial requires a supported superblock address width")
    header_size = 16 + 4 * osize
    if offset + header_size > size:
        raise FormatError("truncated version-3 superblock")
    with snapshot.open("rb") as handle:
        handle.seek(offset)
        header = handle.read(header_size)
    if (header[:8] != SIGNATURE or header[8] != 3 or header[9] != osize
            or header[10] not in (2, 4, 8)
            or int.from_bytes(header[-4:], "little") != lookup3(header[:-4])):
        raise FormatError("status trial needs a valid version-3 superblock checksum and widths")
    if int.from_bytes(header[12:12 + osize], "little") != offset:
        raise UnsupportedCase("relocated superblock base is outside status trial scope")
    altered = bytearray(header)
    altered[11] = 0
    altered[-4:] = lookup3(altered[:-4]).to_bytes(4, "little")
    with snapshot.open("rb") as original, trial.open("xb") as target:
        original_digest, copied = _copy_sparse(original, target, size=size, parent=trial.parent,
                                               budget=budget, deadline=deadline)
    with trial.open("r+b") as handle:
        handle.seek(offset)
        handle.write(altered)
    allowed = {offset + 11, *range(offset + header_size - 4, offset + header_size)}
    changed, unexpected = _changed_trial_bytes(snapshot, trial, allowed, budget=budget, deadline=deadline)
    if unexpected or changed < 1 or changed > 5 or trial.stat().st_size != size:
        raise RecoveryError("disposable status trial changed bytes outside the flag and checksum")
    after = inspect_superblock_status(trial, offset, size)
    if after["status_flags"]["raw"] != 0:
        raise RecoveryError("write flags remain in the disposable trial")
    return {
        "original_flags": int(flags["raw"]),
        "original_superblock_checksum_validated": True,
        "signature_offset": offset,
        "end_of_address_relation": observed["end_of_address"]["relation"],
        "changed_bytes": changed,
        "allowed_changed_offsets": sorted(allowed),
        "all_other_source_bytes_identical": True,
        "original_source_sha256": original_digest,
        "trial_sha256": _checked_hash(trial, size, budget, deadline),
        "trial_physical_data_bytes_copied": copied,
        "trial_was_disposable": True,
        "h5clear_was_not_run": True,
        "historical_measurements_verified": False,
    }


def export_status_trial(source: str | Path, dataset_path: str, output: str | Path,
                        report_path: str | Path, *, published_output: str | Path | None = None,
                        budget: LargeBudget | None = None,
                        ) -> dict[str, Any]:
    """Materialize current values from a status-cleared private copy only.

    Called by the bounded route worker, which publishes a successful staged
    pair. No status-cleared HDF5 container is published or written to source.
    """
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    budget = budget or LargeBudget()
    deadline = time.monotonic() + budget.max_seconds
    with sparse_snapshot(source, budget=budget) as (snapshot, digest, identity, size, copied):
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-status-", dir=output.parent) as directory:
            private = Path(directory)
            trial = private / "status-trial.h5"
            provisional_output = private / "native-output.h5"
            provisional_report = private / "native-report.json"
            details = _status_only_trial(snapshot, trial, size, budget=budget, deadline=deadline)
            if details["original_source_sha256"] != digest:
                raise RecoveryError("status trial copy disagrees with the captured source")
            _deadline(deadline)
            native_budget = replace(budget, max_seconds=deadline - time.monotonic())
            from .source_session import share_image
            with share_image(trial, details["trial_sha256"], size,
                             details["trial_physical_data_bytes_copied"], native_budget):
                exported = export_native_stream(
                    trial, dataset_path, provisional_output, provisional_report,
                    budget=native_budget, published_output=published_output or output,
                    inspection_image=trial,
                )
            if exported.get("mode") != "native_stream_export" or exported.get("checked_root_correction"):
                raise RecoveryError("status trial did not produce a native-readable result")
            report = dict(exported)
            report["mode"] = "status_trial_readable_export"
            report["operation"] = "status_only_disposable_copy_then_native_export"
            report["source"] = {
                "path": str(source), "size_bytes": size,
                "sha256_before": digest, "sha256_after": digest,
                "snapshot_physical_data_bytes_copied": copied,
            }
            report["status_trial"] = details
            report["output_path"] = str(published_output or output)
            report["limits"] = (
                "Only a validated v3 write-status flag was cleared in a disposable copy. "
                "The native library then copied currently readable values with validity and "
                "ownership checks. This does not prove the flag was stale or the historical "
                "measurements were unchanged; the original HDF5 file was not repaired."
            )
            serialized = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if len(serialized) > budget.max_metadata_bytes * 4:
                raise UnsupportedCase("status trial report exceeds the publication limit")
            with h5py.File(provisional_output, "r+") as handle:
                meta = handle[report_metadata_group(report)]
                del meta["report_json"]
                meta.create_dataset("report_json", data=serialized.decode("utf-8"),
                                    dtype=h5py.string_dtype(encoding="utf-8"))
                meta.attrs["source_sha256"] = digest
                handle.flush()
            if provisional_output.stat().st_size > budget.max_output_bytes:
                raise UnsupportedCase("status trial annotated output exceeds the configured output byte budget")
            provisional_report.write_bytes(serialized)
            if (_checked_hash(snapshot, size, budget, deadline) != digest
                    or _checked_hash(trial, size, budget, deadline) != details["trial_sha256"]):
                raise RecoveryError("private status trial or source snapshot changed during export")
            _verify_source(source, identity, digest)
            _validate_paths(source, output, report_path)
            _deadline(deadline)
            linked = False
            try:
                os.link(provisional_output, output)
                linked = True
                os.link(provisional_report, report_path)
            except OSError:
                if linked:
                    output.unlink(missing_ok=True)
                raise
            return report
