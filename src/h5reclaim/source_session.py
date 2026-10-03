"""Reuse one checked private source image across isolated recovery routes."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

ENVIRONMENT_KEY = "H5RECLAIM_SOURCE_SESSION"
CURRENT_MANIFEST = ContextVar("h5reclaim_source_manifest", default=None)


def worker_environment():
    environment = os.environ.copy()
    if CURRENT_MANIFEST.get():
        environment[ENVIRONMENT_KEY] = CURRENT_MANIFEST.get()
    else:
        environment.pop(ENVIRONMENT_KEY, None)
    return environment


def activate_worker_session():
    """Called only by isolated workers receiving a parent-created session."""
    CURRENT_MANIFEST.set(os.environ.pop(ENVIRONMENT_KEY, None))


def reused_image(source: Path) -> dict | None:
    manifest = CURRENT_MANIFEST.get()
    if not manifest:
        return None
    from .recovery import RecoveryError, _identity
    path = Path(manifest)
    if path.is_symlink() or path.stat().st_size > 65536:
        raise RecoveryError("invalid source session manifest")
    record = json.loads(path.read_text(encoding="utf-8"))
    if Path(record["image"]).absolute() != Path(source).absolute():
        return None
    if (source.is_symlink() or list(_identity(source.stat())) != record["image_identity"]
            or len(record["sha256"]) != 64):
        raise RecoveryError("shared private source image changed")
    return record


@contextmanager
def share_image(image: Path, digest: str, size: int, copied: int, budget):
    from .recovery import _identity
    from dataclasses import asdict
    import tempfile

    with tempfile.TemporaryDirectory(prefix="h5reclaim-session-") as directory:
        manifest = Path(directory) / "session.json"
        manifest.write_text(json.dumps({"image": str(image.absolute()), "sha256": digest,
                            "image_identity": list(_identity(image.stat())), "size": size,
                            "copied": copied, "budget": asdict(budget)}), encoding="utf-8")
        manifest.chmod(0o600)
        token = CURRENT_MANIFEST.set(str(manifest))
        try:
            yield
        finally:
            CURRENT_MANIFEST.reset(token)
