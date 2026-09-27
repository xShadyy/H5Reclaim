# Current status

Updated: 2026-09-27

## Version 0.5.0: bounded evidence ledger, modern direct indexes, broader readable export

This release adds a typed evidence ledger to structural recovery. For each
accepted chunk, the report records the selected dataset anchor, observed
index-pointer path, physical byte extent, raw and decoded hashes, filter mask,
parser checks, checksum result or its absence, and contradictions. A second
reconciliation refuses coordinate or byte-range conflicts. An anchored chunk
whose decoder fails is unassigned; `export-fragments` can publish its bounded
raw bytes as a separate, coordinate-free ZIP after rechecking source and
fragment hashes. Missing links without a justified physical extent do not
produce fragments. The ledger records verified parser observations; its
hashes are not independent proof that historical measurements were unchanged.

The older version-1 B-tree route now traverses deeper intact trees and can
bridge one missing **leaf** pointer below a deeper root if one unique
two-sided sibling bridge and the exact parent interval survive. The
version-2/3 superblock, checksum-validated version-2 object-header and
continuation, and version-4/5 chunked-layout parsers support intact modern
single-chunk, implicit, and bounded nonpaged, unfiltered, fully allocated
fixed-array indexes. The fixed-array route validates FAHD/FADB checksums,
back-pointers, slot addresses, and row-major coordinates against native HDF5
on generated fixtures. The authentic four-file corpus still has only two
structural candidates and does not independently demonstrate fixed-array
recovery on scientific data. Modern export is explicitly an
`intact_index_export`, not a damaged-modern-index reconstruction. Paged,
filtered, or sparse fixed arrays, extensible arrays, and version-2 B-trees
remain unsupported by raw structural recovery. Positive end-to-end fixtures
cover unfiltered single, implicit grid, and filtered single chunks, with
checksum, pointer, layout, and datatype contradiction negatives.

`export-readable` now preserves the selected local dataset's HDF5 type,
maxshape, fill rules, layout, and supported built-in filter order for bounded
compact, contiguous, and chunked data. Supported fixed-width representations
include canonical numeric, bool/enum, complex, fixed strings/opaque bytes,
and bounded compound/array fields. Sparse chunks have a **partial** output
with `/_h5reclaim/validity` marking accepted current values separately from
unknown output fill. Per-chunk source addresses, masks, sizes, and raw hashes
appear in the report. Reference/VLEN types, plugins, external/VDS values,
and noncanonical numeric storage are still refused. Native reads remain in
process, not in a crash-isolated worker.

`diagnose` observes superblock status and declared external raw, virtual, and
external-link dependencies without reading values. `--related-files` accepts
an exact-name, SHA-256-pinned manifest of explicitly supplied absolute file
paths and checks file identity, fixed raw byte ranges, and local HDF5 target
metadata. It does **not** export external/VDS values or resolve dynamic
patterns and transitive dependencies. `probe-status` optionally runs
`h5clear --status` only on a disposable copy of an eligible version-3 file;
it is a metadata-openability experiment, not general repair. The real
`h5clear` utility was absent in this environment, so its simulated
status-only success path is a test, not an observed real utility run.

The source snapshot now streams in 1 MiB blocks with a 4 GiB default limit,
30-minute copy deadline, and a preflight for the full logical source size
plus disk reserve. A separate 129 MiB sparse-source test checks operation
beyond the old 128 MiB cap. Structural dataset, chunk, native export, and
report limits remain smaller and explicit in [usage](usage.md). Full native
processing does not yet have a process-wide timeout or memory sandbox.

The seeded authentic-file damage matrix with seed `20260927` and two trials
passed 23 specified cases: 954 accepted chunks independently matched the
original at exact coordinates, including 226 inaccessible to ordinary reads
in the damaged copies; no false accepted chunk was observed; 14 cases
refused safely. A second seed also passed. These correlated constructed
faults and four bundled originals have no known relationship to the
population of real failures. This does **not** establish anything near a
99% success rate. Exact restoration of overwritten unique bytes without
independent redundancy is impossible, and unknown ownership must stay
unknown. Naturally damaged, held-out cases and a defined fault population
are needed before a statistical claim.

