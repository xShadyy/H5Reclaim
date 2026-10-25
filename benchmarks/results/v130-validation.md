# H5Reclaim 1.3.0 validation

Validation date: 2026-10-26. Full local test runs used Linux and Python
3.12.14. This validation did not push to GitHub or publish a release.

| Check | Result |
| --- | --- |
| Full suite with current dependencies | 828 tests, passed, 18 skipped |
| Full suite with minimum core dependencies | 828 tests, passed, 14 skipped |
| Independent benchmark evaluator tests | 14 tests, passed |
| Compressed modern-index streaming panel | 10 of 10 declared outcomes, zero wrong accepted elements |
| Controlled automatic rescue panel | 104 useful of 109 damaged trials: 77 exact, 27 partial, 5 refused, zero wrong accepted elements |
| Bundled scientific-file damage catalog | 10 of 10 cases passed; two adjacent lost links yielded 14 exact and 114 unknown chunks |
| Seeded scientific-file matrix | 13 of 13 cases passed; 523 exact chunks, 113 newly accessible, zero false accepted |
| Official HDF5 copy/repack integration | 3 tests with actual installed h5copy and h5repack binaries passed |
| Translation structure | English plus 10 translated READMEs checked |
| Wheel and source distribution | Source parity and release artifact checks passed |
| Distribution metadata | `twine check --strict` passed |
| Fresh wheel installation | Version 1.3.0 and exact numeric, UTF-8, context, report and source checks passed |

The current environment used NumPy 2.5.4, h5py 3.16.0 and HDF5 2.0.0.
The minimum core environment used NumPy 1.26.0, h5py 3.10.0 and HDF5
1.14.2. Other Python versions and operating systems require the repository's
CI matrix. The HDF5 tool binaries used for the copy/repack tests were version
1.10.8. An actual `h5clear --image` no-image trial reported no changed bytes;
the cache-image probe has mocked mutation and refusal tests but no generated
damaged-cache-image fixture in this validation.

[`v130-streaming-recovery.json`](v130-streaming-recovery.json) scores accepted
positions against separately generated NumPy truth after the public recovery
process exits. Two cases damage links in distinct EA or B-tree parents, and
two damage two links in one parent. The sparse source larger than 4 GiB checks
address handling, not dense multi-gigabyte throughput.

[`v130-release-coverage.json`](v130-release-coverage.json) retains all 109
damaged trials and 23 intact controls. Its 104/109 useful result is unchanged
from 1.2.0. It cannot predict success for an arbitrary damaged scientific
file. The separately controlled
[`v130-damage-catalog.json`](v130-damage-catalog.json) and
[`v130-seeded-matrix.json`](v130-seeded-matrix.json) score output coordinates
against unchanged bundled scientific sources; these are fault injections,
not a collection of actual recovery incidents.

Focused tests cover same-parent EA and B-tree links, disjoint legacy tree
gaps, pointer-bearing native relocation with self and sibling references,
N-bit/scale-offset/SZIP filter decoding, paged free-space managers, checked
detached hard-link aliases and group cycles, and refusal on ambiguous or
uncheckable ownership. Scale-offset reproduces stored decoded values and
may be lossy relative to pre-write measurements. Missing historical bytes
without independent evidence remain unknown.

From a source checkout, the main repeatable checks are:

```sh
python -m unittest discover -s tests -q
python -m unittest benchmarks.test_authentic_v09_routes benchmarks.test_heldout_trials benchmarks.test_incident_intake benchmarks.test_release_coverage -q
python benchmarks/run_streaming_recovery.py
python benchmarks/run_release_coverage.py --work-dir work/release-coverage
python -m build
python tools/check_release_artifacts.py dist --tag v1.3.0
```
