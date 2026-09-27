# Current status

Updated: 2026-09-27

## Version 0.3.3: vary the supported damage position

The fixture damage tool accepts `--child-index` to change a specified,
verified interior root child pointer in a disposable copy. Its default still
selects the first eligible position. A new test constructs a second synthetic
layout with random measurement values and separately breaks all four eligible
interior child positions. Each damaged copy is passed by itself to the public
recovery subprocess, and its output is compared against the healthy reference
by the test. Invalid child positions are refused before any output is made.
The existing authentic GWOSC file has only one interior position with two
surviving neighbors, so its real-data benchmark remains one controlled break.

This adds location coverage within the existing single-pointer failure mode.
It does not randomize other corruption types, validate arbitrary HDF5 layouts,
or show that absent or overwritten payload bytes can be reconstructed. The
healthy reference is used to make and score controlled test cases; recovery
still receives only a damaged file, selected dataset path, and new output
destinations. A structurally located unfiltered payload has no independent
checksum and is not guaranteed to match its historical value.
The updated Linux suite passes 57 discovered tests, including the four-position
matrix case.

## Version 0.3.2: readable command summaries

`h5reclaim survey` and `h5reclaim inspect` now present a concise readable
summary by default. The corpus survey and controlled GWOSC recovery benchmark
also show a readable result, relevant counts, and the location of trial files.
Each of these four commands accepts `--json` for its complete machine-readable
summary; the GWOSC trial always saves independent scoring to
`truth/evaluation.json` and the recovery program saves its separate detailed
report to `results/recovery.json`. The new presentation changes no supported
HDF5 layouts or recovery decisions. A readable survey displays at most five
datasets, putting candidates first, while its JSON includes every inventoried
entry and support reason. `recover` already printed a short completion summary
and still writes its detailed JSON report and status map.

A user supplied a native Windows PowerShell run of the bundled GWOSC trial
after the 0.3.1 snapshot fix: 128/128 chunks recovered, 57 via the
reconstructed link, 57 ordinary HDF5 reads failed or returned incorrect
chunks, and no wrong float64 bits or missing regions. The original file's
SHA-256 before and after matched; the damaged copy's SHA-256 was unchanged
during recovery. The same environment reported `Ran 50 tests` and
`OK (skipped=1)` from `python -m unittest discover -s tests -q`: 49 passed,
one was skipped. That test skips when a Windows account lacks the privilege
to create a symbolic link. These are results from the user environment for
the controlled input. They do not establish behavior for arbitrary corrupted
research files or every Windows setup.

The updated 0.3.2 tree passed 56 discovered tests on Linux after the readable
output changes. That count includes new command-output checks; the 50-test
Windows result above came from the earlier package that the user ran.

## Version 0.3.1: Windows source snapshot fix

On Windows, both bundled commands could stop before inspecting HDF5 with
`input changed while it was being opened`. The source snapshot had required
the complete metadata tuple from `os.fstat(open_handle)` to equal the tuple
from `Path.stat()`. Those two APIs can report different file IDs or timestamps
for an unchanged Windows file. The check now compares pathname metadata with
pathname metadata before and after opening/copying, compares descriptor
metadata with descriptor metadata before and after copying, checks regular-file
type and size, and still rehashes and checks the pathname before accepting the
analysis or publishing output. A replaced path with identical bytes and
changed bytes are both refused by the regression tests.

The Linux run passed 50 tests, verified all four original corpus hashes and
the 1-candidate/250-unsupported baseline, and recovered all 128 GWOSC chunks
in the controlled trial with 57 via the severed link and no wrong bits.
A test simulates discrepant Windows `stat` and `fstat` metadata. A GitHub
Actions workflow is configured to run the tests and both bundled commands on
Windows and Linux after the code is uploaded to GitHub. The native Windows
result subsequently supplied by a user is recorded above.

## Version 0.3.0: authentic scientific data

