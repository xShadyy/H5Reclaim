# Current status

Updated: 2026-09-27

## Implemented

The repository contains an installable `h5reclaim` CLI with `inspect` and `recover`, a bounded read-only parser for a declared HDF5 v1 B-tree case, a deterministic healthy fixture, a verified broken-pointer copy tool, an embedded output status map, per-chunk evidence reports, an independent exact-placement benchmark, and adversarial tests. The pristine reference and mutation manifest are never passed to the recovery subprocess in the end-to-end trial.

The first supported fixture is a single fixed `(512,512)` little-endian `uint32` dataset with `(16,16)` unfiltered chunks. Its actual generated layout has a version-0 superblock, a version-1 object header at relative address 800, a version-3 chunked layout, a type-1 v1 B-tree rooted at 1400, level-one root, 18 leaves, and 1,024 chunks. This was verified by parsing the selected object's layout and comparing every chunk record with h5py on the healthy file. H5Reclaim does not assign ownership by scanning for `TREE` signatures.

The controlled mutation changes one verified eight-byte root child pointer to the HDF5 undefined address in a copy. For the default fixture, 57 affected chunks fail standard h5py reads, while 967 unaffected chunks read exactly. Recovery finds the detached leaf through both reachable neighboring leaves, reciprocal sibling links, and matching parent key bounds.

## Checks run

Environment: Python 3.12.14, h5py 3.12.1, HDF5 1.14.4, NumPy 2.3.5. The local test environment is `/tmp/h5reclaim-venv`; it is not included in the repository. HDF5 command-line inspection utilities were unavailable, so independent h5py chunk-info cross-checks and the project raw parser were used.

| Check | Observed result |
| --- | --- |
| Healthy generation and h5py round-trip | 262,144 values exact; 1,024 allocated chunks; pristine SHA-256 `6c72cf2d1e277e8d7ed05d00ce4be7bb9f1ae6bff973c9c3feaefc0f810ec10d` |
| Raw tree versus healthy HDF5 library | All 1,024 coordinate/address/size/filter records matched `get_chunk_info` |
| Default controlled damage | One parent pointer changed; damaged SHA-256 `24e0cbdc105aac14fbe33eaa04e66d6045a9b9e0c44a008ceef79847f2f36fdf`; 57 affected read errors and 967 unaffected exact chunks |
| Direct `recover` on that copy | 1,024/1,024 chunks and 262,144/262,144 values exact against pristine; 57 mappings used reconstructed link; source hash unchanged |
| Independent random-value benchmark | Seed `92594152704421461`; 1,024/1,024 exact chunks, zero wrong placements, zero incorrect-value chunks, zero missing regions, 57/57 native-unavailable chunks recovered; pristine and damaged hashes unchanged |
| Unit/integration tests | After editable install, `python -m unittest discover -s tests -v` passed 23 tests in 1.92 s; includes parser boundaries, fixture, damage, black-box evaluation, integrity-limit demonstration, safety negatives, and fault-injected publication cleanup |
| Package build and installed wheel | Built `h5reclaim-0.1.0-py3-none-any.whl` with `pip wheel`, installed it in an isolated target, confirmed the CLI imported that wheel, ran `inspect` and `recover`, verified 262,144 exact output elements and 57 reconstructed chunks, and confirmed the embedded and external reports match |

Run from an installed environment:

```sh
python -m unittest discover -s tests -v
python benchmarks/run_recovery.py --work-dir /tmp/a-new-h5reclaim-trial
```

The random-value benchmark's pristine SHA-256 was `a39249692c51f51a77580234a0b0fa3a2451563b7609693c9ba09b8a146651db` and its damaged SHA-256 was `1a43c322e8e1ca7f8b1cc6a0d1f2bb6455ee88ef44828a0d46bf3abd59d3eedf`. These hashes pertain to the recorded seed and environment; rerunning with another HDF5 version may produce different file bytes even if the values and checks pass.

A second independent random-value trial used seed `17444100253769056495` and again recovered all 1,024 chunks, including all 57 unavailable to the native reader, with zero wrong placements, incorrect-value chunks, or missing regions. Both pristine and damaged source hashes were unchanged. The first attempt to repeat this trial used a nonempty work directory and correctly refused to overwrite it; a fresh directory succeeded. The initially attempted `python -m build` command was unavailable in the test environment, so the documented wheel check used `pip wheel` instead.

The Apache 2.0 `LICENSE` text was synchronized byte for byte with GitHub's license API on 2026-09-27. This changed a leading blank line only; the declared license and program behavior remain the same.

## Limits and next work

This is an experimental narrow release. It requires h5py to resolve selected dataset metadata from the damaged file. Only v0/v1 superblocks, inline v1 object headers, v3 chunked layouts, a level-one v1 raw-data B-tree, unfiltered fixed-size rank-two `uint32`, and at most one lost root-to-leaf pointer are implemented. A two-sided sibling bridge and parent key interval support structural attribution, not historical byte integrity. The report says when no checksum is available.

Meaningful future work includes running relevant existing recovery tools on the same inputs, a larger negative corpus with stale/deallocated metadata and distractor datasets, additional indexing families and metadata variants only where users need them, fuzzing stable parser entry points, independent technical review, and suitably consented real cases. No competitor advantage, production reliability, outside user, or publication has been established.
