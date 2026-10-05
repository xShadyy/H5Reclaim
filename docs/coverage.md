# Recovery coverage

H5Reclaim 0.15.0 achieved **80.7% useful recovery in its controlled damaged-file benchmark**. The automatic rescue command produced **88 useful outputs from 109 trials** across varied HDF5 storage layouts and logical datatypes: **63 fully exact recoveries**, **25 partial recoveries**, and **21 refusals**.

The [machine-readable report](../benchmarks/results/v015-release-coverage.json) retains every case, fault location, source identity, refusal, unknown coordinate count, environment and tested source fingerprint. The panel ran on Linux with Python 3.12.14, h5py 3.16.0, HDF5 2.0.0 and NumPy 2.3.5. The tested source fingerprint remained unchanged throughout the evaluation.

## Measured outcomes

| Category | Cases | Useful output | Fully exact | Useful partial | Refused |
| --- | ---: | ---: | ---: | ---: | ---: |
| Intact generated files | 23 | 23 | 22 | 1 | 0 |
| Controlled damaged files | 109 | 88 | 63 | 25 | 21 |
| Entire generated panel | 132 | 111 | 85 | 26 | 21 |

For damaged files, 3,324 of 4,817 logical dataset elements were independently compared and accepted at their correct coordinates, giving **69.0% exact accepted elements**. The other 1,493 elements remained unknown, including all elements of refused cases. Across the entire panel, 4,203 of 5,758 elements were exact accepted values and 1,555 remained unknown. There were **zero wrong accepted elements**, zero changed source files and zero invalid or unscorable cases.

“Useful output” means at least one accepted element exactly matches the independent original at its coordinate, with no wrong accepted elements or schema discrepancies. A correctly preserved empty or null dataset also qualifies. A partial output may omit attributes only when that omission is explicitly reported. “Fully exact” requires every logical element, exact HDF5 datatype, shape, maximum shape and original attribute to match, with no unknown elements or context omissions. A refused case contributes zero useful recoveries and retains all its elements in the denominator.

One intact sparse fixture deliberately leaves 62 of 77 positions unknown. HDF5 displays a configured fill value at those unallocated positions, but H5Reclaim does not promote fill values to recovered measurements. The evaluator independently checks that all and only the physically allocated chunk coordinates are accepted. That case remains partial in the counts above. Ten tail-truncated exports explicitly omit unavailable attribute context.

## Declared fault classes

| Controlled fault | Cases | Useful output | Fully exact | Partial | Refused |
| --- | ---: | ---: | ---: | ---: | ---: |
| One damaged HDF5 signature byte | 23 | 22 | 21 | 1 | 1 |
| One damaged modern superblock root-pointer byte | 22 | 22 | 21 | 1 | 0 |
| Interrupted-write status flag on a closed-writer copy | 22 | 22 | 21 | 1 | 0 |
| One damaged checksummed payload byte | 12 | 12 | 0 | 12 | 0 |
| Tail cut inside the last stored checksummed payload | 12 | 10 | 0 | 10 | 2 |
| One damaged modern chunk-dimension byte | 15 | 0 | 0 | 0 | 15 |
| One damaged stored superblock-checksum byte | 3 | 0 | 0 | 0 | 3 |

Metadata pointer and signature trials retain the original stored checksum. The interrupted-write fixture changes a flag on a verified, cleanly closed source and recomputes the checksum. Modern root-pointer and interrupted-write faults apply to modern files; payload trials use allocated chunks with Fletcher32 checksums. Tail cuts can remove later index metadata as well as data bytes.

The default whole-file command could not discover the selected objects with chunk-dimension damage in these 15 trials. Supplying a known dataset path can make additional bounded repair routes available, but those routes were not credited to this automatic whole-file panel. The stored-checksum mutations lose the original checksum needed to justify a metadata correction. The remaining signature refusal is the legacy file. The two tail refusals are the version-2 B-tree fixture and the rank-five fixture.

## Data and layout families

The fixed panel contains 23 generated families:

| Area | Exercised families |
| --- | --- |
| Storage and indexing | Contiguous, compact, fixed array, extensible array, version-2 B-tree, legacy chunked, sparse chunk allocation and edge chunks |
| Numeric representations | Signed integers, big-endian integers, complex numbers, rank-five arrays, scalar datasets and enumeration datatypes |
| Structured and variable data | Fixed compound records, compound records with variable strings, fixed strings, UTF-8 variable strings and ragged numeric arrays |
| Other logical types | Object references, multidimensional region references, empty datasets and null dataspaces |
| Filters | Gzip, Fletcher32, LZF, shuffle and integer scale-offset |

The default command runs in a separate process with the damaged input and output destinations. After recovery, the evaluator uses the separately retained original to compare exact HDF5 datatypes, logical values, attributes, and reference targets. Region references are compared by their extents and selected coordinates. This measures automatic recovery using the damaged file alone.

## Reproduce and extend

From an installed checkout:

```sh
python -m pip install -e ".[filters]"
python benchmarks/run_release_coverage.py --seed 20261005 --work-dir release-panel --json
python -m unittest benchmarks.test_release_coverage -q
```

The work directory must be new or empty. It retains generated originals, damaged inputs, outputs, reports and `coverage.json`. A deterministic seed controls values and mutation sites; file hashes can vary with writer metadata and HDF5 version. `--families` selects a smaller smoke panel, and its denominator is recorded explicitly.

Scorer regressions verify accepted-value accuracy, validity maps, prior-capture comparisons, attribute coverage, and complete trial denominators.

The [benchmark guide](../benchmarks/README.md) provides additional scientific-file comparisons, controlled GWOSC index recovery, and independent application writer/reader evaluations for MATLAB 7.3, netCDF4, and NWB. Historical `v014-*` reports retain their own version and case counts.

To evaluate more datasets, use a separately supplied panel with `run_heldout_trials.py` or the [incident intake workflow](../benchmarks/INCIDENT_INTAKE.md). Both record each case and compare recovered values with independently retained references.
