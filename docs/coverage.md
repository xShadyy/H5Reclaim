# Release coverage and limits

H5Reclaim 0.15.0 supports varied HDF5 storage layouts and logical datatypes. The automatic rescue command produced useful output in **88 of 109 controlled damaged-file trials (80.7%)** in the fixed release panel below. Of those 109 trials, **63 were fully exact**, **25 were partial**, and **21 were refused**. This is a result for a declared generated panel, not an estimate of the proportion of naturally damaged files recoverable in the field.

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

Metadata pointer and signature trials retain the original stored checksum. The interrupted-write fixture changes a flag on a verified, cleanly closed source and recomputes the checksum; it does not simulate or justify accessing a live writer. The legacy file has no modern checksum or interrupted-write flag, so those two modern fault classes are excluded explicitly. Payload trials require an allocated chunk with a Fletcher32 checksum. A tail cut can remove later index metadata as well as data bytes.

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

The default command is run in a separate process with only the damaged input and output destinations. The pristine source is retained only for post-run scoring. The oracle uses h5py to compare exact file datatypes, logical values, attributes and reference targets. Region references are compared by their extents and selected coordinates, rather than by serialization bytes that can differ between equivalent HDF5 versions. No prior capture or pristine reference is supplied to recovery. Any claimed prior-capture equality would fail the evaluator.

## Reproduce and extend

From an installed checkout:

```sh
python -m pip install -e ".[filters]"
python benchmarks/run_release_coverage.py --seed 20261005 --work-dir release-panel --json
python -m unittest benchmarks.test_release_coverage -q
```

The work directory must be new or empty. It retains generated originals, damaged inputs, outputs, reports and `coverage.json`. A deterministic seed controls values and mutation sites; file hashes can vary with writer metadata and HDF5 version. `--families` selects a smaller smoke panel, and its denominator is recorded explicitly.

Adversarial scorer checks confirm that one changed accepted value is counted as wrong, an unknown mask cannot claim completeness, historical equality cannot be invented without a capture, an unrelated attribute omission cannot conceal a missing attribute, and a refusal cannot disappear from the coordinate denominator.

The [benchmark guide](../benchmarks/README.md) also provides independently compared authentic scientific files, controlled GWOSC index damage and application writer/reader evaluations for MATLAB 7.3, netCDF4 and NWB. Existing `v014-*` reports are historical 0.14.0 results; their intact-file and selected-damage denominators must not be added to this panel to manufacture a recovery percentage.

## What this panel does not establish

These fixtures and faults are chosen, generated and correlated. Several mutations reuse the same original, and their relative counts do not represent the frequency of actual failures. There are no naturally damaged user submissions or representative field-fault weights in this panel. The score does not predict an 80% success rate on a new audience's files.

Unchecksummed payload changes can remain structurally readable while their historical measurement values are wrong. Completely overwritten payloads, missing external dependencies, destroyed ownership evidence, broader metadata destruction and losses beyond configured resource budgets can remain unrecoverable. Evaluator-held truth is separated from recovery by program inputs, not by operating-system access permissions. A representative, separately supplied incident panel evaluated through [incident intake](../benchmarks/INCIDENT_INTAKE.md) is required to estimate field recovery.
