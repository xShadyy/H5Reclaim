"""Build exact, hashed dependency manifests from an explicitly supplied directory."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import h5py

from .large_streaming import LargeBudget, _deadline, sparse_snapshot
from .recovery import RecoveryError, _verify_source
from .worker_limits import run_worker


def _declarations(image, budget):
    """Read local metadata without letting HDF5 open any declared dependency."""
    names, seen, links = set(), set(), 0
    with h5py.File(image, 'r') as handle:
        pending = [handle['/']]
        while pending:
            obj = pending.pop()
            address = int(h5py.h5o.get_info(obj.id).addr)
            if address in seen:
                continue
            seen.add(address)
            if len(seen) > budget.max_objects:
                raise RecoveryError('dependency discovery exceeds the object budget')
            if isinstance(obj, h5py.Group):
                for name in obj:
                    links += 1
                    if links > budget.max_links:
                        raise RecoveryError('dependency discovery exceeds the link budget')
                    link = obj.get(name, getlink=True)
                    if isinstance(link, h5py.ExternalLink):
                        names.add(link.filename)
                    elif isinstance(link, h5py.HardLink):
                        pending.append(obj[name])
            elif isinstance(obj, h5py.Dataset):
                creation = obj.id.get_create_plist()
                for index in range(creation.get_external_count()):
                    names.add(creation.get_external(index)[0])
                if obj.is_virtual:
                    for index in range(creation.get_virtual_count()):
                        names.add(creation.get_virtual_filename(index))
    return sorted(name.decode('utf-8') if isinstance(name, bytes) else name for name in names if name not in ('.', b'.'))


def _inspect(image, directory, budget):
    response = Path(directory) / 'declarations.json'
    environment = os.environ.copy()
    environment['HDF5_PLUGIN_PRELOAD'] = '::'
    environment.pop('HDF5_PLUGIN_PATH', None)
    environment.pop('HDF5_EXTFILE_PREFIX', None)
    try:
        result = run_worker([sys.executable, '-m', 'h5reclaim.related_manifest', str(image),
                             str(response), json.dumps(asdict(budget))], env=environment,
                            timeout_seconds=budget.max_seconds, memory_bytes=budget.worker_memory_bytes)
    except subprocess.TimeoutExpired as exc:
        raise RecoveryError('dependency discovery exceeded its deadline') from exc
    if not response.is_file() or response.stat().st_size > budget.max_metadata_bytes:
        raise RecoveryError('dependency discovery worker did not return a bounded response')
    message = json.loads(response.read_text(encoding='utf-8'))
    if result.returncode or message.get('status') != 'ok':
        raise RecoveryError(message.get('error', 'dependency metadata is unreadable'))
    return message['names']


def build_related_manifest(source, related_directory, output, *, budget=None):
    """Pin declared files inside the selected directory, including nested trees."""
    budget = budget or LargeBudget()
    source, root, output = Path(source).resolve(strict=True), Path(related_directory).resolve(strict=True), Path(output)
    if not root.is_dir() or output.exists() or output.is_symlink():
        raise RecoveryError('select a related-file directory and a new manifest path')
    deadline = time.monotonic() + budget.max_seconds
    entries, unresolved, captures, seen = {}, [], [], set()
    pending, total = deque([source]), 0
    with tempfile.TemporaryDirectory(prefix='h5reclaim-manifest-') as directory:
        while pending:
            _deadline(deadline)
            owner = pending.popleft()
            if owner in seen:
                continue
            seen.add(owner)
            with sparse_snapshot(owner, budget=budget) as (image, digest, identity, size, _):
                total += size
                if total > budget.max_source_bytes:
                    raise RecoveryError('related files exceed the aggregate source byte budget')
                captures.append((owner, identity, digest))
                # Raw segments have no HDF5 metadata to recurse into.
                if not h5py.is_hdf5(image):
                    continue
                names = _inspect(image, directory, budget)
            for name in names:
                if not name or '\x00' in name or len(name.encode('utf-8')) > budget.max_metadata_bytes:
                    raise RecoveryError('declared related-file name is invalid or exceeds the metadata budget')
                declared = Path(name)
                candidates = [declared] if declared.is_absolute() else [owner.parent / declared, root / declared]
                matched = {}
                for candidate in candidates:
                    if '%b' in str(candidate):
                        regex = re.compile('^' + re.escape(str(candidate)).replace('%b', r'([0-9]+)') + '$')
                        paths = candidate.parent.glob(candidate.name.replace('%b', '*'))
                    else:
                        regex, paths = None, [candidate]
                    for path in paths:
                        _deadline(deadline)
                        if not path.is_file() or regex is not None and not regex.fullmatch(str(path)):
                            continue
                        resolved = path.resolve(strict=True)
                        if not resolved.is_relative_to(root):
                            continue
                        exact = name
                        if regex is not None:
                            numbers = regex.fullmatch(str(path)).groups()
                            exact = name.replace('%b', numbers[0])
                        if exact in matched and matched[exact] != resolved:
                            raise RecoveryError('ambiguous related-file name; use an explicit manifest: ' + exact)
                        matched[exact] = resolved
                        if len(entries) + len(matched) > budget.max_links:
                            raise RecoveryError('related-file discovery exceeds the link budget')
                if not matched:
                    unresolved.append({'owner': str(owner), 'declared_name': name,
                                       'reason': 'no matching regular file inside the supplied directory'})
                for exact, path in matched.items():
                    if exact in entries:
                        if Path(entries[exact]['path']) != path:
                            raise RecoveryError('one declared name identifies multiple files; use an explicit manifest: ' + exact)
                        continue
                    with sparse_snapshot(path, budget=budget) as (_, digest, identity, _, _):
                        captures.append((path, identity, digest))
                    entries[exact] = {'declared_name': exact, 'path': str(path), 'sha256': digest}
                    pending.append(path)
        for path, identity, digest in captures:
            _verify_source(path, identity, digest)
        document = {'schema_version': 1, 'files': sorted(entries.values(), key=lambda item: item['declared_name'])}
        serialized = json.dumps(document, sort_keys=True, indent=2) + '\n'
        if len(serialized.encode('utf-8')) > budget.max_metadata_bytes:
            raise RecoveryError('related-file manifest exceeds the metadata byte budget')
        with tempfile.TemporaryDirectory(prefix='.h5reclaim-manifest-', dir=output.parent) as staging:
            staged = Path(staging) / 'manifest.json'
            staged.write_text(serialized, encoding='utf-8')
            os.link(staged, output)
    return {'manifest': document, 'unresolved': unresolved, 'complete': not unresolved}


def main():
    response = Path(sys.argv[2])
    try:
        from .native_worker import _apply_memory_limit
        budget = LargeBudget(**json.loads(sys.argv[3]))
        _apply_memory_limit(budget.worker_memory_bytes)
        result = {'status': 'ok', 'names': _declarations(Path(sys.argv[1]), budget)}
        code = 0
    except Exception as exc:
        result, code = {'status': 'error', 'error': str(exc)[:300]}, 2
    response.write_text(json.dumps(result), encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
