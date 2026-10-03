"""Use the shared native streamer with pinned Family and Split address spaces."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import h5py

from .large_streaming import LargeBudget, sparse_snapshot
from .recovery import RecoveryError, _verify_source, sha256_file


@contextmanager
def opened_bundle(kind, manifest, budget):
    """Open only private copies of the exact, verified members supplied."""
    if kind == 'family':
        from .family_bundle import _manifest
        member_size, members = _manifest(manifest)
        sources = [(Path(item['path']), item['sha256']) for item in members]
    elif kind == 'split':
        from .split_bundle import _load_manifest
        metadata, metadata_pin, raw, raw_pin = _load_manifest(manifest)
        sources = [(metadata, metadata_pin), (raw, raw_pin)]
    else:
        raise ValueError('unknown bundle driver')
    with tempfile.TemporaryDirectory(prefix='h5reclaim-bundle-') as directory, ExitStack() as stack:
        directory = Path(directory)
        captures, sizes, paths = [], [], []
        for number, (source, pin) in enumerate(sources):
            image, digest, identity, size, _ = stack.enter_context(sparse_snapshot(source, budget=budget))
            if digest != pin:
                raise RecoveryError('bundle member differs from its pinned SHA-256')
            staged = directory / (f'member{number:06d}.h5' if kind == 'family' else f'capture-{number}.h5')
            os.link(image, staged)
            if sha256_file(staged) != digest:
                raise RecoveryError('private bundle member changed while staged')
            captures.append((source, digest, identity))
            sizes.append(size)
            paths.append(staged)
        if sum(sizes) > budget.max_source_bytes:
            raise RecoveryError('bundle exceeds the configured source byte budget')
        if kind == 'family':
            from .family_bundle import _extent
            if any(size > member_size for size in sizes) or not sizes[0]:
                raise RecoveryError('Family member sizes contradict the supplied member size')
            handle = stack.enter_context(h5py.File(str(directory / 'member%06d.h5'), 'r', driver='family', memb_size=member_size))
            span = len(paths) * member_size
            def parts(address, length):
                return [(paths[item['member_index']], item['physical_offset'], item['length'])
                        for item in _extent(address, length, member_size, sizes)]
        else:
            from .split_bundle import inspect_split_map, _raw_range
            mapping = inspect_split_map(paths[0], sizes[1], max_bundle_bytes=budget.max_source_bytes)
            stem = directory / 'bundle'
            meta_ext, raw_ext = (name[2:] for name in mapping.stored_member_names)
            staged_metadata = Path(str(stem) + meta_ext)
            staged_raw = Path(str(stem) + raw_ext)
            os.link(paths[0], staged_metadata)
            os.link(paths[1], staged_raw)
            handle = stack.enter_context(h5py.File(stem, 'r', driver='split', meta_ext=meta_ext.encode(), raw_ext=raw_ext.encode()))
            span = mapping.raw_eoa
            def parts(address, length):
                if address < mapping.raw_address:
                    if address < 0 or length > sizes[0] - address:
                        raise RecoveryError('Split metadata allocation exceeds the pinned member')
                    return [(staged_metadata, address, length)]
                start, end = _raw_range(address, length, mapping, sizes[1])
                return [(staged_raw, start, end - start)]
        def range_hash(address, length):
            digest = hashlib.sha256()
            for path, offset, count in parts(address, length):
                with path.open('rb') as stream:
                    stream.seek(offset)
                    while count:
                        block = stream.read(min(count, budget.block_bytes))
                        if not block:
                            raise RecoveryError('bundle allocation ended inside a pinned range')
                        digest.update(block)
                        count -= len(block)
            return digest.hexdigest()
        yield handle, span, range_hash, captures, paths
        for source, digest, identity in captures:
            _verify_source(source, identity, digest)


def export_bundle_selected(kind, manifest, dataset, output, report, *, budget=None, resume_dir=None):
    from .native_stream import export_native_stream, inspect_storage
    from .readable_export import _selected_dataset
    import time
    from .filter_registry import register_optional
    register_optional()
    budget = budget or LargeBudget()
    with opened_bundle(kind, manifest, budget) as (handle, span, range_hash, captures, paths):
        selected = _selected_dataset(handle, dataset)
        _, ranges, _ = inspect_storage(selected, span, budget, time.monotonic() + budget.max_seconds)
        for start, end, _ in ranges:
            range_hash(start, end - start)
        context = {'driver': kind, 'members': [{'path': str(source), 'sha256': digest}
                                              for source, digest, _ in captures],
                   'address_space_bytes': span}
        checkpoint_digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode('utf-8')).hexdigest()
        def verify_members():
            for source, digest, identity in captures:
                _verify_source(source, identity, digest)
        return export_native_stream(paths[0], dataset, output, report, budget=budget,
                                     opened_file=handle, address_space_size=span, range_hash=range_hash,
                                     resume_dir=resume_dir, checkpoint_source_digest=checkpoint_digest,
                                     bundle_context=context, verify_related=verify_members)


def inventory_bundle(kind, manifest, directory, budget):
    from .source_session import worker_environment
    from .worker_limits import run_worker
    response = Path(directory) / 'bundle-inventory.json'
    environment = worker_environment()
    environment['HDF5_PLUGIN_PRELOAD'] = '::'
    environment.pop('HDF5_PLUGIN_PATH', None)
    command = [sys.executable, '-m', 'h5reclaim.bundle_stream', kind, str(Path(manifest).absolute()),
               str(response), json.dumps(asdict(budget))]
    try:
        result = run_worker(command, env=environment, timeout_seconds=budget.max_seconds + 30, memory_bytes=budget.worker_memory_bytes)
    except subprocess.TimeoutExpired as exc:
        raise RecoveryError('bundle inventory exceeded its deadline') from exc
    if not response.is_file() or response.stat().st_size > budget.max_metadata_bytes * 4:
        raise RecoveryError('bundle inventory did not produce a bounded response')
    parsed = json.loads(response.read_text(encoding='utf-8'))
    if result.returncode or parsed.get('status') != 'ok':
        raise RecoveryError(parsed.get('error', 'bundle inventory failed'))
    return parsed['inventory']


def main():
    response = Path(sys.argv[3])
    try:
        from .native_worker import _apply_memory_limit
        budget = LargeBudget(**json.loads(sys.argv[4]))
        _apply_memory_limit(budget.worker_memory_bytes)
        from .whole_file import _inventory_local
        from .source_session import activate_worker_session
        activate_worker_session()
        with opened_bundle(sys.argv[1], sys.argv[2], budget) as (handle, _, _, _, paths):
            result = {'status': 'ok', 'inventory': _inventory_local(paths[0], opened_file=handle, budget=budget)}
        code = 0
    except Exception as exc:
        result, code = {'status': 'error', 'error': str(exc)}, 2
    response.write_text(json.dumps(result), encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
