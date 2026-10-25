"""Portable fingerprints of a closed recovery output and its saved report."""

import hashlib
import json
import os
import tempfile
from pathlib import Path


def file_digest(path, *, max_bytes=64 << 30):
    if type(max_bytes) is not int or not 0 < max_bytes < 1 << 63:
        raise ValueError("hashing byte budget must be a positive bounded integer")
    path = Path(path)
    before = path.stat()
    if before.st_size > max_bytes:
        raise ValueError("artifact exceeds the configured hashing byte budget")
    digest, consumed = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            consumed += len(block)
            if consumed > max_bytes:
                raise ValueError("artifact grew beyond its hashing byte budget")
            digest.update(block)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("artifact changed while it was being hashed")
    return {"size_bytes": consumed, "sha256": digest.hexdigest()}


def fingerprint_result(output, report, destination, *, max_bytes=64 << 30):
    output, report, destination = map(Path, (output, report, destination))
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("fingerprint destination already exists")
    record = {"schema_version": 1, "tool": "h5reclaim", "kind": "result_fingerprint",
              "algorithm": "sha256", "output": file_digest(output, max_bytes=max_bytes),
              "report": file_digest(report, max_bytes=min(max_bytes, 256 << 20)),
              "scope": "exact artifact bytes at capture; retain this fingerprint separately to detect later changes"}
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-fingerprint-", dir=destination.parent) as directory:
        staged = Path(directory) / "fingerprint.json"
        staged.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.link(staged, destination)
    return record


def verify_fingerprint(output, report, fingerprint, *, max_bytes=64 << 30):
    with Path(fingerprint).open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("fingerprint exceeds 16 KiB")
    record = json.loads(raw)
    if (not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("tool") != "h5reclaim"
            or record.get("kind") != "result_fingerprint" or record.get("algorithm") != "sha256"):
        raise ValueError("expected an H5Reclaim SHA-256 result fingerprint")
    for key, path, limit in (("output", output, max_bytes), ("report", report, min(max_bytes, 256 << 20))):
        if record.get(key) != file_digest(path, max_bytes=limit):
            raise ValueError(f"{key} bytes differ from the separately retained fingerprint")
    return {"status": "passed", "scope": "exact output and report bytes match the supplied fingerprint"}
