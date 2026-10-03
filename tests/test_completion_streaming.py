"""Large, growing, heap-backed and multi-file recovery compatibility checks."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.__main__ import main
from h5reclaim.bundle_stream import export_bundle_selected
from h5reclaim.dependency_stream import export_dependency_stream
from h5reclaim.large_streaming import LargeBudget
from h5reclaim.native_stream import export_native_stream
from h5reclaim.recovery import RecoveryError, sha256_file
from h5reclaim.related_manifest import build_related_manifest
from h5reclaim.rescue import auto_rescue
from h5reclaim.whole_file import rescue_all


class StreamingCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.output, self.report = (self.root / name for name in ('input.h5', 'output.h5', 'report.json'))
        self.manifest = self.root / 'related.json'

    def pin(self, files):
        self.manifest.write_text(json.dumps({'schema_version': 1, 'files': [
            {'declared_name': name, 'path': str(path), 'sha256': sha256_file(path)} for name, path in files]}))
        return self.manifest

    def test_large_virtual_dataset_streams_exact_values(self):
        leaf = self.root / 'leaf.h5'
        expected = np.arange(70000, dtype='i4')
        with h5py.File(leaf, 'w') as handle:
            handle.create_dataset('data', data=expected, chunks=(1024,))
        layout = h5py.VirtualLayout(expected.shape, expected.dtype)
        layout[:] = h5py.VirtualSource('leaf.h5', 'data', expected.shape)
        with h5py.File(self.source, 'w', libver='latest') as handle:
            handle.create_virtual_dataset('data', layout)
        report = export_dependency_stream(self.source, '/data', self.pin([('leaf.h5', leaf)]), self.output, self.report)
        self.assertEqual(report['accepted_elements'], len(expected))
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle['data'][:], expected)
            self.assertTrue(np.all(handle[report['validity_map']][:] == 1))

    def pattern_file(self, axis=0, missing=()):
        shape = (0,) if axis == 0 else (2, 0)
        maximum = (h5py.h5s.UNLIMITED,) if axis == 0 else (2, h5py.h5s.UNLIMITED)
        stride, count, block = ((4,), (h5py.h5s.UNLIMITED,), (4,)) if axis == 0 else ((1, 3), (2, h5py.h5s.UNLIMITED), (1, 3))
        part_shape = (4,) if axis == 0 else (2, 3)
        files = []
        for number in range(3):
            if number in missing:
                continue
            path = self.root / f'part-{number}.h5'
            with h5py.File(path, 'w') as handle:
                handle['data'] = np.arange(np.prod(part_shape), dtype='i4').reshape(part_shape) + number * 10
            files.append((path.name, path))
        with h5py.File(self.source, 'w', libver='latest') as handle:
            virtual = h5py.h5s.create_simple(shape, maximum)
            virtual.select_hyperslab(tuple(0 for _ in shape), count, stride=stride, block=block)
            source_space = h5py.h5s.create_simple(part_shape)
            creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            creation.set_virtual(virtual, b'part-%b.h5', b'/data', source_space)
            h5py.h5d.create(handle.id, b'data', h5py.h5t.STD_I32LE, h5py.h5s.create_simple(shape, maximum), dcpl=creation)
        return files, part_shape

    def test_numbered_growing_virtual_sources_and_directory_manifest(self):
        _, part_shape = self.pattern_file()
        built = build_related_manifest(self.source, self.root, self.manifest)
        self.assertTrue(built['complete'])
        self.assertEqual(len(built['manifest']['files']), 3)
        report = export_dependency_stream(self.source, '/data', self.manifest, self.output, self.report)
        self.assertEqual(report['outcome'], 'complete')
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle['data'][:], np.concatenate([np.arange(4) + number * 10 for number in range(3)]))
            self.assertEqual(handle['data'].maxshape, (None,))

    def test_growing_pattern_on_second_axis(self):
        files, shape = self.pattern_file(axis=1)
        report = export_dependency_stream(self.source, '/data', self.pin(files), self.output, self.report)
        self.assertEqual(report['outcome'], 'complete')
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle['data'][:], np.concatenate([
                np.arange(6).reshape(shape) + number * 10 for number in range(3)], axis=1))

    def test_missing_numbered_source_remains_unknown(self):
        files, _ = self.pattern_file(missing=(1,))
        report = export_dependency_stream(self.source, '/data', self.pin(files), self.output, self.report)
        self.assertEqual(report['accepted_elements'], 8)
        self.assertEqual(report['unknown_elements'], 4)
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle[report['validity_map']][:], [1] * 4 + [2] * 4 + [1] * 4)

    def test_transitive_virtual_graph_and_directory_cli(self):
        paths = []
        leaf = self.root / 'leaf.h5'
        with h5py.File(leaf, 'w') as handle:
            handle['data'] = np.arange(12, dtype='i4')
        prior = leaf.name
        for number in range(3):
            path = self.root / f'layer{number}.h5'
            layout = h5py.VirtualLayout((12,), 'i4')
            layout[:] = h5py.VirtualSource(prior, '/data', (12,))
            with h5py.File(path, 'w', libver='latest') as handle:
                handle.create_virtual_dataset('data', layout)
            prior = path.name
            paths.append(path)
        self.source = paths[-1]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(['rescue', str(self.source), '--related-dir', str(self.root),
                         '--output', str(self.output), '--report', str(self.report)])
        self.assertEqual(code, 0)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['outcome'], 'complete')
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle['data'][:], np.arange(12))

    def test_external_string_null_and_empty_datasets(self):
        leaf = self.root / 'leaf.h5'
        with h5py.File(leaf, 'w') as handle:
            handle.create_dataset('strings', data=['alpha', 'βeta', ''], dtype=h5py.string_dtype())
            handle.create_dataset('null', data=h5py.Empty('i4'))
            handle.create_dataset('empty', shape=(0,), dtype='i4')
        with h5py.File(self.source, 'w') as handle:
            for name in ('strings', 'null', 'empty'):
                handle[name] = h5py.ExternalLink('leaf.h5', '/' + name)
        self.pin([('leaf.h5', leaf)])
        for name in ('strings', 'null', 'empty'):
            with self.subTest(name=name):
                output, report_path = self.root / (name + '.h5'), self.root / (name + '.json')
                report = export_dependency_stream(self.source, '/' + name, self.manifest, output, report_path)
                self.assertEqual(report['outcome'], 'complete')
                with h5py.File(output, 'r') as handle:
                    if name == 'strings':
                        np.testing.assert_array_equal(handle[name].asstr()[:], ['alpha', 'βeta', ''])
                    else:
                        self.assertEqual(handle[name].shape, None if name == 'null' else (0,))

    def test_heap_batches_and_failed_batch_isolation(self):
        expected = [f'value-{index}' for index in range(1024)]
        with h5py.File(self.source, 'w') as handle:
            handle.create_dataset('data', data=expected, dtype=h5py.string_dtype(), chunks=(512,))
        original = h5py.Dataset.__getitem__
        def failed(dataset, selection, *args, **kwargs):
            if dataset.file.mode == 'r' and dataset.name == '/data':
                if selection == (slice(0, 256),) or selection == (17,):
                    raise OSError('controlled read failure')
            return original(dataset, selection, *args, **kwargs)
        with patch.object(h5py.Dataset, '__getitem__', failed):
            report = export_native_stream(self.source, '/data', self.output, self.report)
        self.assertEqual(report['accepted_elements'], 1023)
        self.assertEqual(report['logical_reads']['scalar_fallback_reads'], 256)
        self.assertEqual(report['logical_reads']['batches'], 3)
        with h5py.File(self.output, 'r') as handle:
            status = handle[report['validity_map']][:]
            self.assertEqual(status[17], 6)
            self.assertEqual(np.count_nonzero(status == 1), 1023)
            np.testing.assert_array_equal(handle['data'].asstr()[:17], expected[:17])

    def test_native_large_allocation_ledger_is_stored_in_hdf5(self):
        with h5py.File(self.source, 'w') as handle:
            handle.create_dataset('data', data=np.arange(1040, dtype='i4'), chunks=(2,))
        report = export_native_stream(self.source, '/data', self.output, self.report)
        self.assertEqual(report['source_chunk_records'], [])
        self.assertEqual(report['source_allocation_count'], 520)
        with h5py.File(self.output, 'r') as handle:
            ledger = handle[report['source_allocations']]
            self.assertEqual(len(ledger), 520)
            record = ledger[0]
            with self.source.open('rb') as stream:
                stream.seek(int(record['address']))
                digest = hashlib.sha256(stream.read(int(record['stored_bytes']))).hexdigest()
            self.assertEqual(record['raw_sha256'].decode(), digest)
            np.testing.assert_array_equal(handle['data'][:], np.arange(1040))

    def test_unique_root_pointer_checksum_correction(self):
        with h5py.File(self.source, 'w', libver='latest') as handle:
            handle['data'] = np.arange(16, dtype='i4')
        with self.source.open('r+b') as stream:
            stream.seek(36)
            byte = stream.read(1)
            stream.seek(36)
            stream.write(bytes([byte[0] ^ 1]))
        damaged = sha256_file(self.source)
        report = rescue_all(self.source, self.output, self.report)
        self.assertEqual(report['outcome'], 'complete')
        self.assertEqual(report['inventory']['inspection_view']['checksum_bytes_changed'], 0)
        selected = auto_rescue(self.source, '/data', self.root / 'selected.h5', self.root / 'selected.json')
        self.assertEqual(selected['accepted_elements'], 16)
        self.assertEqual(sha256_file(self.source), damaged)

    def test_split_checkpoint_pins_raw_member(self):
        stem = self.root / 'split'
        with h5py.File(stem, 'w', driver='split') as handle:
            handle['data'] = np.arange(8, dtype='i4')
        metadata, raw = Path(str(stem) + '-m.h5'), Path(str(stem) + '-r.h5')
        manifest = self.root / 'split.json'
        def pin():
            manifest.write_text(json.dumps({'schema_version': 1, 'driver': 'split', 'members': [
                {'role': 'metadata', 'path': str(metadata), 'sha256': sha256_file(metadata)},
                {'role': 'raw', 'path': str(raw), 'sha256': sha256_file(raw)}]}))
        pin()
        progress = self.root / 'progress'
        export_bundle_selected('split', manifest, '/data', self.output, self.report, resume_dir=progress)
        with raw.open('r+b') as stream:
            stream.write(np.array([90], dtype='i4').tobytes())
        pin()
        with self.assertRaisesRegex(RecoveryError, 'different source'):
            export_bundle_selected('split', manifest, '/data', self.root / 'again.h5', self.root / 'again.json', resume_dir=progress)
        self.assertFalse((self.root / 'again.h5').exists())

    def test_directory_builder_leaves_outside_files_unresolved(self):
        inside = self.root / 'companions'
        inside.mkdir()
        outside = self.root / 'outside.h5'
        with h5py.File(outside, 'w') as handle:
            handle['data'] = np.arange(3)
        with h5py.File(self.source, 'w') as handle:
            handle['data'] = h5py.ExternalLink(str(outside), '/data')
        built = build_related_manifest(self.source, inside, self.manifest)
        self.assertFalse(built['complete'])
        self.assertEqual(built['manifest']['files'], [])
        self.assertEqual(built['unresolved'][0]['declared_name'], str(outside))

    def test_overlapping_virtual_mappings_match_native_priority(self):
        files = []
        for name, value in [('first', 1), ('second', 2)]:
            path = self.root / (name + '.h5')
            with h5py.File(path, 'w') as handle:
                handle['data'] = np.full(8, value, dtype='i4')
            files.append((path.name, path))
        layout = h5py.VirtualLayout((8,), 'i4')
        layout[:] = h5py.VirtualSource('first.h5', '/data', (8,))
        layout[2:6] = h5py.VirtualSource('second.h5', '/data', (8,))[2:6]
        with h5py.File(self.source, 'w', libver='latest') as handle:
            handle.create_virtual_dataset('data', layout)
        with h5py.File(self.source, 'r') as handle:
            expected = handle['data'][:]
        report = export_dependency_stream(self.source, '/data', self.pin(files), self.output, self.report)
        self.assertEqual(report['outcome'], 'complete')
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle['data'][:], expected)

    def test_numbered_datasets_in_a_single_file(self):
        leaf = self.root / 'leaf.h5'
        with h5py.File(leaf, 'w') as handle:
            for index in range(3):
                handle[f'data-{index}'] = np.arange(4, dtype='i4') + index * 10
        with h5py.File(self.source, 'w', libver='latest') as handle:
            extent = h5py.h5s.create_simple((0,), (h5py.h5s.UNLIMITED,))
            virtual = extent.copy()
            virtual.select_hyperslab((0,), (h5py.h5s.UNLIMITED,), stride=(4,), block=(4,))
            creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            creation.set_virtual(virtual, b'leaf.h5', b'/data-%b', h5py.h5s.create_simple((4,)))
            h5py.h5d.create(handle.id, b'data', h5py.h5t.STD_I32LE, extent, dcpl=creation)
        report = export_dependency_stream(self.source, '/data', self.pin([('leaf.h5', leaf)]), self.output, self.report)
        self.assertEqual(report['accepted_elements'], 12)
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle['data'][:], np.concatenate([np.arange(4) + number * 10 for number in range(3)]))

    def test_string_virtual_values_and_wrong_pin_refusal(self):
        leaf = self.root / 'leaf.h5'
        with h5py.File(leaf, 'w') as handle:
            handle.create_dataset('data', data=['alpha', 'βeta'], dtype=h5py.string_dtype())
        layout = h5py.VirtualLayout((2,), h5py.string_dtype())
        layout[:] = h5py.VirtualSource('leaf.h5', '/data', (2,))
        with h5py.File(self.source, 'w', libver='latest') as handle:
            handle.create_virtual_dataset('data', layout)
        self.pin([('leaf.h5', leaf)])
        report = export_dependency_stream(self.source, '/data', self.manifest, self.output, self.report)
        self.assertEqual(report['outcome'], 'complete')
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle['data'].asstr()[:], ['alpha', 'βeta'])
        document = json.loads(self.manifest.read_text())
        document['files'][0]['sha256'] = '0' * 64
        self.manifest.write_text(json.dumps(document))
        with self.assertRaisesRegex(RecoveryError, 'pinned SHA-256'):
            export_dependency_stream(self.source, '/data', self.manifest, self.root / 'wrong.h5', self.root / 'wrong.json')
        self.assertFalse((self.root / 'wrong.h5').exists())

    def test_external_tree_checkpoint_and_file_scoped_references(self):
        files = []
        for number in range(2):
            leaf = self.root / f'leaf{number}.h5'
            with h5py.File(leaf, 'w') as handle:
                group = handle.create_group('tree')
                data = group.create_dataset('data', data=np.arange(4) + number * 100)
                group.create_dataset('reference', data=[data.ref], dtype=h5py.ref_dtype)
                group.attrs['label'] = f'group-{number}'
            files.append((leaf.name, leaf))
        with h5py.File(self.source, 'w') as handle:
            for number in range(2):
                handle[f'external{number}'] = h5py.ExternalLink(f'leaf{number}.h5', '/tree')
            layout = h5py.VirtualLayout((2,), h5py.ref_dtype)
            for number in range(2):
                layout[number:number + 1] = h5py.VirtualSource(f'leaf{number}.h5', '/tree/reference', (1,))
            handle.create_virtual_dataset('virtual_references', layout)
        self.pin(files)
        progress = self.root / 'progress'
        first = rescue_all(self.source, self.output, self.report, related_files=self.manifest, resume_dir=progress)
        self.assertEqual(first['outcome'], 'complete')
        second = rescue_all(self.source, self.root / 'second.h5', self.root / 'second.json', related_files=self.manifest, resume_dir=progress)
        self.assertEqual(second['checkpoint']['datasets_reused'], 5)
        with h5py.File(self.root / 'second.h5', 'r') as handle:
            for number in range(2):
                referent = handle[handle[f'external{number}/reference'][0]]
                self.assertEqual(referent.name, f'/external{number}/data')
                np.testing.assert_array_equal(referent[:], np.arange(4) + number * 100)
                self.assertEqual(handle[handle['virtual_references'][number]].name, referent.name)

    def test_whole_family_and_split_cli_preserve_heap_context(self):
        for kind in ('family', 'split'):
            with self.subTest(driver=kind):
                folder = self.root / kind
                folder.mkdir()
                stem = folder / 'input'
                arguments = {'driver': kind}
                name = str(stem)
                if kind == 'family':
                    name += '%03d.h5'
                    arguments['memb_size'] = 1024
                with h5py.File(name, 'w', **arguments) as handle:
                    handle.create_dataset('data', data=np.arange(24), chunks=(8,))
                    handle.create_dataset('strings', data=['alpha', 'beta'], dtype=h5py.string_dtype())
                    handle.attrs['label'] = kind
                manifest = folder / 'members.json'
                if kind == 'family':
                    members = sorted(folder.glob('input[0-9][0-9][0-9].h5'))
                    document = {'schema_version': 1, 'member_size': 1024, 'members': [
                        {'index': index, 'path': str(path), 'sha256': sha256_file(path)} for index, path in enumerate(members)]}
                    source = members[0]
                else:
                    source, raw = Path(str(stem) + '-m.h5'), Path(str(stem) + '-r.h5')
                    document = {'schema_version': 1, 'driver': 'split', 'members': [
                        {'role': role, 'path': str(path), 'sha256': sha256_file(path)} for role, path in [('metadata', source), ('raw', raw)]]}
                manifest.write_text(json.dumps(document))
                output, report = folder / 'out.h5', folder / 'out.json'
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = main(['rescue', str(source), '--' + kind + '-members', str(manifest),
                                 '--resume-dir', str(folder / 'progress'), '--output', str(output), '--report', str(report)])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(report.read_text())['outcome'], 'complete')
                with h5py.File(output, 'r') as handle:
                    np.testing.assert_array_equal(handle['data'][:], np.arange(24))
                    np.testing.assert_array_equal(handle['strings'].asstr()[:], ['alpha', 'beta'])
                    self.assertEqual(handle.attrs['label'], kind)

    def test_long_and_deep_legal_dataset_names(self):
        path = '/' + '/'.join(['group'] * 70) + '/data\n' + 'x' * 4200
        with h5py.File(self.source, 'w', libver='latest') as handle:
            handle.create_dataset(path, data=np.arange(4), chunks=(2,))
        report = auto_rescue(self.source, path, self.output, self.report)
        self.assertEqual(report['accepted_elements'], 4)
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle[path][:], np.arange(4))

    def test_non_numpy_integer_width_preserves_exact_file_records(self):
        payload = np.frombuffer(b''.join(value.to_bytes(16, 'little', signed=True)
            for value in [2**90 + 3, -2**89, 0, 42]), dtype='V16').copy()
        with h5py.File(self.source, 'w', libver='latest') as handle:
            typ = h5py.h5t.STD_I64LE.copy()
            typ.set_size(16)
            typ.set_precision(128)
            space = h5py.h5s.create_simple((4,))
            creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            creation.set_chunk((2,))
            selected = h5py.h5d.create(handle.id, b'data', typ, space, dcpl=creation)
            selected.write(space, space, payload, mtype=typ)
        report = auto_rescue(self.source, '/data', self.output, self.report)
        self.assertEqual(report['accepted_elements'], 4)
        from h5reclaim.native_io import read_fixed_block
        with h5py.File(self.output, 'r') as handle:
            self.assertEqual(handle['data'].id.get_type().get_precision(), 128)
            self.assertEqual(read_fixed_block(handle['data'], (slice(0, 4),)).tobytes(), payload.tobytes())

    def test_checkpoint_directories_reject_concurrent_recovery(self):
        from h5reclaim.checkpoint import Checkpoint
        progress = self.root / 'progress'
        with Checkpoint(progress, '0' * 64, {}):
            with self.assertRaisesRegex(RecoveryError, 'another recovery'):
                Checkpoint(progress, '0' * 64, {})
        with Checkpoint(progress, '0' * 64, {}):
            pass

    def test_heap_checkpoint_skips_completed_chunk_reads(self):
        expected = [f'value-{index}' for index in range(16)]
        with h5py.File(self.source, 'w') as handle:
            handle.create_dataset('data', data=expected, dtype=h5py.string_dtype(), chunks=(8,))
        progress = self.root / 'progress'
        from h5reclaim.logical_types import write_value
        def interrupted(dataset, index, value):
            if index == (8,):
                raise RecoveryError('controlled interrupted heap output')
            return write_value(dataset, index, value)
        with patch('h5reclaim.native_stream.write_value', side_effect=interrupted):
            with self.assertRaisesRegex(RecoveryError, 'interrupted heap'):
                export_native_stream(self.source, '/data', self.output, self.report, resume_dir=progress)
        from h5reclaim.native_stream import logical_records
        def checked(dataset, selection, *args):
            if selection[0].start < 8:
                raise AssertionError('completed heap source values were read again')
            return logical_records(dataset, selection, *args)
        with patch('h5reclaim.native_stream.logical_records', side_effect=checked):
            report = export_native_stream(self.source, '/data', self.output, self.report, resume_dir=progress)
        self.assertEqual(report['selection_checkpoint']['selections_reused'], 1)
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle['data'].asstr()[:], expected)


if __name__ == '__main__':
    unittest.main()
