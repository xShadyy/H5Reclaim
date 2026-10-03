"""Stream pinned virtual, external raw and external-link values without fill claims."""

from __future__ import annotations

from contextlib import ExitStack
import ctypes
import hashlib
import itertools
import json
from math import prod
import os
import re
from pathlib import Path
import tempfile
import time

import h5py
import numpy as np

from .format import FormatError
from .large_streaming import LargeBudget, _deadline, sparse_snapshot
from .logical_types import (contains_pointers, validate_type, encode_value, decode_value,
                            reference_addresses, token_bytes, write_value, type_label)
from .metadata import UnsupportedCase
from .native_bindings import public_function
from .native_stream import inspect_storage, logical_records
from .ownership_inventory import inventory_other_allocations
from .readable_export import _require_no_competing_owner
from .recovery import VERSION, RecoveryError, _validate_paths, _verify_source
from .output_annotations import metadata_group_for_path


def _product(axes, prefix=()):
    if not axes:
        yield prefix
    else:
        for item in axes[0]():
            yield from _product(axes[1:], prefix + (item,))


def selection_coordinates(space, extent, budget):
    """Enumerate selection order with bounded buffers, including irregular selections."""
    kind, rank = space.get_select_type(), len(extent)
    if kind == h5py.h5s.SEL_NONE:
        return
    if kind == h5py.h5s.SEL_ALL:
        yield from _product([lambda size=size: iter(range(size)) for size in extent])
        return
    if kind == h5py.h5s.SEL_POINTS:
        count = int(space.get_select_elem_npoints())
        function = public_function('H5Sget_select_elem_pointlist',
                                  [ctypes.c_int64, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p])
        for start in range(0, count, 1024):
            coordinates = np.empty((min(1024, count - start), rank), dtype='u8')
            if function(space.id, start, len(coordinates), coordinates.ctypes.data) < 0:
                raise FormatError('HDF5 could not enumerate the point selection')
            for coordinate in coordinates:
                point = tuple(int(value) for value in coordinate)
                if any(value >= size for value, size in zip(point, extent)):
                    raise FormatError('point selection is outside its extent')
                yield point
        return
    if kind != h5py.h5s.SEL_HYPERSLABS:
        raise UnsupportedCase('unrecognized HDF5 selection class')
    try:
        start, stride, count, block = space.get_regular_hyperslab()
    except (ValueError, RuntimeError):
        # HDF5 reports disjoint blocks. Merge their C-order iterators so the
        # point correspondence agrees with native selection iteration order.
        import heapq
        count = int(space.get_select_hyper_nblocks())
        if count * rank * 16 > budget.max_metadata_bytes:
            raise UnsupportedCase('irregular selection exceeds the metadata budget')
        blocks = space.get_select_hyper_blocklist()
        iterators = [_product([lambda a=int(a), b=min(int(b), size - 1): iter(range(a, b + 1))
                              for a, b, size in zip(begin, end, extent)]) for begin, end in blocks]
        yield from heapq.merge(*iterators)
        return
    axes = []
    for origin, step, repetitions, width, size in zip(start, stride, count, block, extent):
        if step <= 0 or width <= 0:
            raise FormatError('invalid regular hyperslab stride or block')
        repetitions = min(repetitions, max(0, (size - origin + step - 1) // step))
        def axis(origin=origin, step=step, repetitions=repetitions, width=width, size=size):
            if width >= step:
                yield from range(origin, min(size, origin + max(0, repetitions - 1) * step + width))
            else:
                for repeat in range(repetitions):
                    yield from range(origin + repeat * step, min(size, origin + repeat * step + width))
        axes.append(axis)
    yield from _product(axes)


def _local_object(handle, path):
    obj = handle['/']
    for part in path.strip('/').split('/') if path != '/' else []:
        if not isinstance(obj, h5py.Group) or not isinstance(obj.get(part, getlink=True), h5py.HardLink):
            raise UnsupportedCase('referenced object path must use local hard links')
        obj = obj[part]
    return obj


def _block_shape(shape, itemsize, block_bytes):
    remaining, widths = max(1, block_bytes // itemsize), []
    for size in reversed(shape):
        width = max(1, min(size, remaining))
        widths.append(width)
        remaining = max(1, remaining // width)
    return tuple(reversed(widths))


class Selection:
    """Map a coordinate to selection order without enumerating regular points."""
    def __init__(self, space, extent, budget):
        self.axes, self.points, self.positions = [], None, None
        kind = space.get_select_type()
        if kind == h5py.h5s.SEL_ALL:
            self.axes = [(0, 1, size, 1, size) for size in extent]
        elif kind == h5py.h5s.SEL_NONE:
            self.axes = [(0, 1, 0, 1, 0) for _ in extent]
        else:
            try:
                start, stride, count, block = space.get_regular_hyperslab()
            except (ValueError, RuntimeError):
                if int(space.get_select_npoints()) * max(1, len(extent)) * 32 > budget.max_metadata_bytes:
                    raise UnsupportedCase('irregular mapping exceeds its configured metadata budget')
                self.points = tuple(selection_coordinates(space, extent, budget))
                self.positions = {point: index for index, point in enumerate(self.points)}
                if len(self.positions) != len(self.points):
                    raise FormatError('mapping selection repeats a coordinate')
                self.count = len(self.points)
                return
            for origin, step, repeats, width, size in zip(start, stride, count, block, extent):
                repeats = min(repeats, max(0, (size - origin + step - 1) // step))
                if width >= step:
                    length = max(0, min(size, origin + max(0, repeats - 1) * step + width) - origin)
                    self.axes.append((origin, 1, length, 1, length))
                else:
                    length = max(0, (repeats - 1) * width + min(width, max(0, size - origin - max(0, repeats - 1) * step))) if repeats else 0
                    self.axes.append((origin, step, repeats, width, length))
        self.count = prod(axis[-1] for axis in self.axes)

    def index(self, coordinate):
        if self.positions is not None:
            return self.positions.get(coordinate)
        result = 0
        for value, (origin, step, repeats, width, length) in zip(coordinate, self.axes):
            delta = value - origin
            if delta < 0:
                return None
            repeat, within = divmod(delta, step)
            index = repeat * width + within
            if repeat >= repeats or within >= width or index >= length:
                return None
            result = result * length + index
        return result

    def point(self, index):
        if not 0 <= index < self.count:
            raise UnsupportedCase('mapped source selection is shorter than its virtual selection')
        if self.points is not None:
            return self.points[index]
        coordinate = []
        for origin, step, repeats, width, length in reversed(self.axes):
            index, within = divmod(index, length)
            repeat, element = divmod(within, width)
            coordinate.append(origin + repeat * step + element)
        return tuple(reversed(coordinate))


class Resolver:
    def __init__(self, source, manifest, budget, stack):
        from .dependency_routes import load_dependency_manifest
        records = load_dependency_manifest(manifest)['files'] if manifest else []
        self.entries = {record['declared_name']: record for record in records}
        self.budget, self.stack = budget, stack
        self.deadline = time.monotonic() + budget.max_seconds
        self.files, self.captures, self.storage, self.mappings = {}, [], {}, {}
        from collections import OrderedDict
        self.values, self.value_bytes, self.owners = OrderedDict(), 0, []
        self.cache_sizes = {}
        self.datasets = {}
        self.shapes = {}
        self.main = self._capture(Path(source), None)

    def _capture(self, path, pin):
        image, digest, identity, size, copied = self.stack.enter_context(sparse_snapshot(path, budget=self.budget))
        if pin is not None and digest != pin:
            raise RecoveryError('a related file differs from its pinned SHA-256')
        record = {'path': path, 'image': image, 'digest': digest, 'identity': identity,
                  'size': size, 'copied': copied}
        self.captures.append(record)
        return record

    def related(self, name):
        if name not in self.files:
            if name not in self.entries:
                raise UnsupportedCase('a declared related file was not supplied: ' + name)
            entry = self.entries[name]
            path = Path(entry['path'])
            if not path.is_file():
                raise UnsupportedCase('a supplied related file is unavailable: ' + name)
            self.files[name] = self._capture(path, entry['sha256'])
        return self.files[name]

    def opened(self, record):
        if 'handle' not in record:
            record['handle'] = self.stack.enter_context(h5py.File(record['image'], 'r'))
        return record['handle']

    def dataset_pattern_indices(self, record, path):
        parent, pattern = path.rsplit('/', 1)
        if '%b' in parent:
            raise UnsupportedCase('dataset block patterns in group names require explicit finite mappings')
        expression = re.compile('^' + re.escape(pattern).replace('%b', r'([0-9]+)') + '$')
        group = _local_object(self.opened(record), parent or '/')
        if not isinstance(group, h5py.Group):
            raise FormatError('dataset block pattern parent is not a group')
        indices = []
        for index, name in enumerate(group):
            if index >= self.budget.max_links:
                raise UnsupportedCase('dataset block pattern exceeds the link budget')
            match = expression.fullmatch(name)
            if match and isinstance(group.get(name, getlink=True), h5py.HardLink) and isinstance(group[name], h5py.Dataset):
                indices.append(int(match.group(1)))
        return indices

    def shape(self, record, dataset, active=()):
        key = (record['digest'], int(h5py.h5o.get_info(dataset.id).addr))
        if key in self.shapes:
            return self.shapes[key]
        shape = dataset.shape
        if not dataset.is_virtual or shape is None:
            self.shapes[key] = shape
            return shape
        if key in active:
            raise UnsupportedCase('cyclic virtual extent dependency')
        shape = list(shape)
        creation = dataset.id.get_create_plist()
        for index in range(creation.get_virtual_count()):
            virtual = creation.get_virtual_vspace(index)
            try:
                start, stride, count, block = virtual.get_regular_hyperslab()
            except (ValueError, RuntimeError):
                continue
            unlimited = [axis for axis, value in enumerate(count) if value == h5py.h5s.UNLIMITED]
            if not unlimited:
                continue
            if len(unlimited) != 1:
                raise UnsupportedCase('virtual mapping has more than one unlimited axis')
            axis = unlimited[0]
            name = creation.get_virtual_filename(index)
            name = name.decode() if isinstance(name, bytes) else name
            path = creation.get_virtual_dsetname(index)
            path = path.decode() if isinstance(path, bytes) else path
            if '%b' in name:
                pattern = re.compile('^' + re.escape(name).replace('%b', r'([0-9]+)') + '$')
                indices = [int(match.group(1)) for filename in self.entries if (match := pattern.fullmatch(filename))]
                if indices:
                    shape[axis] = max(shape[axis], start[axis] + max(indices) * stride[axis] + block[axis])
            elif '%b' in path:
                related = record if name == '.' else self.related(name)
                indices = self.dataset_pattern_indices(related, path)
                if indices:
                    shape[axis] = max(shape[axis], start[axis] + max(indices) * stride[axis] + block[axis])
            else:
                related = record if name == '.' else self.related(name)
                related, source = self.dataset(related, path if path.startswith('/') else '/' + path)
                source_shape = self.shape(related, source, active + (key,))
                points = Selection(creation.get_virtual_srcspace(index), source_shape, self.budget).count
                per_repeat = block[axis] * prod(count[a] * block[a] for a in range(len(shape)) if a != axis)
                repeats = (points + per_repeat - 1) // per_repeat
                if repeats:
                    shape[axis] = max(shape[axis], start[axis] + (repeats - 1) * stride[axis] + block[axis])
        self.shapes[key] = tuple(shape)
        return self.shapes[key]

    def dataset(self, record, path, seen=None):
        seen = set() if seen is None else seen
        identity = (record['digest'], path)
        if identity in seen or len(seen) > self.budget.max_objects:
            raise UnsupportedCase('cyclic or oversized external-link graph')
        seen.add(identity)
        if identity in self.datasets:
            return self.datasets[identity]
        obj = self.opened(record)['/']
        parts = path.strip('/').split('/')
        for index, part in enumerate(parts):
            if not isinstance(obj, h5py.Group):
                raise UnsupportedCase('referenced path crosses a non-group object')
            link = obj.get(part, getlink=True)
            if isinstance(link, h5py.ExternalLink):
                tail = '/'.join(parts[index + 1:])
                target = link.path.rstrip('/') + ('/' + tail if tail else '')
                return self.dataset(self.related(link.filename), target, seen)
            if not isinstance(link, h5py.HardLink):
                raise UnsupportedCase('referenced path requires a local hard link')
            obj = obj[part]
        if not isinstance(obj, h5py.Dataset):
            raise UnsupportedCase('referenced object is not a dataset')
        self.datasets[identity] = (record, obj)
        return record, obj

    def value(self, record, dataset, coordinate, expected_type, active=()):
        _deadline(self.deadline)
        typ = dataset.id.get_type()
        shape = self.shape(record, dataset)
        if not typ.equal(expected_type) or len(coordinate) != len(shape):
            raise FormatError('related source datatype or rank contradicts its mapping')
        if any(value < 0 or value >= size for value, size in zip(coordinate, shape)):
            raise UnsupportedCase('mapped source coordinate is outside its current extent')
        key = (record['digest'], int(h5py.h5o.get_info(dataset.id).addr))
        if key in active:
            raise UnsupportedCase('cyclic virtual dataset mapping')
        if dataset.is_virtual:
            creation = dataset.id.get_create_plist()
            if key not in self.mappings:
                self.mappings[key] = [Selection(creation.get_virtual_vspace(index), shape, self.budget)
                                      for index in range(creation.get_virtual_count())]
            chosen = None
            for index, virtual in enumerate(self.mappings[key]):
                offset = virtual.index(coordinate)
                if offset is None:
                    continue
                # Native HDF5 applies the last declared mapping to overlaps.
                # Missing bytes in that mapping remain unknown; an earlier
                # mapping must not silently replace the selected source.
                chosen = (index, offset)
            if chosen is not None:
                index, offset = chosen
                name = creation.get_virtual_filename(index)
                name = name.decode() if isinstance(name, bytes) else name
                path = creation.get_virtual_dsetname(index)
                path = path.decode() if isinstance(path, bytes) else path
                patterned = '%b' in name or '%b' in path
                if patterned:
                    start, stride, count, block = creation.get_virtual_vspace(index).get_regular_hyperslab()
                    axes = [axis for axis, value in enumerate(count) if value == h5py.h5s.UNLIMITED]
                    if len(axes) != 1:
                        raise UnsupportedCase('filename block pattern needs one unlimited mapping axis')
                    axis = axes[0]
                    number = (coordinate[axis] - start[axis]) // stride[axis]
                    name, path = name.replace('%b', str(number)), path.replace('%b', str(number))
                    per_file = Selection(creation.get_virtual_vspace(index), shape, self.budget)
                    per_file.axes[axis] = (start[axis], stride[axis], 1, block[axis], block[axis])
                    adjusted = list(coordinate)
                    adjusted[axis] -= number * stride[axis]
                    offset = per_file.index(tuple(adjusted))
                related = record if name == '.' else self.related(name)
                related, source = self.dataset(related, path if path.startswith('/') else '/' + path)
                source_space = creation.get_virtual_srcspace(index)
                selection_key = (key, index, related['digest'], int(h5py.h5o.get_info(source.id).addr))
                if selection_key not in self.mappings:
                    self.mappings[selection_key] = Selection(source_space, self.shape(related, source), self.budget)
                input_point = self.mappings[selection_key].point(offset)
                return self.value(related, source, input_point, expected_type, active + (key,))
            raise UnsupportedCase('virtual coordinate has no supplied source')
        creation = dataset.id.get_create_plist()
        if creation.get_external_count():
            if contains_pointers(typ):
                raise UnsupportedCase('external raw pointer records have no verified HDF5 heap context')
            offset = 0
            for value, size in zip(coordinate, dataset.shape):
                offset = offset * size + value
            offset *= typ.get_size()
            payload = bytearray()
            for index in range(creation.get_external_count()):
                name, start, length = creation.get_external(index)
                name = name.decode() if isinstance(name, bytes) else name
                if length != h5py.h5f.UNLIMITED and offset >= length:
                    offset -= length
                    continue
                source = self.related(name)
                count = typ.get_size() - len(payload)
                if length != h5py.h5f.UNLIMITED:
                    count = min(count, length - offset)
                physical = start + offset
                if physical < 0 or count > source['size'] - physical:
                    raise UnsupportedCase('external raw bytes are physically missing')
                with source['image'].open('rb') as stream:
                    stream.seek(physical)
                    block = stream.read(count)
                if len(block) != count:
                    raise RecoveryError('external raw range changed')
                payload.extend(block)
                offset = 0
                if len(payload) == typ.get_size():
                    return bytes(payload)
            raise UnsupportedCase('external storage does not supply a complete record')
        if key not in self.storage:
            records, ranges, filters = inspect_storage(dataset, record['size'], self.budget, self.deadline)
            inventory = inventory_other_allocations(record['image'], key[1],
                max_objects=self.budget.max_objects, max_links=self.budget.max_links,
                max_allocations=self.budget.max_chunks, max_seconds=self.budget.max_seconds,
                max_path_bytes=self.budget.max_metadata_bytes)
            _require_no_competing_owner(inventory, ranges)
            self.storage[key] = {tuple(origin) for origin, *_ in records}
            self.owners.append({'file_sha256': record['digest'], 'dataset': dataset.name,
                                'inventory': inventory.report()})
        if dataset.chunks:
            origin = tuple(value // width * width for value, width in zip(coordinate, dataset.chunks))
            if origin not in self.storage[key]:
                raise UnsupportedCase('source coordinate has no allocated chunk')
        elif not dataset.id.get_storage_size():
            raise UnsupportedCase('source dataset is unallocated')
        pointer_type = contains_pointers(typ)
        record_bytes = max(typ.get_size(), (self.budget.block_bytes + self.budget.logical_batch_records - 1)
                           // self.budget.logical_batch_records) if pointer_type else typ.get_size()
        widths = _block_shape(dataset.chunks or dataset.shape, record_bytes, self.budget.block_bytes)
        chunk_origin = tuple(value // width * width for value, width in zip(coordinate, dataset.chunks)) if dataset.chunks else tuple(0 for _ in coordinate)
        origin = tuple(base + (value - base) // width * width for value, base, width in zip(coordinate, chunk_origin, widths))
        cache_key = key + (origin,)
        if cache_key not in self.values:
            from .native_io import read_fixed_block
            selection = tuple(slice(value, min(value + width, size, base + chunk)) for value, width, size, base, chunk in
                              zip(origin, widths, dataset.shape, chunk_origin, dataset.chunks or dataset.shape))
            try:
                if pointer_type:
                    data, size = {}, 0
                    for point, value, error in logical_records(dataset, selection, self.budget):
                        relative = tuple(value - start for value, start in zip(point, origin))
                        if error is not None:
                            data[relative] = error
                            continue
                        try:
                            token = encode_value(value, typ, self.opened(record), max_bytes=self.budget.max_chunk_bytes,
                                                 max_depth=self.budget.max_type_depth)
                            size += len(token_bytes(token))
                            if size > self.budget.max_chunk_bytes:
                                raise UnsupportedCase('decoded logical batch exceeds its byte budget')
                            data[relative] = (token, record['digest'])
                        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                            data[relative] = exc
                else:
                    data = read_fixed_block(dataset, selection)
                    size = data.nbytes
            except (OSError, RuntimeError, ValueError) as exc:
                self.values[cache_key] = UnsupportedCase('native source read failed: ' + str(exc)[:240])
                raise self.values[cache_key]
            self.values[cache_key] = data
            self.cache_sizes[cache_key] = size
            self.value_bytes += size
            while self.value_bytes > 8 * self.budget.block_bytes and len(self.values) > 1:
                removed, _ = self.values.popitem(last=False)
                self.value_bytes -= self.cache_sizes.pop(removed, 0)
        data = self.values[cache_key]
        self.values.move_to_end(cache_key)
        if isinstance(data, Exception):
            raise data
        index = tuple(value - start for value, start in zip(coordinate, origin))
        value = data[index]
        if isinstance(value, Exception):
            raise value
        return value if pointer_type else value.tobytes()


def export_dependency_stream(source, dataset_path, manifest, output, report_path, *, budget=None,
                             published_output=None):
    source, output, report_path = Path(source), Path(output), Path(report_path)
    _validate_paths(source, output, report_path)
    budget = budget or LargeBudget()
    with ExitStack() as stack, tempfile.TemporaryDirectory(prefix='.h5reclaim-dependencies-', dir=output.parent) as directory:
        resolver = Resolver(source, manifest, budget, stack)
        record, selected = resolver.dataset(resolver.main, dataset_path)
        typ, shape = selected.id.get_type(), resolver.shape(record, selected)
        validate_type(typ, budget=budget)
        pointer_type, elements = contains_pointers(typ), 0 if shape is None else prod(shape)
        if elements > budget.max_grid or elements * typ.get_size() > budget.max_logical_bytes:
            raise UnsupportedCase('dependency extent exceeds its configured map or byte budget')
        staged, staged_report = Path(directory) / 'output.h5', Path(directory) / 'report.json'
        accepted, failures, value_hash, value_bytes, deferred_count = 0, [], hashlib.sha256(), 0, 0
        from .userblock import userblock_size, copy_userblock
        application_header_size = userblock_size(resolver.main['image'])
        with h5py.File(staged, 'x', track_order=True, userblock_size=application_header_size) as target:
            parent, name = dataset_path.rsplit('/', 1)
            group = target.require_group(parent or '/')
            creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            creation.set_attr_creation_order(h5py.h5p.CRT_ORDER_TRACKED | h5py.h5p.CRT_ORDER_INDEXED)
            if shape:
                creation.set_chunk(_block_shape(shape, typ.get_size(), budget.block_bytes))
            if not pointer_type:
                from .native_io import copy_fixed_fill
                copy_fixed_fill(selected.id.get_create_plist(), creation, typ)
            space = h5py.h5s.create(h5py.h5s.NULL) if shape is None else h5py.h5s.create_simple(
                shape, tuple(h5py.h5s.UNLIMITED if width is None else width for width in selected.maxshape))
            result = h5py.Dataset(h5py.h5d.create(group.id, name.encode(), typ.copy(), space, dcpl=creation))
            meta = target.require_group(metadata_group_for_path(dataset_path))
            status = meta.create_dataset('element_status', shape=shape or (), dtype='u1', fillvalue=2, chunks=True if shape else None) if shape is not None else None
            pending = meta.create_dataset('deferred_references', shape=(0,), maxshape=(None,), chunks=(128,), dtype=h5py.string_dtype())
            buffer_coordinates, buffer_values = [], []
            def flush():
                if not buffer_coordinates:
                    return
                data = np.frombuffer(b''.join(buffer_values), dtype=f'V{typ.get_size()}').copy()
                memory = h5py.h5s.create_simple((len(data),))
                space = result.id.get_space()
                if shape:
                    space.select_elements(np.asarray(buffer_coordinates, dtype='u8'))
                result.id.write(memory, space, data, mtype=typ)
                checked = np.empty(data.shape, dtype=data.dtype)
                result.id.read(memory, space, checked, mtype=typ)
                if checked.tobytes() != data.tobytes():
                    raise RecoveryError('dependency record readback differs')
                status_space = status.id.get_space()
                if shape:
                    status_space.select_elements(np.asarray(buffer_coordinates, dtype="u8"))
                status.id.write(memory, status_space, np.ones(len(buffer_coordinates), dtype="u1"))
                buffer_coordinates.clear()
                buffer_values.clear()
            for coordinate in _product([lambda size=size: iter(range(size)) for size in shape]) if shape is not None else ():
                _deadline(resolver.deadline)
                try:
                    payload = resolver.value(record, selected, coordinate, typ)
                except (FormatError, RecoveryError):
                    raise
                except (OSError, RuntimeError, ValueError, KeyError) as exc:
                    if len(failures) < 128:
                        failures.append({'coordinate': list(coordinate), 'reason': str(exc)[:300]})
                    continue
                if pointer_type:
                    token, source_digest = payload
                    encoded = token_bytes(token)
                    value_bytes += len(encoded)
                    if value_bytes > budget.max_logical_bytes:
                        raise UnsupportedCase('decoded dependency heap values exceed the logical byte budget')
                    if reference_addresses(token):
                        pending.resize((len(pending) + 1,))
                        pending[-1] = json.dumps({'index': list(coordinate), 'value': token,
                                                 'source_file_sha256': source_digest})
                        status[coordinate] = 4
                        deferred_count += 1
                        continue
                    value = decode_value(token, typ, target, {})
                    write_value(result, coordinate, value)
                    checked = encode_value(result[coordinate], typ, target, max_bytes=budget.max_chunk_bytes,
                                           max_depth=budget.max_type_depth)
                    if token_bytes(checked) != encoded:
                        raise RecoveryError('dependency logical record readback differs')
                    status[coordinate] = 1
                    value_hash.update(encoded)
                    accepted += 1
                    continue
                buffer_coordinates.append(coordinate)
                buffer_values.append(payload)
                value_hash.update(payload)
                accepted += 1
                if len(buffer_values) * typ.get_size() >= budget.block_bytes or len(buffer_values) >= 4096:
                    flush()
                    target.flush()
                    if staged.stat().st_size > budget.max_output_bytes:
                        raise UnsupportedCase('dependency output exceeds its byte budget')
            flush()
            from .whole_file import _attributes, _restore_attributes
            attrs, omitted = _attributes(selected, budget=budget)
            omitted += _restore_attributes(result, attrs)
            report = {'schema_version': 1, 'tool': 'h5reclaim', 'tool_version': VERSION,
                'mode': 'dependency_stream_export', 'metadata_group': meta.name,
                'outcome': 'complete' if accepted == elements else 'partial',
                'source': {'path': str(source), 'sha256_before': resolver.main['digest'], 'sha256_after': resolver.main['digest'],
                           'size_bytes': resolver.main['size']}, 'output_path': str(published_output or output),
                'dataset': {'path': dataset_path, 'shape': list(shape) if shape is not None else None, 'dtype': type_label(typ),
                            'maxshape': list(selected.maxshape) if selected.maxshape is not None else None,
                            'chunks': None, 'file_type_encoding_hex': typ.encode().hex(),
                            'attributes_copied': [item['name'] for item in attrs if item['name'] not in omitted],
                            'attributes_omitted': omitted},
                'accepted_elements': accepted, 'unknown_elements': elements - accepted,
                'validity_map': status.name if status is not None else None, 'element_status': status.name if status is not None else None,
                'deferred_references': pending.name if len(pending) else None,
                'deferred_reference_elements': deferred_count, 'decoded_value_bytes': value_bytes,
                'failed_reads': failures, 'native_value_sha256': value_hash.hexdigest(),
                'related_files': [{'path': str(item['path']), 'sha256': item['digest']} for item in resolver.captures[1:]],
                'ownership_inventories': resolver.owners,
                'output_filter_policy': 'materialized decoded records without source filters'}
            serialized = json.dumps(report, sort_keys=True, indent=2) + '\n'
            meta.create_dataset('report_json', data=serialized, dtype=h5py.string_dtype())
            target.flush()
            if staged.stat().st_size > budget.max_output_bytes:
                raise UnsupportedCase('dependency output exceeds its byte budget')
        copy_userblock(resolver.main['image'], staged, application_header_size, budget.block_bytes)
        staged_report.write_text(serialized, encoding='utf-8')
        for capture in resolver.captures:
            _verify_source(capture['path'], capture['identity'], capture['digest'])
        _validate_paths(source, output, report_path)
        os.link(staged_report, report_path)
        try:
            os.link(staged, output)
        except Exception:
            report_path.unlink(missing_ok=True)
            raise
        return report
