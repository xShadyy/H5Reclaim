"""Atomic pre-incident protection bundle for one selected local dataset.

The bundle pins a recovery capsule and, when requested, a chunk baseline and
multi-erasure parity sidecar to one observed source hash. The archive contains
no source file. Its manifest digest must be retained separately: a digest
stored only beside the archive cannot establish independent provenance.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .baseline import capture_baseline
from .erasure_sidecar import MAX_ARCHIVE_BYTES, capture_erasure_sidecar, restore_from_erasure
from .recovery import (
    VERSION, RecoveryError, _identity, _validate_paths, _verify_source, sha256_file,
)
from .recovery_capsule import (
    MAX_CAPSULE_BYTES, MAX_REPORT_BYTES, capture_recovery_capsule, restore_from_capsule,
)


MAX_BASELINE_BYTES = 32 << 20
MAX_MANIFEST_BYTES = 16 << 10
MAX_DRILL_SOURCE_BYTES = 512 << 20
_MANIFEST = "manifest.json"
_PARTS = {"capsule.zip": MAX_CAPSULE_BYTES, "baseline.json": MAX_BASELINE_BYTES,
          "erasure.zip": MAX_ARCHIVE_BYTES + (40 << 20)}
MAX_BUNDLE_BYTES = sum(_PARTS.values()) + (1 << 20)
_SIGNATURE = b"\x89HDF\r\n\x1a\n"


def _digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise RecoveryError("protection manifest SHA-256 must be a lowercase digest")
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError(f"duplicate protection manifest key: {key}")
        result[key] = value
    return result


def _encode(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def _archive_parts(document: dict[str, Any]) -> list[str]:
    if (not isinstance(document, dict)
            or type(document.get("schema_version")) is not int or document["schema_version"] != 1
            or document.get("kind") != "prospective_protection_bundle"
            or set(document) != {"schema_version", "kind", "tool_version", "dataset_path",
                                 "captured_source", "observed_utc_unverified", "components",
                                 "retention_note"}):
        raise RecoveryError("unsupported protection manifest schema")
    captured = document["captured_source"]
    if (not isinstance(captured, dict) or set(captured) != {"name", "size_bytes", "sha256"}
            or not isinstance(captured["name"], str)
            or type(captured["size_bytes"]) is not int or captured["size_bytes"] < 0):
        raise RecoveryError("invalid captured source description")
    _digest(captured["sha256"])
    if (not isinstance(document["dataset_path"], str)
            or not document["dataset_path"].startswith("/")
            or not isinstance(document["observed_utc_unverified"], str)
            or not isinstance(document["tool_version"], str)
            or not isinstance(document["retention_note"], str)):
        raise RecoveryError("invalid protection manifest metadata")
    parts = document["components"]
    if not isinstance(parts, dict) or set(parts) not in (
        {"capsule.zip"}, {"capsule.zip", "baseline.json", "erasure.zip"},
    ):
        raise RecoveryError("invalid protection bundle components")
    for name, description in parts.items():
        if (not isinstance(description, dict) or set(description) != {"sha256", "size_bytes"}
                or type(description["size_bytes"]) is not int
                or not 0 < description["size_bytes"] <= _PARTS[name]):
            raise RecoveryError("invalid protection component description")
        _digest(description["sha256"])
    return sorted(parts)


def capture_protection_bundle(
    source: str | Path, dataset_path: str, destination: str | Path, *,
    include_erasure: bool = True, stripe_width: int = 4, parity_shards: int = 2,
) -> dict[str, Any]:
    """Capture and exclusively publish a complete versioned ZIP.

    All requested components must succeed for publication. `include_erasure`
    requires a complete supported numeric chunk grid. Capsule-only mode can
    cover additional fixed-record and sparse chunked datasets. A capture
    cannot prove the source was healthy before it was first observed here.
    """
    source, destination = Path(source), Path(destination)
    if type(include_erasure) is not bool:
        raise RecoveryError("include_erasure must be boolean")
    if not destination.parent.is_dir() or destination.exists() or destination.is_symlink():
        raise RecoveryError("protection destination must be a new file in an existing directory")
    if source.resolve(strict=True) == destination.resolve(strict=False):
        raise RecoveryError("protection destination aliases the source")
    before = _identity(source.stat())
    observed_hash = sha256_file(source)
    _verify_source(source, before, observed_hash)
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-protect-", dir=destination.parent) as tmp:
        stage = Path(tmp)
        capsule = capture_recovery_capsule(source, dataset_path, stage / "capsule.zip")
        if capsule["source_sha256"] != observed_hash:
            raise RecoveryError("capsule observed a different source version")
        names = ["capsule.zip"]
        if include_erasure:
            baseline = capture_baseline(source, dataset_path, stage / "baseline.json")
            if baseline["source_sha256"] != observed_hash:
                raise RecoveryError("baseline observed a different source version")
            erasure = capture_erasure_sidecar(
                source, dataset_path, stage / "baseline.json", stage / "erasure.zip",
                stripe_width=stripe_width, parity_shards=parity_shards,
            )
            if erasure["source_sha256"] != observed_hash:
                raise RecoveryError("erasure sidecar observed a different source version")
            names.extend(("baseline.json", "erasure.zip"))
        _verify_source(source, before, observed_hash)
        parts = {name: {"sha256": sha256_file(stage / name),
                        "size_bytes": (stage / name).stat().st_size} for name in names}
        document = {
            "schema_version": 1, "kind": "prospective_protection_bundle",
            "tool_version": VERSION, "dataset_path": capsule["schema"]["path"],
            "captured_source": {"name": source.name, "sha256": observed_hash,
                                "size_bytes": source.stat().st_size},
            "observed_utc_unverified": datetime.now(timezone.utc).isoformat(),
            "components": parts,
            "retention_note": (
                "Retain this ZIP and its separately recorded manifest SHA-256 in an independent "
                "failure domain before damage. The local clock and source correctness are not authenticated."
            ),
        }
        _archive_parts(document)
        manifest = _encode(document)
        if len(manifest) > MAX_MANIFEST_BYTES:
            raise RecoveryError("protection manifest exceeds its size limit")
        staged_zip = stage / "protection.zip"
        with zipfile.ZipFile(staged_zip, "x", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(_MANIFEST, manifest, compress_type=zipfile.ZIP_STORED)
            for name in sorted(names):
                archive.write(stage / name, name, compress_type=zipfile.ZIP_STORED)
        if staged_zip.stat().st_size > MAX_BUNDLE_BYTES:
            raise RecoveryError("protection bundle exceeds its size limit")
        with staged_zip.open("rb") as ready:
            os.fsync(ready.fileno())
        # Verify the staged archive, then check the original once more. Hard
        # linking is an exclusive atomic publication even if another process
        # creates the destination after our first existence check.
        pin = hashlib.sha256(manifest).hexdigest()
        verify_protection_bundle(staged_zip, pin, source=source)
        _verify_source(source, before, observed_hash)
        try:
            os.link(staged_zip, destination)
        except FileExistsError as exc:
            raise RecoveryError("protection destination appeared during capture") from exc
        return {**document, "manifest_sha256": pin, "bundle_sha256": sha256_file(destination),
                "bundle_path": str(destination)}


def verify_protection_bundle(
    bundle: str | Path, expected_manifest_sha256: str, *, source: str | Path | None = None,
) -> dict[str, Any]:
    """Verify exact member pins and optionally the unchanged source bytes."""
    bundle = Path(bundle)
    pin = _digest(expected_manifest_sha256)
    if not bundle.is_file() or bundle.is_symlink() or bundle.stat().st_size > MAX_BUNDLE_BYTES:
        raise RecoveryError("protection bundle is missing, linked, or exceeds its size limit")
    identity = _identity(bundle.stat())
    try:
        with zipfile.ZipFile(bundle) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 4 or len({entry.filename for entry in infos}) != len(infos):
                raise RecoveryError("protection bundle has invalid or duplicate entries")
            if any(info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1
                   or info.is_dir() or info.file_size != info.compress_size for info in infos):
                raise RecoveryError("protection bundle entries must be uncompressed regular files")
            head = archive.getinfo(_MANIFEST)
            if head.file_size > MAX_MANIFEST_BYTES:
                raise RecoveryError("protection manifest exceeds its size limit")
            raw = archive.read(_MANIFEST)
            if hashlib.sha256(raw).hexdigest() != pin:
                raise RecoveryError("protection manifest disagrees with separately retained SHA-256")
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
            names = _archive_parts(document)
            if {entry.filename for entry in infos} != {_MANIFEST, *names}:
                raise RecoveryError("protection bundle has unexpected or missing components")
            for name in names:
                row = document["components"][name]
                info = archive.getinfo(name)
                if info.file_size != row["size_bytes"]:
                    raise RecoveryError("protection component size differs from manifest")
                digest = hashlib.sha256()
                with archive.open(name) as stream:
                    while block := stream.read(1 << 20):
                        digest.update(block)
                if digest.hexdigest() != row["sha256"]:
                    raise RecoveryError("protection component differs from manifest SHA-256")
    except RecoveryError:
        raise
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RuntimeError, RecursionError,
            zipfile.BadZipFile) as exc:
        raise RecoveryError(f"protection bundle cannot be read safely: {exc}") from exc
    if _identity(bundle.stat()) != identity:
        raise RecoveryError("protection bundle changed during verification")
    if source is not None:
        source = Path(source)
        source_identity = _identity(source.stat())
        if (source.stat().st_size != document["captured_source"]["size_bytes"]
                or sha256_file(source) != document["captured_source"]["sha256"]):
            raise RecoveryError("current source differs from the captured source hash")
        _verify_source(source, source_identity, document["captured_source"]["sha256"])
    bundle_digest = sha256_file(bundle)
    if _identity(bundle.stat()) != identity:
        raise RecoveryError("protection bundle changed during verification")
    return {**document, "manifest_sha256": pin, "bundle_sha256": bundle_digest}


def drill_protection_bundle(
    bundle: str | Path, expected_manifest_sha256: str, source: str | Path,
) -> dict[str, Any]:
    """On disposable files, destroy the HDF signature and restore via capsule.

    The drill checks every allocated captured chunk and exact selected HDF5
    datatype against the live intact source. For bundles with parity, a
    second disposable copy loses up to two chunks in one stripe and must be
    rebuilt at the captured coordinates. Neither route proves capture time.
    """
    source, bundle = Path(source), Path(bundle)
    document = verify_protection_bundle(bundle, expected_manifest_sha256, source=source)
    if source.stat().st_size > MAX_DRILL_SOURCE_BYTES:
        raise RecoveryError("restore drill requires a source no larger than 512 MiB")
    import h5py
    import numpy as np

    with tempfile.TemporaryDirectory(prefix=".h5reclaim-drill-", dir=bundle.parent) as tmp:
        stage = Path(tmp)
        with zipfile.ZipFile(bundle) as archive:
            for name in document["components"]:
                with archive.open(name) as stream, (stage / name).open("xb") as target:
                    shutil.copyfileobj(stream, target, length=1 << 20)
                if sha256_file(stage / name) != document["components"][name]["sha256"]:
                    raise RecoveryError("protection component changed during restore drill")
        broken = stage / "broken.h5"
        shutil.copyfile(source, broken)
        with broken.open("r+b") as handle:
            signature = handle.read(min(1 << 20, source.stat().st_size)).find(_SIGNATURE)
            if signature < 0:
                raise RecoveryError("restore drill cannot locate HDF5 signature")
            handle.seek(signature)
            handle.write(b"\0" * len(_SIGNATURE))
        report = restore_from_capsule(
            broken, stage / "capsule.zip", document["components"]["capsule.zip"]["sha256"],
            stage / "recovered.h5", stage / "report.json", dataset_path=document["dataset_path"],
        )
        with h5py.File(source, "r") as intact, h5py.File(stage / "recovered.h5", "r") as repaired:
            old = intact[document["dataset_path"]]
            new = repaired[document["dataset_path"]]
            if not old.id.get_type().equal(new.id.get_type()) or old.shape != new.shape:
                raise RecoveryError("restore drill changed selected schema")
            allocated = 0
            for index in range(old.id.get_num_chunks()):
                origin = old.id.get_chunk_info(index).chunk_offset
                slices = tuple(slice(pos, min(pos + width, length))
                               for pos, width, length in zip(origin, old.chunks, old.shape))
                if np.asarray(old[slices]).tobytes() != np.asarray(new[slices]).tobytes():
                    raise RecoveryError("restore drill changed captured dataset bytes")
                allocated += 1
            if allocated != len(report["mappings"]) or report["partial_chunks"]:
                raise RecoveryError("restore drill failed to retain every captured chunk")
        erasure_restored = 0
        if "erasure.zip" in document["components"]:
            try:
                with zipfile.ZipFile(stage / "erasure.zip") as sidecar:
                    info = sidecar.getinfo("manifest.json")
                    if info.file_size > (32 << 20) or info.compress_type != zipfile.ZIP_STORED:
                        raise RecoveryError("parity drill manifest exceeds its bounds")
                    parity_doc = json.loads(sidecar.read("manifest.json").decode("utf-8"),
                                            object_pairs_hook=_unique)
                first = parity_doc["stripes"][0]["members"]
                if not isinstance(first, list) or not 1 <= len(first) <= 16:
                    raise RecoveryError("parity drill has invalid stripe coordinates")
                missing = [tuple(item) for item in first[:2]]
                if any(not isinstance(item, tuple) or not item or len(item) > 4
                       or any(type(axis) is not int or axis < 0 for axis in item)
                       for item in missing):
                    raise RecoveryError("parity drill has invalid chunk coordinates")
            except RecoveryError:
                raise
            except (OSError, ValueError, KeyError, IndexError, TypeError, UnicodeError,
                    RecursionError, zipfile.BadZipFile) as exc:
                raise RecoveryError(f"parity drill cannot inspect sidecar: {exc}") from exc
            parity_damaged = stage / "parity-damaged.h5"
            shutil.copyfile(source, parity_damaged)
            with h5py.File(source, "r") as intact, parity_damaged.open("r+b") as stream:
                selected = intact[document["dataset_path"]]
                for origin in missing:
                    if (selected.chunks is None or len(origin) != selected.ndim
                            or any(axis >= extent or axis % step
                                   for axis, extent, step in zip(origin, selected.shape, selected.chunks))):
                        raise RecoveryError("parity drill coordinate is outside the selected grid")
                    info = selected.id.get_chunk_info_by_coord(origin)
                    if info.byte_offset is None or info.size < 1:
                        raise RecoveryError("parity drill cannot locate selected chunk")
                    stream.seek(info.byte_offset)
                    prior = stream.read(1)
                    if len(prior) != 1:
                        raise RecoveryError("parity drill cannot read selected chunk")
                    stream.seek(info.byte_offset)
                    stream.write(bytes([prior[0] ^ 0x5a]))
            parity_manifest = stage / "parity-recovery.json"
            parity_manifest.write_bytes(_encode({
                "schema_version": 1, "damaged_sha256": sha256_file(parity_damaged),
                "baseline": {"path": str(stage / "baseline.json"),
                             "sha256": document["components"]["baseline.json"]["sha256"]},
                "erasure": {"path": str(stage / "erasure.zip"),
                            "sha256": document["components"]["erasure.zip"]["sha256"]},
            }))
            parity_report = restore_from_erasure(
                parity_damaged, document["dataset_path"], parity_manifest,
                stage / "parity-recovered.h5", stage / "parity-report.json",
            )
            if (not parity_report["complete"]
                    or parity_report["reconstructed_from_erasure"] != len(missing)):
                raise RecoveryError("parity restore drill did not rebuild the selected losses")
            with h5py.File(source, "r") as intact, h5py.File(
                stage / "parity-recovered.h5", "r",
            ) as repaired:
                old = intact[document["dataset_path"]]
                new = repaired[document["dataset_path"]]
                if not old.id.get_type().equal(new.id.get_type()) or old.shape != new.shape:
                    raise RecoveryError("parity restore drill changed selected schema")
                for index in range(old.id.get_num_chunks()):
                    origin = old.id.get_chunk_info(index).chunk_offset
                    slices = tuple(slice(pos, min(pos + width, length))
                                   for pos, width, length in zip(origin, old.chunks, old.shape))
                    if np.asarray(old[slices]).tobytes() != np.asarray(new[slices]).tobytes():
                        raise RecoveryError("parity restore drill changed captured dataset bytes")
            erasure_restored = len(missing)
        verify_protection_bundle(bundle, expected_manifest_sha256, source=source)
        return {"restored_allocated_chunks": allocated,
                "erasure_restored_chunks": erasure_restored,
                "source_sha256": document["captured_source"]["sha256"],
                "manifest_sha256": document["manifest_sha256"],
                "operation": "disposable_root_loss_restore_drill"}


def restore_from_protection_bundle(
    damaged: str | Path, dataset_path: str, bundle: str | Path,
    manifest_sha256: str, output: str | Path, report_path: str | Path, *,
    method: str = "capsule",
) -> dict[str, Any]:
    """Recover using only a damaged source and a retained, pinned bundle.

    Extracted components and the erasure route's generated input manifest live
    in a private temporary directory. The durable JSON and embedded HDF5
    report both refer to the bundle and its named, hashed members rather than
    their short-lived extraction paths. Both final destinations are new.
    """
    damaged, bundle = Path(damaged).absolute(), Path(bundle).absolute()
    output, report_path = Path(output), Path(report_path)
    if method not in ("capsule", "erasure") or type(method) is not str:
        raise RecoveryError("protection restore method must be capsule or erasure")
    _validate_paths(damaged, output, report_path)
    if not bundle.is_file() or bundle.is_symlink():
        raise RecoveryError("protection bundle must be a regular file")
    bundle_path = bundle.resolve(strict=True)
    if bundle_path == damaged.resolve(strict=True) or any(
        target.resolve(strict=False) == bundle_path for target in (output, report_path)
    ):
        raise RecoveryError("protection bundle, damaged source, output, and report must be separate")
    before_source = _identity(damaged.stat())
    damaged_sha = sha256_file(damaged)
    _verify_source(damaged, before_source, damaged_sha)
    protected = verify_protection_bundle(bundle, manifest_sha256)
    if method == "erasure" and "erasure.zip" not in protected["components"]:
        raise RecoveryError("protection bundle has no erasure sidecar")
    before_bundle = _identity(bundle.stat())
    bundle_sha = protected["bundle_sha256"]
    if sha256_file(bundle) != bundle_sha or _identity(bundle.stat()) != before_bundle:
        raise RecoveryError("protection bundle changed while it was opened")

    import h5py

    with tempfile.TemporaryDirectory(prefix=".h5reclaim-bundle-restore-", dir=output.parent) as tmp:
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-bundle-report-", dir=report_path.parent) as rep_tmp:
            stage = Path(tmp)
            components = (["capsule.zip"] if method == "capsule"
                          else ["baseline.json", "erasure.zip"])
            try:
                with zipfile.ZipFile(bundle) as archive:
                    for name in components:
                        expected = protected["components"][name]
                        info = archive.getinfo(name)
                        if (info.file_size != expected["size_bytes"]
                                or info.file_size > _PARTS[name]
                                or info.compress_type != zipfile.ZIP_STORED):
                            raise RecoveryError("protection component changed before extraction")
                        with archive.open(name) as stream, (stage / name).open("xb") as target:
                            shutil.copyfileobj(stream, target, length=1 << 20)
                        if sha256_file(stage / name) != expected["sha256"]:
                            raise RecoveryError("protection component changed during extraction")
            except RecoveryError:
                raise
            except (OSError, KeyError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
                raise RecoveryError(f"protection component cannot be extracted safely: {exc}") from exc
            _verify_source(bundle, before_bundle, bundle_sha)

            staged_output, staged_report = stage / "recovered.h5", stage / "report.json"
            if method == "capsule":
                original = restore_from_capsule(
                    damaged, stage / "capsule.zip",
                    protected["components"]["capsule.zip"]["sha256"],
                    staged_output, staged_report, dataset_path=dataset_path,
                )
            else:
                recovery_manifest = stage / "erasure-recovery.json"
                recovery_manifest.write_bytes(_encode({
                    "schema_version": 1, "damaged_sha256": damaged_sha,
                    "baseline": {"path": str(stage / "baseline.json"),
                                 "sha256": protected["components"]["baseline.json"]["sha256"]},
                    "erasure": {"path": str(stage / "erasure.zip"),
                                "sha256": protected["components"]["erasure.zip"]["sha256"]},
                }))
                original = restore_from_erasure(
                    damaged, dataset_path, recovery_manifest, staged_output, staged_report,
                )
            durable = dict(original)
            durable["protection_bundle"] = {
                "path": str(bundle_path), "sha256": bundle_sha,
                "manifest_sha256": protected["manifest_sha256"],
                "captured_source_sha256": protected["captured_source"]["sha256"],
                "components": protected["components"],
                "retention_note": protected["retention_note"],
            }
            if method == "capsule":
                durable["capsule"] = {**original["capsule"],
                                      "path": str(bundle_path), "bundle_member": "capsule.zip"}
            else:
                durable["manifest"] = {
                    "kind": "derived_from_protection_bundle",
                    "bundle_path": str(bundle_path),
                    "bundle_manifest_sha256": protected["manifest_sha256"],
                    "derived_sha256": original["manifest"]["sha256"],
                }
                durable["baseline"] = {**original["baseline"],
                                       "path": str(bundle_path), "bundle_member": "baseline.json"}
                durable["erasure"] = {**original["erasure"],
                                      "path": str(bundle_path), "bundle_member": "erasure.zip"}
            serialized = _encode(durable)
            if len(serialized) > MAX_REPORT_BYTES:
                raise RecoveryError("protection restore report exceeds its size limit")
            with h5py.File(staged_output, "r+") as opened:
                evidence = opened["/_h5reclaim/report_json"]
                evidence[()] = serialized.decode("utf-8")
                opened.flush()
            durable_report = Path(rep_tmp) / "report.json"
            durable_report.write_bytes(serialized)
            with h5py.File(staged_output, "r") as opened:
                if json.loads(opened["/_h5reclaim/report_json"][()]) != durable:
                    raise RecoveryError("embedded protection report differs after publication staging")
            _verify_source(damaged, before_source, damaged_sha)
            _verify_source(bundle, before_bundle, bundle_sha)
            _validate_paths(damaged, output, report_path)
            if any(target.resolve(strict=False) == bundle_path
                   for target in (output, report_path)):
                raise RecoveryError("protection output aliases the bundle")
            published = False
            try:
                os.link(staged_output, output)
                published = True
                os.link(durable_report, report_path)
            except Exception:
                if published:
                    output.unlink(missing_ok=True)
                raise
            return durable
