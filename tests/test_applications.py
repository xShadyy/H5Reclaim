"""Application files are generated and reopened by their independent libraries."""

import importlib.util
import unittest

from benchmarks.run_application_corpus import run


class ApplicationTests(unittest.TestCase):
    def check(self, application, module):
        if importlib.util.find_spec(module) is None:
            self.skipTest('install h5reclaim[applications] for this application reader')
        result = run((application,))
        self.assertTrue(result['passed'], result)
        intact, damaged = result['applications'][0]['cases']
        self.assertEqual(intact['accepted_elements'], 12)
        self.assertTrue(intact['intact_values_equal'])
        self.assertGreater(damaged['unknown_elements'], 0)
        self.assertEqual(damaged['wrong_accepted_elements'], 0)

    def test_matlab_writer_and_reader(self):
        self.check('matlab', 'hdf5storage')

    def test_netcdf_writer_and_reader(self):
        self.check('netcdf', 'netCDF4')

    def test_nwb_writer_and_reader(self):
        self.check('nwb', 'pynwb')


if __name__ == '__main__':
    unittest.main()
