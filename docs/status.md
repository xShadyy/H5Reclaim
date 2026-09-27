# Current status

Updated: 2026-09-27

## Implemented

The current package is version 0.2.0. It contains an installable `h5reclaim` CLI with `survey`, `inspect`, and `recover`. Survey inventories local dataset metadata, storage layouts, filters, and support reasons without reading dataset values or resolving soft or external links. The recovery path uses a bounded parser for a declared HDF5 v1 B-tree case, a private source snapshot shared by h5py and the raw parser, a deterministic healthy fixture, a verified broken-pointer copy tool, an embedded output status map, per-chunk evidence reports, an exact-placement benchmark, and adversarial tests. The pristine reference and mutation manifest are never passed as arguments to the recovery subprocess in the end-to-end trial, though the same-user process can access their sibling directory on disk.

The first supported fixture is a single fixed `(512,512)` little-endian `uint32` dataset with `(16,16)` unfiltered chunks. Its actual generated layout has a version-0 superblock, a version-1 object header at relative address 800, a version-3 chunked layout, a type-1 v1 B-tree rooted at 1400, level-one root, 18 leaves, and 1,024 chunks. This was verified by parsing the selected object's layout and comparing every chunk record with h5py on the healthy file. H5Reclaim does not assign ownership by scanning for `TREE` signatures.

The controlled mutation changes one verified eight-byte root child pointer to the HDF5 undefined address in a copy. For the default fixture, 57 affected chunks fail standard h5py reads, while 967 unaffected chunks read exactly. Recovery finds the detached leaf through both reachable neighboring leaves, reciprocal sibling links, and matching parent key bounds.

Recovery now selects one local dataset per invocation even when other local datasets coexist. A regression uses an identically shaped distractor and a nested selected path. Direct byte export accepts only canonical little-endian unsigned 32-bit storage with full precision, zero bit offset, and standard padding. A valid HDF5 four-byte integer with only 24 significant bits exposed a false-value export before this check was added; it is now refused. Accepted payloads must not overlap each other or the parsed superblock, selected object header, or full allocated B-tree node extents. Source identity and SHA-256 are checked around the private snapshot and again before publishing output. The snapshot temporarily needs disk space up to the source's size, within the 128 MiB input limit. Original attributes, dimension scales, links, sibling objects, and other scientific context are not reproduced in the output; this limit is recorded in the report.

## Checks run

Original experiment environment: Python 3.12.14, h5py 3.12.1, HDF5 1.14.4, NumPy 2.3.5. A subsequent verification used Python 3.12.14, h5py 3.16.0, HDF5 2.0.0, and NumPy 2.5.3. Both environments are outside the repository. HDF5 command-line inspection utilities were unavailable for the original experiment, so h5py chunk-info cross-checks and the project raw parser were used.

| Check | Observed result |
| --- | --- |
| Healthy generation and h5py round-trip | 262,144 values exact; 1,024 allocated chunks; pristine SHA-256 `6c72cf2d1e277e8d7ed05d00ce4be7bb9f1ae6bff973c9c3feaefc0f810ec10d` |
| Raw tree versus healthy HDF5 library | All 1,024 coordinate/address/size/filter records matched `get_chunk_info` |
| Default controlled damage | One parent pointer changed; damaged SHA-256 `24e0cbdc105aac14fbe33eaa04e66d6045a9b9e0c44a008ceef79847f2f36fdf`; 57 affected read errors and 967 unaffected exact chunks |
| Direct `recover` on that copy | 1,024/1,024 chunks and 262,144/262,144 values exact against pristine; 57 mappings used reconstructed link; source hash unchanged |
| Independent random-value benchmark | Seed `92594152704421461`; 1,024/1,024 exact chunks, zero wrong placements, zero incorrect-value chunks, zero missing regions, 57/57 native-unavailable chunks recovered; pristine and damaged hashes unchanged |
| Original unit/integration tests | After editable install, `python -m unittest discover -s tests -v` passed 23 tests in 1.92 s; includes parser boundaries, fixture, damage, black-box evaluation, integrity-limit demonstration, safety negatives, and fault-injected publication cleanup |
| Prior 0.1.0 package build and installed wheel | Built `h5reclaim-0.1.0-py3-none-any.whl` with `pip wheel`, installed it in an isolated target, confirmed the CLI imported that wheel, ran `inspect` and `recover`, verified 262,144 exact output elements and 57 reconstructed chunks, and confirmed the embedded and external reports match |
| Current unit/integration tests | The final `python -m unittest discover -s tests -q` run passed 35 tests in 6.418 s; a separate run under Python 3.12.14, h5py 3.16.0, HDF5 2.0.0, NumPy 2.5.3 also passed all 35. Tests include survey behavior, canonical datatype checks, multi-dataset selection, and metadata overlap rejection |
| Current random-value benchmark | A fresh trial with seed `11862106990323427801` recovered 1,024/1,024 chunks, including 57/57 unavailable to ordinary reads, with zero wrong placements, incorrect values, or missing regions; source and pristine hashes remained unchanged |
| Current 0.2.0 package build | Built and installed the `h5reclaim-0.2.0` wheel; the installed CLI's `survey` classified the damaged controlled fixture as a candidate |

Run from an installed environment:

```sh
python -m unittest discover -s tests -v
python benchmarks/run_recovery.py --work-dir /tmp/a-new-h5reclaim-trial
```

The random-value benchmark's pristine SHA-256 was `a39249692c51f51a77580234a0b0fa3a2451563b7609693c9ba09b8a146651db` and its damaged SHA-256 was `1a43c322e8e1ca7f8b1cc6a0d1f2bb6455ee88ef44828a0d46bf3abd59d3eedf`. These hashes pertain to the recorded seed and environment; rerunning with another HDF5 version may produce different file bytes even if the values and checks pass.

A second independent random-value trial used seed `17444100253769056495` and again recovered all 1,024 chunks, including all 57 unavailable to the native reader, with zero wrong placements, incorrect-value chunks, or missing regions. Both pristine and damaged source hashes were unchanged. The first attempt to repeat this trial used a nonempty work directory and correctly refused to overwrite it; a fresh directory succeeded. The initially attempted `python -m build` command was unavailable in the test environment, so the documented wheel check used `pip wheel` instead.

The Apache 2.0 `LICENSE` text was synchronized byte for byte with GitHub's license API on 2026-09-27. This changed a leading blank line only; the declared license and program behavior remain the same.

## Limits and next work

This is an experimental narrow release. It requires h5py to resolve the selected dataset metadata from the damaged file. Only v0/v1 superblocks, inline v1 object headers, v3 chunked layouts, a level-one v1 raw-data B-tree, unfiltered fixed-size rank-two canonical little-endian `uint32`, and at most one lost root-to-leaf pointer are implemented. One selected local dataset can be recovered from a file containing other local datasets, but detached candidates still require the selected object's root and two-sided sibling bridge with matching parent key interval. These conditions support structural attribution, not historical byte integrity. The report says when no checksum is available and warns that scientific metadata is not preserved. Survey reports unsupported or indeterminate structures without converting them.

Meaningful future work includes running relevant existing recovery tools on the same inputs, a larger negative corpus with stale/deallocated metadata and varied distractor datasets, additional indexing families and metadata variants only where users need them, fuzzing stable parser entry points, independent technical review, and suitably consented real cases. The benchmark keeps truth out of the recovery program's explicit inputs, but its sibling directories are not a filesystem isolation boundary. No competitor advantage, production reliability, outside user, or publication has been established.
