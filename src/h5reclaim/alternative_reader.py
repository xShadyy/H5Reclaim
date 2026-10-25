"""Optional Pyfive export with explicit cross-reader and candidate statuses.

The alternative reader never silently replaces a failed structural route.
Only fixed values that agree with native HDF5 on an independently preflighted
file are accepted. Other exported values remain masked inspection candidates.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from math import prod

import h5py
import numpy as np

from .large_streaming import LargeBudget, sparse_snapshot, _stream_blocks, _deadline
from .preflight import inspect_metadata_local
from .recovery import VERSION, RecoveryError, _validate_paths, _verify_source
from .output_annotations import ensure_group, metadata_group_for_path


def export_alternative(source, dataset_path, output, report_path, *, budget=None, published_output=None):
    try:
        import pyfive
    except ImportError as exc:
        raise ValueError("install h5reclaim[readers] to enable the optional Pyfive reader") from exc
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    budget = budget or LargeBudget()
    deadline = time.monotonic() + budget.max_seconds
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, copied):
        inspection = inspect_metadata_local(image, budget=budget)
        selected = next((r for r in inspection["objects"] if r["path"] == dataset_path and r.get("kind") == "dataset"), None)
        if selected is None or inspection["declared_dependencies"]:
            raise ValueError("alternative export needs a raw-checked local path with no declared soft/external/virtual dependencies")
        from contextlib import ExitStack
        with ExitStack() as stack:
            try:
                alternative = stack.enter_context(pyfive.File(str(image)))
                data = alternative[dataset_path]
            except Exception as exc:
                raise ValueError(f"Pyfive cannot open the selected metadata: {type(exc).__name__}: {exc}") from exc
            dtype, shape = np.dtype(data.dtype), tuple(data.shape)
            if (dtype.hasobject or dtype.subdtype or dtype.itemsize != selected["element_size"]
                    or list(shape) != selected.get("shape") or len(shape) > 32):
                raise ValueError("alternative reader schema disagrees with checked fixed-record metadata")
            total = prod(shape)
            if total * dtype.itemsize > budget.max_logical_bytes:
                raise ValueError("alternative data exceeds its logical byte quota")
            native = None
            if inspection["decision"] in ("accept", "accept_with_warnings") and inspection["analysis"]["complete"]:
                try:
                    native = stack.enter_context(h5py.File(image, "r"))[dataset_path]
                    if native.shape != shape or native.dtype != dtype:
                        native = None
                except (OSError, ValueError, RuntimeError, KeyError):
                    native = None
            with tempfile.TemporaryDirectory(prefix=".h5reclaim-alternative-", dir=output.parent) as outdir, \
                 tempfile.TemporaryDirectory(prefix=".h5reclaim-alternative-", dir=report_path.parent) as repdir:
                staged, saved = Path(outdir) / "output.h5", Path(repdir) / "report.json"
                accepted, candidates, failed, copied_attrs, omitted_attrs = 0, 0, 0, [], []
                hashes = hashlib.sha256()
                group_path = metadata_group_for_path(dataset_path)
                with h5py.File(staged, "x") as handle:
                    parent, leaf = dataset_path.rsplit("/", 1)
                    target = ensure_group(handle, parent or "/").create_dataset(leaf, shape=shape, dtype=dtype)
                    meta = ensure_group(handle, group_path)
                    status = meta.create_dataset("element_status", shape=shape, dtype="u1", fillvalue=3)
                    selections = [()] if not shape else [] if not total else _stream_blocks(shape, dtype.itemsize, budget.block_bytes)
                    failures = []
                    for selection in selections:
                        _deadline(deadline)
                        count = prod(tuple(s.stop - s.start for s in selection)) if selection else 1
                        try:
                            values = np.asarray(data[selection if selection else ()])
                            if values.dtype != dtype or values.size != count or values.nbytes > budget.block_bytes:
                                raise ValueError("alternative block schema or length changed")
                            target[selection if selection else ()] = values
                            checked = np.asarray(target[selection if selection else ()])
                            if checked.tobytes() != values.tobytes():
                                raise ValueError("alternative output differs on readback")
                            code = 2
                            if native is not None:
                                try:
                                    reference = np.asarray(native[selection if selection else ()])
                                    if reference.dtype == dtype and reference.tobytes() == values.tobytes():
                                        code = 1
                                except (OSError, ValueError, RuntimeError):
                                    pass
                            status[selection if selection else ()] = code
                            if code == 1:
                                accepted += count
                                hashes.update(checked.tobytes())
                            else:
                                candidates += count
                        except Exception as exc:
                            failed += count
                            if len(failures) < 64:
                                failures.append({"start": [s.start for s in selection], "reason": str(exc)[:400]})
                        handle.flush()
                        if staged.stat().st_size > budget.max_output_bytes or shutil.disk_usage(staged.parent).free < budget.disk_reserve_bytes:
                            raise ValueError("alternative output exceeds its disk quota")
                    try:
                        names = list(data.attrs)
                        if len(names) > 64:
                            raise ValueError("alternative attribute count exceeds its quota")
                        for name in names:
                            try:
                                value = np.asarray(data.attrs[name])
                                if value.shape != () or value.dtype.hasobject or value.nbytes > 4096 or len(name) > 128:
                                    raise ValueError("attribute is not a bounded fixed scalar")
                                target.attrs[name] = value
                                copied_attrs.append(name)
                            except Exception:
                                omitted_attrs.append(str(name)[:128])
                    except Exception:
                        omitted_attrs.append("attribute inventory unavailable or over budget")
                    codes = {"recovered": 1, "candidate": 2, "read_failed": 3}
                    status.attrs["codes_json"] = json.dumps(codes)
                    result = {"schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
                        "operation": "alternative_reader_export", "outcome": "complete" if accepted == total else "partial",
                        "source": {"path": str(source), "sha256_before": digest, "sha256_after": digest, "size_bytes": size},
                        "dataset": {"path": dataset_path, "shape": list(shape), "dtype": dtype.str, "chunks": None,
                            "attributes_copied": copied_attrs, "attributes_omitted": omitted_attrs},
                        "metadata_group": group_path, "element_status": status.name, "validity_codes": codes,
                        "accepted_elements": accepted, "unknown_elements": total - accepted,
                        "candidate_elements": candidates, "failed_elements": failed, "failed_reads": failures,
                        "native_value_sha256": hashes.hexdigest(), "output_path": str(published_output or output),
                        "reader": "pyfive", "raw_preflight": {"decision": inspection["decision"], "complete": inspection["analysis"]["complete"]},
                        "value_evidence": "Status 1 requires native and Pyfive value agreement after independent metadata preflight. Status 2 is an unaccepted alternative-reader candidate. No historical authenticity is established."}
                    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
                    meta.create_dataset("report_json", data=text, dtype=h5py.string_dtype("utf-8"))
                    meta.attrs["source_sha256"] = digest
                    handle.flush()
                saved.write_text(text)
                _verify_source(source, identity, digest)
                _validate_paths(source, output, report_path)
                os.link(staged, output)
                try:
                    os.link(saved, report_path)
                except Exception:
                    output.unlink(missing_ok=True)
                    raise
                return result
