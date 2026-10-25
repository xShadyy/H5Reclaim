# H5Reclaim 1.2.0 validation

Validation date: 2026-10-25. Full local test runs used Linux and Python
3.12.14. No GitHub push or public release was part of this validation.

| Check | Result |
| --- | --- |
| Full suite with current dependencies | 790 tests, passed, 9 skipped |
| Full suite with minimum core dependency versions | 790 tests, passed, 14 skipped |
| Independent benchmark evaluator tests | 14 tests, passed |
| Compressed streaming benchmark | 8 of 8 declared outcomes, zero wrong accepted elements |
| Controlled release panel | 104 useful of 109 damaged trials: 77 fully exact, 27 partial, 5 refused, zero wrong accepted elements |
| Official HDF5 tool integration | 3 tests using installed h5copy/h5repack passed |
| Translation structure | English plus 10 translated READMEs checked |
| Source archive and wheel | Source parity and artifact checks passed |
| Distribution metadata | `twine check --strict` passed |
| Clean wheel installation | Version 1.2.0 and exact numeric, UTF-8, context and report checks passed |

The current environment used NumPy 2.5.4, h5py 3.16.0 and HDF5 2.0.0,
with optional readers, codecs and scientific application libraries available.
The minimum core run used NumPy 1.26.0, h5py 3.10.0 and HDF5 1.14.2.
Other Python versions and operating systems require the repository's CI
matrix. The official-tool tests ran separately with installed HDF5 binaries.

[`v120-streaming-recovery.json`](v120-streaming-recovery.json) scores
accepted positions against independently retained NumPy truth after the
public recovery process exits. Two cases damage links in distinct EA or
B-tree metadata parents; the other six cases cover previously supported
compressed layouts, partial payload loss and a sparse address beyond 4 GiB.
Additional tests recover 8,100 chunks after two B-tree links fail, a sparse
fixed-array grid with over one million slots, rank 32 compound/string records,
and wider fixed compound and array records.

[`v120-release-coverage.json`](v120-release-coverage.json) contains the
full 109 damaged-file controlled panel and its intact controls. The damaged
panel's 104/109 useful result is unchanged from the earlier release panel.
It cannot predict success for an arbitrary damaged scientific file. The
4 GiB case is a sparse address test, not dense multi-gigabyte throughput.

Metadata fault tests include two distinct checksum-restoring index links,
corrupt persistent FSHD/FSSE metadata, a lost compact hard link to a checked
detached dataset, and a group whose surviving internal links can be rebuilt
under a synthetic path. Prospective snapshots restore only previously
captured bytes while the rest of the source matches the earlier digest.
Tests cover refusal on ambiguous links, changed uncaptured payloads and
unsupported metadata variants. No test proves missing historical bytes can
be inferred without independent evidence.

For the main checks from a source checkout:

```sh
python -m pip install -e ".[filters,readers,applications]"
python -m unittest discover -s tests -q
python benchmarks/run_streaming_recovery.py
python benchmarks/run_release_coverage.py --work-dir work/release-coverage
python -m build
python tools/check_release_artifacts.py dist --tag v1.2.0
```
