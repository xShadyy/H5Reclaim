# H5Reclaim 1.1.0 local validation

Validation date: 2026-10-25. Tested implementation: `edca36d`; the subsequent
packaging commit adds documentation and benchmark evidence only.

| Check | Result |
| --- | --- |
| Full suite, current dependencies | 758 tests, passed, 7 skipped |
| Full suite, minimum core dependency versions | 758 tests, passed, 14 skipped |
| Benchmark evaluator/scoring tests | 14 tests, passed |
| Controlled compressed streaming benchmark | 6 of 6 expected outcomes, zero wrong accepted elements |
| Official HDF5 integration tests | Selected copy, gzip repack and source-mutation refusal passed |
| HTML report DOM checks | Search filtering and embedded JSON evidence passed with jsdom |
| Wheel and source archive validation | Passed, matching packaged Python sources |
| Distribution metadata checks | `twine check --strict` passed |
| Clean virtual environment wheel installation | Numeric values, UTF-8 values, context, report and source preservation passed |
| README translation structure | All 10 translations passed the repository checker |

Both full-suite runs used Linux and Python 3.12.14. Current dependencies were
NumPy 2.5.4, h5py 3.16.0 and HDF5 2.0.0, with the optional readers, codecs,
scientific application libraries and official HDF5 tools available. The
minimum core dependency run used NumPy 1.26.0, h5py 3.10.0 and HDF5 1.14.2.
This is not a claim that every supported Python version or operating system
was tested locally. The CI matrix provides the additional checks on push.

The streaming benchmark results are in
[`v110-streaming-recovery.json`](v110-streaming-recovery.json). It covers
compressed fixed-array, extensible-array and v2 B-tree links; multidimensional
data; a 4 MiB decoded chunk; LZF; partial payload loss; and an address above
4 GiB in a sparse source. The sparse case has small physically allocated
data and does not establish dense-file throughput. Separate tests cover
10,000-slot extensible-array and B-tree grids.

These are controlled faults scored against separately retained truth. They
do not prove universal recovery, performance on dense large acquisitions or
scientific validity. See `USAGE.md` and `h5reclaim capabilities` for the
implemented bounds. Browser rendering was not visually validated; HTML
escaping and DOM behavior were checked programmatically.

Reproduce the principal checks from the project root:

```sh
python -m pip install -e ".[filters,readers,applications]"
python -m unittest discover -s tests -q
python benchmarks/run_streaming_recovery.py
python -m pip install build twine
python -m build
python tools/check_release_artifacts.py dist --tag v1.1.0
python -m twine check --strict dist/h5reclaim-1.1.0-py3-none-any.whl dist/h5reclaim-1.1.0.tar.gz
```

Official-tool checks need an installed `h5copy` and `h5repack` in `PATH`;
optional library checks require those extras.
