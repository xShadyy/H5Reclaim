"""Hash-pinned checkpoints for completed dataset exports."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from .recovery import VERSION, RecoveryError, sha256_file


class Checkpoint:
    """Reuse only complete, unchanged unit files from the same source/options.

    A checkpoint commits after both files are closed, read back and hashed.
    A killed or failed in-progress dataset is retried; completed datasets are
    retained. This deliberately does not claim to resume a half-written chunk.
    """

    def __init__(self, directory, digest, options):
        self.directory = Path(directory).absolute()
        if self.directory.is_symlink():
            raise RecoveryError("checkpoint directory cannot be a symlink")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        from .unit_checkpoint import acquire_lock
        self.lock = acquire_lock(self.directory, 'whole-file.lock')
        self.path = self.directory / "checkpoint.json"
        identity = {"schema_version": 1, "source_sha256": digest,
                    "tool_version": VERSION, "options": options}
        if self.path.exists():
            if self.path.is_symlink() or self.path.stat().st_size > 32 * 1024**2:
                raise RecoveryError("invalid checkpoint manifest")
            self.record = json.loads(self.path.read_text(encoding="utf-8"))
            if any(self.record.get(key) != value for key, value in identity.items()):
                raise RecoveryError("checkpoint belongs to a different source, version or recovery options")
        else:
            if any(path.name != 'whole-file.lock' for path in self.directory.iterdir()):
                raise RecoveryError("new checkpoint directory must be empty")
            self.record = {**identity, "units": {}}
            self._save()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.lock.close()

    def __del__(self):
        lock = getattr(self, 'lock', None)
        if lock is not None:
            lock.close()

    def _save(self):
        temporary = self.directory / "checkpoint.tmp"
        if temporary.is_symlink():
            raise RecoveryError("checkpoint temporary file cannot be a symlink")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.record, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def _paths(self, path):
        key = hashlib.sha256(path.encode("utf-8")).hexdigest()
        return key, self.directory / (key + ".h5"), self.directory / (key + ".json")

    def load(self, path):
        key, output, report = self._paths(path)
        record = self.record["units"].get(key)
        if record is None:
            return None
        if record["path"] != path:
            raise RecoveryError("checkpoint dataset identity is inconsistent")
        for file, field in ((output, "output_sha256"), (report, "report_sha256")):
            if file.is_symlink() or not file.is_file() or sha256_file(file) != record[field]:
                raise RecoveryError("checkpoint unit changed; refusing cached recovery")
        recovered = json.loads(report.read_text(encoding="utf-8"))
        if recovered.get("source", {}).get("sha256_before") != record.get("source_sha256", self.record["source_sha256"]):
            raise RecoveryError("checkpoint source digest is inconsistent")
        return recovered, output, report

    def store(self, path, output, report):
        key, saved_output, saved_report = self._paths(path)
        for source, target in ((output, saved_output), (report, saved_report)):
            temporary = target.with_suffix(target.suffix + ".tmp")
            if temporary.is_symlink() or target.is_symlink():
                raise RecoveryError("checkpoint unit cannot be a symlink")
            shutil.copyfile(source, temporary)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        self.record["units"][key] = {"path": path, "output_sha256": sha256_file(saved_output),
                                   "report_sha256": sha256_file(saved_report),
                                   "source_sha256": json.loads(saved_report.read_text(encoding='utf-8'))['source']['sha256_before']}
        self._save()
