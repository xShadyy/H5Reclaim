# Recovery benchmarks

H5Reclaim's benchmarks measure recovered values, exact coordinates, file structure, and source preservation against independently retained originals.

The [0.16.0 controlled benchmark](../docs/coverage.md) achieved **90.8% useful recovery across 109 damaged-file trials**: **72 fully exact**, **27 partial**, and **10 refused**. Across 23 generated data and layout families, the panel recorded zero wrong accepted elements and zero changed sources. The full evaluation also includes 23 intact-file trials, for 132 cases in total.

## Regression checks

```sh
python -m unittest discover -s tests -q
python -m unittest benchmarks.test_authentic_v09_routes benchmarks.test_heldout_trials benchmarks.test_incident_intake -q
python -m unittest benchmarks.test_release_coverage -q
```

The recovery suite includes structural parsers, codecs, sparse allocation, tail cuts, metadata trials, driver bundles, prior captures, parity, misleading ownership, source preservation, publication failures, and public CLI routes. `tests/test_broader_recovery.py` covers LZF, multidimensional fixed records, variable strings, ragged arrays, and whole-file context restoration.

`tests/test_universality.py` covers automatic decoder fallback, partial native chunks, empty/null dataspaces, compound variable fields, cross-object and region references, reference attributes, packaged lossless codecs, detached-header discovery, shared snapshots, configured budgets, pinned whole-file dependencies and completed-dataset checkpoint reuse. CI checks Linux, Windows and macOS on Python 3.10, 3.12 and 3.14, plus the declared minimum h5py and NumPy versions.

Platform regressions in `tests/test_platform_io.py`, `test_sparse_io.py` and `test_posix_worker_limits.py` check packaged HDF5 exports, native handle cleanup, allocated-range pagination, sparse captures and worker memory/timeout cleanup. Status-flag fixtures modify a closed writer's superblock and checksum so they run with Windows file locking enabled.

`tests/test_completion.py` and `tests/test_completion_streaming.py` check healthy namespace collisions, 300 attributes, large/null attributes, committed type identity, legacy root damage, additional codecs, interrupted selection caches, 70,000-value virtual mappings, growing numbered sources, overlapping mappings, long paths, 128-bit file integers, whole Family/Split exports and file-scoped references. The application CI job installs independent application writers and readers.

## Scientific-file evaluations

Run from the repository root after installation:

| Command | Evaluation |
| --- | --- |
| `python benchmarks/run_release_coverage.py --seed 20261005 --json` | Score public automatic rescue across 23 generated families and declared faults, retaining refusals and independently checking accepted coordinates |
| `python benchmarks/run_real_corpus.py` | Verify four original hashes and compare structural survey classifications |
| `python benchmarks/run_real_candidate_exports.py` | Compare structural candidate exports with exact original values and extents |
| `python benchmarks/run_real_readable_corpus.py` | Compare native-readable numeric and compound representative datasets |
| `python benchmarks/run_whole_file_corpus.py` | Independently compare every dataset, exact datatype and attribute across four original scientific files |
| `python benchmarks/run_application_corpus.py` | Write MATLAB 7.3, netCDF4 and NWB files independently, recover intact and checksum-damaged copies, then reopen them with their own Python readers |
| `python benchmarks/run_gwosc_recovery.py` | Damage a verified GWOSC index pointer and score recovered chunks at exact coordinates |
| `python benchmarks/run_damage_catalog.py` | Apply declared controlled faults to scientific-file copies |
| `python benchmarks/run_seeded_matrix.py --seed 20260927 --trials 2` | Score varied seeded mutations and all resulting outcomes |
| `python benchmarks/run_stratified_layouts.py --seed 11235813 --trials 1` | Exercise declared storage-layout strata |
| `python benchmarks/run_authentic_baseline_integrity.py` | Evaluate prior-baseline comparison on authentic bytes |
| `python benchmarks/run_authentic_v09_routes.py` | Evaluate capsule, parity, and tail routes on controlled authentic-file copies |

These programs expose readable results and JSON where their `--help` indicates it. Each report records its evaluation category and case counts. The structural survey describes parser coverage; automatic rescue additionally uses native, variable, and streaming methods.

Install `python -m pip install -e ".[filters,applications]"` for the application evaluation. It uses independent application writers and readers, then compares accepted coordinates against retained originals after recovery.

## Independent panels and incidents

`run_heldout_trials.py` evaluates a separately supplied hash-pinned panel with declared fault classes. Its report includes eligible and excluded cases, exact and wrong acceptance, unknown values, and refusals. See its `--help` for manifest and work-directory arguments.

[Incident intake](INCIDENT_INTAKE.md) evaluates submitted damaged files. Recovery runs first on a source copy. A separate score command compares outputs with a matching earlier file or previously captured element hashes.

Reports retain trial denominators, recovered and unknown coordinates, context restoration, and refusals. The [release coverage report](../docs/coverage.md) gives the full breakdown and reproducible commands for the 0.16.0 benchmark. The previous [0.15.0 panel](results/v015-release-coverage.json) remains available.

## Development fixtures

```sh
python tools/make_healthy_fixture.py --help
python tools/make_broken_link_fixture.py --help
python benchmarks/run_recovery.py --help
```

The fixture evaluator keeps the pristine dataset and mutation description for post-run scoring. It invokes recovery in a separate process and checks exact values, chunk placement, reader behavior, and unchanged source hashes.