The final Linux regression run passed **163 discovered tests**. The pinned
corpus check still reports four verified originals and 2 structural candidates
among 251 datasets. The controlled 16 kHz GWOSC trial recovered 128/128
bit-exact chunks from the damaged copy, with 57/57 native-inaccessible chunks
behind one reconstructed leaf link. The source and damaged copy remained
unchanged. These outcomes are specific to the tests and their environment;
this release has not been rerun on the user's Windows machine.

The next concrete milestone is an anchored, checksummed version-2 B-tree
parser with independent coordinate tests and misleading stale-node negatives,
followed by a worker boundary for native reads. Paged/filtered fixed arrays,
extensible arrays, edge chunks, VLEN/reference graphs, and dependent-value
export each need separate evidence and resource rules before support claims.

## Version 0.4.0: triage, bounded native export, and another authentic index shape

`h5reclaim diagnose SOURCE [--dataset PATH]` checks a stable snapshot's HDF5
signature and inventories metadata if possible. It reports limited evidence
when the header is truncated or the library cannot read metadata. It does not
read measurements, scan for plausible payloads, or attempt recovery. The
readable terminal summary has an optional full `--json` form. `survey` remains
the detailed local-dataset inventory.

`h5reclaim export-readable` uses the standard HDF5 reader on a fully allocated,
local, primitive numeric dataset and copies its **current readable** values
into a new file. It verifies a bitwise readback, checks source preservation,
and identifies the operation as `readable_export` in an external and embedded
report. This route covers bounded compact, contiguous, and chunked layouts,
including some newer indexes, if native reads actually succeed. It accepts
only rank one through four, standard fixed-width numeric representation,
built-in DEFLATE/shuffle/Fletcher32 filters, a source and logical size no
larger than 128 MiB, blocks no larger than 1 MiB, stored chunks no larger
than 2 MiB, and at most 8,192 allocated chunks. It refuses sparse reads that
could silently return fill, unsupported types, plugins, external/virtual
storage, and invalid allocation addresses. A successful copy is not
structural repair and does not verify historical scientific measurements.

Optional `--hints hints.json` accepts a bounded scientist assertion about
dataset path, shape, chunks, datatype, filters, and/or damaged-input SHA-256.
Observed conflicts stop export; fields not observable from the damaged file
remain explicitly unverified. Hints do not establish chunk ownership or
authorize reconstructing an unknown layout. The two export commands can take
their selected dataset path from the hints file.

Structural export now also accepts a completely intact, direct level-zero
version-1 raw-data B-tree root, with no attempt to infer a missing direct
payload pointer. The authentic 4 kHz GWOSC strain file supplies this layout:
64/64 chunks exported at exact original float64 bit values, zero reconstructed
links. Its corrupt-DEFLATE copy produces 63 exact accepted chunks and one
explicit `decode_failed` chunk; a missing direct pointer refuses without
publishing an output. The existing 16 kHz broken-link case still recovered
128/128 bit-exact chunks, including 57 behind the reconstructed link.

The corpus survey verifies four original hashes and inventories 251 datasets:
two structural candidates and 249 unsupported. `run_damage_catalog.py`
checks ten chosen cases on disposable authentic-file copies, including
payload corruption, combined index/payload damage, multiple broken links,
header damage, intact direct indexing, and unsupported layouts. It scores
accepted chunks against evaluator-only original values at the original
coordinates; a refusal is not counted as recovered data. This deliberately
constructed catalog has no representative fault distribution, and none of
these results supports a 99% claim. See
[the generalization plan](generalization-plan.md) for specific remaining
format families, fault classes, evidence gates, and statistical requirements.

For bounded metadata reads, this release omits variable-length string
attributes before dereferencing them. Their small on-disk attribute record
can reference a much larger heap value. In the 16 kHz GWOSC trial, three
fixed-size numeric attributes are copied and checked; four heap-backed
strings are explicitly listed as omitted. Users need the original metadata
or an independent record to restore those descriptions. Other links, scales,
objects, and scientific context are also outside the selected-dataset export.

The current Linux run passed 94 discovered tests and the corpus survey,
16 kHz controlled recovery benchmark, and ten-case catalog. The earlier
Windows PowerShell evidence below applies to the preceding version; this
new release has not been rerun on that computer. The next concrete recovery
milestone is an evidence-backed deeper version-1 tree or a documented newer
chunk-index family, with positive and misleading-negative cases from real
files. No software can recreate uniquely overwritten measurements without
redundancy or independent evidence.

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