Four unchanged, license-attributed scientific HDF5 files are bundled in
`corpus/files/` with SHA-256 hashes and source records in `corpus/manifest.json`.
`python benchmarks/run_real_corpus.py` verifies all four originals before
inventorying 251 local datasets. One dataset is a candidate: the original
GWOSC GW150914 Hanford 16 kHz `/strain/Strain` array, rank-one canonical
little-endian IEEE float64, shape `(524288,)`, chunks `(4096,)`, Fletcher32
then DEFLATE, a v1 object-header continuation, and a level-one v1 raw-data
B-tree with 128 chunks in three leaves. The other 250 datasets remain
unsupported under explicit rules, including the 4 kHz strain variant whose
root is level zero. A candidate is a metadata assessment, not a recovered file.

The rank-one adapter parses the one bounded continuation, checks the selected
object's index against rank-one key coordinates, reverses only the declared
filter pipeline with bounded decompression, verifies stored Fletcher32 for
each accepted chunk, and writes direct unfiltered bytes to preserve exact
float bits. It copies seven bounded scalar attributes of this selected
dataset and reports copied/omitted names. Other attributes, scale links,
sibling objects, and full research context are not reproduced. The rank-two
unfiltered path and its no-checksum integrity warning remain supported.

`python benchmarks/run_gwosc_recovery.py` independently checked all 128 raw
chunk index records against h5py on the untouched original, made one verified
root child-pointer change in a byte-for-byte **copy**, and measured 57 chunks
that a native reader could no longer return correctly. Recovery of that copy
exported 128/128 chunks at their original sample coordinates with identical
float64 bit patterns, including 57/57 from the reconstructed link. The seven
source dataset scalar attributes, embedded and external reports, status map,
and both original and damaged-input SHA-256 hashes were checked. This is one
controlled corruption against authentic experiment data. No organically
damaged research file or broad real-world recovery has been demonstrated.

At the time of 0.3.0, the suite passed 47 tests (`PYTHONPATH=src python -m unittest discover
-s tests -q`), including real bundled-source tests and negative checksum,
deflate, filter mask, and unsupported pipeline cases. The corpus baseline
passes with four hashes verified, 1 candidate, 250 unsupported. Earlier
0.2.0 results below are retained as historical synthetic-fixture evidence.

## Implemented

The 0.2.0 package contained an installable `h5reclaim` CLI with `survey`, `inspect`, and `recover`. Survey inventories local dataset metadata, storage layouts, filters, and support reasons without reading dataset values or resolving soft or external links. The recovery path uses a bounded parser for a declared HDF5 v1 B-tree case, a private source snapshot shared by h5py and the raw parser, a deterministic healthy fixture, a verified broken-pointer copy tool, an embedded output status map, per-chunk evidence reports, an exact-placement benchmark, and adversarial tests. The pristine reference and mutation manifest are never passed as arguments to the recovery subprocess in the end-to-end trial, though the same-user process can access their sibling directory on disk.

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

This is an experimental narrow release. It requires h5py to resolve the selected dataset metadata from the damaged file. Only v0/v1 superblocks, v1 object headers (inline or one bounded continuation), v3 chunked layouts, a level-one v1 raw-data B-tree, fixed aligned chunks of rank-two canonical little-endian `uint32` without filters **or** rank-one canonical little-endian IEEE `float64` with exactly Fletcher32 followed by DEFLATE, and at most one lost root-to-leaf pointer are implemented. One selected local dataset can be recovered from a file containing other local datasets, but detached candidates still require the selected object's root and two-sided sibling bridge with matching parent key interval. These conditions support structural attribution; a Fletcher32 match detects some byte errors but cannot prove a measurement's origin or historical authenticity. Survey reports unsupported or indeterminate structures without converting them.

Meaningful future work includes running relevant existing recovery tools on the same inputs, a larger negative corpus with stale/deallocated metadata and varied distractor datasets, additional indexing families and metadata variants based on real demand, fuzzing stable parser entry points, independent technical review, and suitably consented naturally damaged cases. The benchmarks keep truth out of the recovery program's explicit inputs, but their sibling directories are not a filesystem isolation boundary. No competitor advantage, production reliability, outside user, or publication has been established.
