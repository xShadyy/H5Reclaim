"""Verified, atomic value checkpoints independent of an open HDF5 output."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .recovery import VERSION, RecoveryError, sha256_file


def acquire_lock(directory, name):
    path = Path(directory) / name
    if path.is_symlink():
        raise RecoveryError('checkpoint lock is a symlink')
    lock = path.open('a+b')
    try:
        lock.seek(0)
        lock.write(b'0')
        lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock.close()
        raise RecoveryError('another recovery is using these checkpoints') from exc
    return lock


class UnitCheckpoint:
    """Cache completed selections; interrupted writes never enter the journal."""

    def __init__(self, directory, source_digest, dataset, datatype, shape, chunks, block_bytes):
        self.directory = Path(directory).absolute()
        if self.directory.is_symlink():
            raise RecoveryError('selection checkpoint directory cannot be a symlink')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = acquire_lock(self.directory, 'running.lock')
        self.manifest = self.directory / 'selections.json'
        identity = {'version': VERSION, 'source_sha256': source_digest,
                    'dataset': dataset, 'datatype': datatype.encode().hex(),
                    'shape': shape, 'chunks': chunks, 'block_bytes': block_bytes}
        identity = json.loads(json.dumps(identity))
        if self.manifest.exists():
            if self.manifest.is_symlink() or self.manifest.stat().st_size > 128 * 1024**2:
                raise RecoveryError('invalid selection checkpoint manifest')
            self.record = json.loads(self.manifest.read_text(encoding='utf-8'))
            if self.record.get('identity') != identity:
                raise RecoveryError('selection checkpoints belong to a different source, dataset or schema')
        else:
            if any(path.name != 'running.lock' for path in self.directory.iterdir()):
                raise RecoveryError('new selection checkpoint directory must be empty')
            self.record = {'identity': identity, 'selections': {}}
            self._save()
        self.journal = self.directory / 'commits.jsonl'
        if self.journal.is_symlink():
            raise RecoveryError('selection checkpoint journal is a symlink')
        if self.journal.exists():
            # An interrupted append is uncommitted. The OS lock makes it safe
            # to discard only that final incomplete line before continuing.
            with self.journal.open('r+b') as stream:
                while True:
                    start = stream.tell()
                    line = stream.readline(4096)
                    if not line:
                        break
                    if not line.endswith(b'\n'):
                        stream.truncate(start)
                        break
                    record = json.loads(line)
                    key = record.pop('key')
                    if len(key) != 64 or any(char not in '0123456789abcdef' for char in key):
                        raise RecoveryError('invalid selection checkpoint journal key')
                    if (set(record) != {'sha256', 'bytes'} or type(record['bytes']) is not int or record['bytes'] < 0
                            or not isinstance(record['sha256'], str) or len(record['sha256']) != 64
                            or any(char not in '0123456789abcdef' for char in record['sha256'])):
                        raise RecoveryError('invalid selection checkpoint journal record')
                    self.record['selections'][key] = record

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.lock.close()

    def __del__(self):
        lock = getattr(self, 'lock', None)
        if lock is not None:
            lock.close()

    def _save(self):
        temporary = self.directory / 'selections.tmp'
        if temporary.is_symlink():
            raise RecoveryError('selection checkpoint manifest is a symlink')
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(self.record, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.manifest)

    def paths(self, selection):
        identity = [[part.start, part.stop] for part in selection]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        return key, self.directory / (key + '.values'), self.directory / (key + '.partial')

    def load(self, selection):
        key, completed, _ = self.paths(selection)
        record = self.record['selections'].get(key)
        if record is None:
            return None
        if (completed.is_symlink() or not completed.is_file()
                or completed.stat().st_size != record['bytes']
                or sha256_file(completed) != record['sha256']):
            raise RecoveryError('selection checkpoint values changed')
        return completed

    def temporary(self, selection):
        _, _, temporary = self.paths(selection)
        if temporary.is_symlink():
            raise RecoveryError('selection checkpoint temporary file is a symlink')
        return temporary

    def commit(self, selection):
        key, completed, temporary = self.paths(selection)
        if completed.is_symlink():
            raise RecoveryError('selection checkpoint values are a symlink')
        with temporary.open('r+b') as handle:
            handle.flush()
            os.fsync(handle.fileno())
        digest, size = sha256_file(temporary), temporary.stat().st_size
        os.replace(temporary, completed)
        record = {'sha256': digest, 'bytes': size}
        with self.journal.open('ab') as stream:
            stream.write((json.dumps({'key': key, **record}) + '\n').encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        self.record['selections'][key] = record


def write_frame(stream, record, payload=b''):
    header = json.dumps(record, separators=(',', ':')).encode('utf-8')
    stream.write(len(header).to_bytes(8, 'little'))
    stream.write(header)
    stream.write(payload)


def read_frames(stream, max_header_bytes, max_payload_bytes):
    while length := stream.read(8):
        if len(length) != 8:
            raise RecoveryError('truncated selection checkpoint frame')
        length = int.from_bytes(length, 'little')
        if not 0 < length <= max_header_bytes:
            raise RecoveryError('selection checkpoint header exceeds its byte budget')
        header = stream.read(length)
        if len(header) != length:
            raise RecoveryError('truncated selection checkpoint header')
        record = json.loads(header)
        size = record.get('payload_bytes', 0)
        if type(size) is not int or not 0 <= size <= max_payload_bytes:
            raise RecoveryError('selection checkpoint payload exceeds its byte budget')
        payload = stream.read(size)
        if len(payload) != size:
            raise RecoveryError('truncated selection checkpoint payload')
        yield record, payload
