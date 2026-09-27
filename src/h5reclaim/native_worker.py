"""Child process for bounded native HDF5 export.

Run as ``python -m h5reclaim.native_worker REQUEST RESPONSE``. The caller owns
the private staging directories and is responsible for publishing outputs.
The memory quota and plugin setting are applied before importing h5py.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MAX_REQUEST_BYTES = 65536


def _apply_memory_limit(max_bytes: int) -> int | None:
    if os.name == "nt":
        return None  # Windows currently has a wall-clock deadline, but no Job Object quota.
    try:
        import resource
    except ImportError:
        return None
    if hasattr(resource, "RLIMIT_AS"):
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        applied = min(max_bytes, hard) if hard >= 0 else max_bytes
        resource.setrlimit(resource.RLIMIT_AS, (applied, hard))
        return applied
    return None


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    response_path = Path(sys.argv[2])
    try:
        raw = Path(sys.argv[1]).read_bytes()
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("oversized native worker request")
        request = json.loads(raw)
        # Do not allow a parent environment to reenable arbitrary filters or VFDs.
        os.environ["HDF5_PLUGIN_PRELOAD"] = "::"
        os.environ.pop("HDF5_PLUGIN_PATH", None)
        applied_memory_limit = _apply_memory_limit(int(request["memory_bytes"]))

        from .hints import DatasetHints
        from .readable_export import _export_readable_local

        hint_fields = request.get("hints")
        if hint_fields is not None:
            for field in ("shape", "chunks", "filters"):
                if hint_fields.get(field) is not None:
                    hint_fields[field] = tuple(hint_fields[field])
        hints = DatasetHints(**hint_fields) if hint_fields is not None else None
        _export_readable_local(
            request["source"], request["dataset"], request["output"],
            request["report"], hints=hints,
            published_output=Path(request["published_output"]),
            worker_budget={
                "wall_time_seconds": request["timeout_seconds"],
                "address_space_cap_bytes": applied_memory_limit,
                "dynamic_plugins_disabled": True,
            },
        )
        response = {"status": "ok"}
        result = 0
    except Exception as exc:
        response = {"status": "error", "kind": type(exc).__name__, "detail": str(exc)[:300]}
        result = 2
    response_path.write_text(json.dumps(response), encoding="utf-8")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
