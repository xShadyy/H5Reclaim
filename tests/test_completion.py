"""Compatibility checks for real omissions found after the initial broadening."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.native_stream import export_native_stream
from h5reclaim.object_discovery import discover
from h5reclaim.recovery import RecoveryError
from h5reclaim.rescue import auto_rescue
from h5reclaim.whole_file import rescue_all


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'input.h5'
        self.output = self.root / 'output.h5'
        self.report = self.root / 'report.json'

    def test_source_owned_metadata_names_survive(self):
        with h5py.File(self.source, 'w', libver='latest') as handle:
            handle.create_dataset('_h5reclaim/report_json', data=np.arange(5))
            handle.create_dataset('_h5reclaim_metadata_1/user', data=np.arange(3))
        report = rescue_all(self.source, self.output, self.report)
        self.assertEqual(report['outcome'], 'complete')
        self.assertEqual(report['metadata_group'], '/_h5reclaim_metadata_2')
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle['_h5reclaim/report_json'][:], np.arange(5))
            np.testing.assert_array_equal(handle['_h5reclaim_metadata_1/user'][:], np.arange(3))
            self.assertEqual(json.loads(handle[report['metadata_group'] + '/report_json'][()]), report)
        for item in report['datasets']:
            self.assertEqual(item['report']['dataset']['path'], item['path'])
            self.assertEqual(item['report']['metadata_group'], item['report']['whole_file_metadata_group'])

    def test_many_large_and_null_attributes_survive(self):
        with h5py.File(self.source, 'w', track_order=True) as handle:
            dataset = handle.create_dataset('data', data=np.arange(3), track_order=True)
            for index in range(300):
                dataset.attrs[f'a{index}'] = index
            dataset.attrs['vector'] = np.arange(16384, dtype='i8')
            dataset.attrs['empty'] = h5py.Empty('f8')
            handle.attrs['description'] = 'x' * 8192
        report = rescue_all(self.source, self.output, self.report)
        self.assertEqual(report['outcome'], 'complete')
        with h5py.File(self.output, 'r') as handle:
            for index in range(300):
                self.assertEqual(handle['data'].attrs[f'a{index}'], index)
            np.testing.assert_array_equal(handle['data'].attrs['vector'], np.arange(16384))
            self.assertIsInstance(handle['data'].attrs['empty'], h5py.Empty)
            self.assertEqual(handle.attrs['description'], 'x' * 8192)

    def test_committed_type_identity_and_aliases_survive(self):
        with h5py.File(self.source, 'w') as handle:
            for name in ('type_a', 'type_b'):
                handle[name] = np.dtype('i4')
                handle[name].attrs['meaning'] = name
                handle.create_dataset(name + '_data', data=np.arange(3, dtype='i4'), dtype=handle[name])
            handle['type_alias'] = handle['type_b']
        report = rescue_all(self.source, self.output, self.report)
        self.assertEqual(report['outcome'], 'complete')
        with h5py.File(self.output, 'r') as handle:
            for name in ('type_a', 'type_b'):
                self.assertEqual(handle[name].attrs['meaning'], name)
                self.assertTrue(handle[name + '_data'].id.get_type().committed())
                self.assertEqual(h5py.h5o.get_info(handle[name].id).addr,
                                 h5py.h5o.get_info(handle[name + '_data'].id.get_type()).addr)
            self.assertNotEqual(h5py.h5o.get_info(handle['type_a'].id).addr,
                                h5py.h5o.get_info(handle['type_b'].id).addr)
            self.assertEqual(handle['type_alias'].id, handle['type_b'].id)

    def test_legacy_root_damage_discovers_surviving_values(self):
        with h5py.File(self.source, 'w', libver='earliest') as handle:
            handle.create_dataset('data', data=np.arange(32, dtype='i4'), chunks=(8,))
            root = int(h5py.h5o.get_info(handle['/'].id).addr)
        original = self.source.read_bytes()
        with self.source.open('r+b') as stream:
            stream.seek(root)
            stream.write(b'BAD!')
        damaged = self.source.read_bytes()
        report = discover(self.source)
        self.assertEqual(len(report['datasets']), 1)
        self.assertIn('unchecksummed legacy', report['datasets'][0]['header_validation'])
        report = rescue_all(self.source, self.output, self.report)
        self.assertEqual(report['datasets_exported'], 1)
        self.assertEqual(report['outcome'], 'partial')
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle[report['datasets'][0]['path']][:], np.arange(32))
        self.assertEqual(self.source.read_bytes(), damaged)
        self.assertNotEqual(original, damaged)

    def test_selection_resume_skips_completed_source_reads(self):
        with h5py.File(self.source, 'w') as handle:
            handle.create_dataset('data', data=np.arange(24, dtype='i4'), chunks=(8,))
        progress = self.root / 'progress'
        from h5reclaim.native_io import write_fixed_block, read_fixed_block
        def interrupted(dataset, selection, block):
            if selection[0].start == 16:
                raise RecoveryError('simulated interrupted output')
            return write_fixed_block(dataset, selection, block)
        with patch('h5reclaim.native_stream.write_fixed_block', side_effect=interrupted):
            with self.assertRaisesRegex(RecoveryError, 'simulated interrupted'):
                export_native_stream(self.source, '/data', self.output, self.report, resume_dir=progress)
        self.assertFalse(self.output.exists())
        def checked(dataset, selection):
            if dataset.file.mode == 'r' and selection[0].start < 16:
                raise AssertionError('completed source values were read again')
            return read_fixed_block(dataset, selection)
        with patch('h5reclaim.native_stream.read_fixed_block', side_effect=checked):
            report = export_native_stream(self.source, '/data', self.output, self.report, resume_dir=progress)
        self.assertEqual(report['selection_checkpoint']['selections_reused'], 2)
        with h5py.File(self.output, 'r') as handle:
            np.testing.assert_array_equal(handle['data'][:], np.arange(24))

    def test_checkpoint_corruption_refuses_publication(self):
        with h5py.File(self.source, 'w') as handle:
            handle.create_dataset('data', data=np.arange(8), chunks=(4,))
        progress = self.root / 'progress'
        export_native_stream(self.source, '/data', self.output, self.report, resume_dir=progress)
        next(progress.glob('*.values')).write_bytes(b'changed')
        with self.assertRaisesRegex(RecoveryError, 'checkpoint values changed'):
            export_native_stream(self.source, '/data', self.root / 'again.h5', self.root / 'again.json', resume_dir=progress)
        self.assertFalse((self.root / 'again.h5').exists())

    def test_more_packaged_codecs_match_decoded_source_values(self):
        try:
            import hdf5plugin
        except ImportError:
            self.skipTest('optional codec package is not installed')
        codecs = {'bzip2': hdf5plugin.BZip2(), 'zfp': hdf5plugin.Zfp(accuracy=0.001),
                  'sz3': hdf5plugin.SZ3(absolute=0.001), 'sz': hdf5plugin.SZ(absolute=0.001),
                  'sperr': hdf5plugin.Sperr(absolute=0.001), 'blosc2': hdf5plugin.Blosc2(),
                  'htj2k': hdf5plugin.Htj2k(), 'fci': hdf5plugin.FciDecomp()}
        for name, codec in codecs.items():
            with self.subTest(codec=name):
                source = self.root / (name + '.h5')
                output = self.root / (name + '-output.h5')
                values = np.linspace(0, 1, 256).reshape(16, 16)
                if name in ('fci', 'htj2k'):
                    values = np.arange(256, dtype='u2').reshape((1, 16, 16) if name == 'fci' else (16, 16))
                with h5py.File(source, 'w', libver='latest') as handle:
                    handle.create_dataset('data', data=values, chunks=values.shape, **codec)
                with h5py.File(source, 'r') as handle:
                    expected = handle['data'][:]
                report = auto_rescue(source, '/data', output, self.root / (name + '.json'))
                self.assertEqual(report['outcome'], 'complete')
                with h5py.File(output, 'r') as handle:
                    np.testing.assert_array_equal(handle['data'][:], expected)


if __name__ == '__main__':
    unittest.main()
