"""Independent application-writer, recovery and application-reader evaluation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import h5py
import numpy as np

from h5reclaim.native_addresses import chunk_address
from h5reclaim.recovery import sha256_file


def make_case(application, original):
    expected = np.arange(12, dtype='f8')
    if application == 'matlab':
        import hdf5storage
        values = {'matrix': expected.reshape(3, 4), 'labels': np.array(['alpha', 'βeta'], dtype=object)}
        hdf5storage.writes(mdict=values, filename=original, options=hdf5storage.Options(
            store_python_metadata=False, matlab_compatible=True,
            compress_size_threshold=0, compressed_fletcher32_filter=True))
        return '/matrix', hdf5storage.loadmat(original)
    if application == 'netcdf':
        import netCDF4
        with netCDF4.Dataset(original, 'w', format='NETCDF4') as handle:
            handle.createDimension('time', None)
            time = handle.createVariable('time', 'f8', ('time',))
            time[:] = expected
            time.units = 'seconds since 2020-01-01 00:00:00'
            data = handle.createVariable('observations', 'f8', ('time',), zlib=True, fletcher32=True, chunksizes=(4,))
            data[:] = expected
            data.units = 'volts'
            handle.title = 'Application compatibility evaluation'
        return '/observations', expected
    if application == 'nwb':
        from hdmf.backends.hdf5.h5_utils import H5DataIO
        from pynwb import NWBFile, NWBHDF5IO, TimeSeries
        handle = NWBFile('Application compatibility evaluation', 'h5reclaim-evaluation', datetime(2020, 1, 1, tzinfo=timezone.utc))
        handle.add_acquisition(TimeSeries(name='observations', data=H5DataIO(expected, chunks=(4,), compression='gzip', fletcher32=True),
                                          unit='volts', starting_time=0.0, rate=1.0))
        with NWBHDF5IO(original, 'w') as writer:
            writer.write(handle)
        return '/acquisition/observations/data', expected
    raise ValueError('unknown application')


def read_case(application, output, expected, *, damaged):
    if application == 'matlab':
        import hdf5storage
        values = hdf5storage.loadmat(output)
        exact = all(np.array_equal(values[name], value) for name, value in expected.items())
    elif application == 'netcdf':
        import netCDF4
        with netCDF4.Dataset(output, 'r') as handle:
            observed = handle.variables['observations'][:]
            exact = np.array_equal(observed, expected)
            if handle.variables['observations'].units != 'volts' or handle.title != 'Application compatibility evaluation':
                raise ValueError('netCDF context changed')
    else:
        from pynwb import NWBHDF5IO
        with NWBHDF5IO(output, 'r') as reader:
            observed = reader.read().acquisition['observations']
            exact = np.array_equal(observed.data[:], expected)
            if observed.unit != 'volts' or observed.rate != 1.0:
                raise ValueError('NWB context changed')
    return {'reader_opened': True, 'intact_values_equal': exact if not damaged else None}


def score_values(original, output, report_path, dataset_path):
    """Score only accepted coordinates against truth never passed to recovery."""
    report = json.loads(report_path.read_text(encoding='utf-8'))
    selected = next(item['report'] for item in report['datasets'] if item['path'] == dataset_path)
    with h5py.File(original, 'r') as source, h5py.File(output, 'r') as recovered:
        old, new = source[dataset_path], recovered[dataset_path]
        if old.shape != new.shape or not old.id.get_type().equal(new.id.get_type()):
            raise ValueError('application dataset schema changed')
        old_values, new_values = old[:], new[:]
        status_path = selected.get('validity_map') or selected.get('validity', {}).get('dataset')
        status_path = status_path or selected['whole_file_metadata_group'] + '/chunk_status'
        status = recovered[status_path]
        if selected.get('element_status'):
            accepted = np.asarray(status[:] == 1)
        elif old.chunks:
            accepted = np.zeros(old.shape, dtype=bool)
            for unit in np.ndindex(status.shape):
                if status[unit] == 1:
                    selection = tuple(slice(index * width, min((index + 1) * width, size))
                                      for index, width, size in zip(unit, old.chunks, old.shape))
                    accepted[selection] = True
        else:
            accepted = np.full(old.shape, status[0] == 1)
        correct = int(np.count_nonzero(old_values[accepted] == new_values[accepted]))
        count = int(np.count_nonzero(accepted))
        if json.loads(recovered[report['metadata_group'] + '/report_json'][()]) != report:
            raise ValueError('embedded application report differs')
    return {'outcome': report['outcome'], 'datasets_exported': report['datasets_exported'],
            'accepted_elements': count, 'exact_accepted_elements': correct,
            'wrong_accepted_elements': count - correct, 'unknown_elements': old_values.size - count}


def run(applications=('matlab', 'netcdf', 'nwb'), *, directory=None):
    results = []
    with tempfile.TemporaryDirectory(prefix='h5reclaim-applications-', dir=directory) as work:
        root = Path(work)
        for application in applications:
            extension = {'matlab': '.mat', 'netcdf': '.nc', 'nwb': '.nwb'}[application]
            original = root / (application + '-original' + extension)
            dataset_path, expected = make_case(application, original)
            original_digest = sha256_file(original)
            with h5py.File(original, 'r') as handle:
                dataset = handle[dataset_path]
                if not dataset.fletcher32:
                    raise ValueError('application fixture has no payload checksum')
                info = dataset.id.get_chunk_info(0)
                physical, stored = chunk_address(dataset, info.byte_offset), int(info.size)
            cases = []
            for damaged in (False, True):
                label = 'damaged' if damaged else 'intact'
                source, output, report = (root / f'{application}-{label}{suffix}' for suffix in (extension, '-output' + extension, '.json'))
                shutil.copyfile(original, source)
                if damaged:
                    with source.open('r+b') as stream:
                        position = physical + stored // 2
                        stream.seek(position)
                        byte = stream.read(1)
                        stream.seek(position)
                        stream.write(bytes([byte[0] ^ 1]))
                before = sha256_file(source)
                process = subprocess.run([sys.executable, '-m', 'h5reclaim', 'rescue', str(source),
                    '--output', str(output), '--report', str(report)], capture_output=True, text=True, timeout=120)
                if process.returncode:
                    raise ValueError('application recovery failed: ' + process.stderr[:1200])
                result = score_values(original, output, report, dataset_path)
                result.update(read_case(application, output, expected, damaged=damaged))
                result.update({'case': label, 'source_unchanged': before == sha256_file(source),
                               'fault': 'one stored checksummed payload byte' if damaged else None})
                if damaged and result['unknown_elements'] == 0:
                    raise ValueError('controlled damaged payload was accepted as complete')
                cases.append(result)
            results.append({'application': application, 'dataset_path': dataset_path, 'cases': cases,
                            'original_unchanged': sha256_file(original) == original_digest})
    packages = {'matlab': 'hdf5storage', 'netcdf': 'netCDF4', 'nwb': 'pynwb'}
    return {'category': 'independent application writers and readers, intact and controlled checksum damage',
            'application_versions': {name: version(packages[name]) for name in applications},
            'applications': results,
            'passed': all(item['original_unchanged'] and all(case['source_unchanged'] and case['reader_opened']
                and case['wrong_accepted_elements'] == 0 and (case['case'] == 'damaged' or case['intact_values_equal'])
                for case in item['cases']) for item in results)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Application corpus: {'PASS' if result['passed'] else 'FAIL'}")
        for item in result['applications']:
            for case in item['cases']:
                print(f"{item['application']} {case['case']}: reader opened, {case['exact_accepted_elements']} exact, "
                      f"{case['wrong_accepted_elements']} wrong, {case['unknown_elements']} unknown")
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
