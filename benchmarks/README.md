# Recovery benchmarks

## Version 0.10 focused checks

The selected chunk-dimension metadata trial tests a one-byte damaged object
header against its original checksum, then requires the corrected rooted
schema, complete index and competing-owner inventory to agree. Capsule
regression also rejects an otherwise readable selected object whose encoded
HDF5 enum labels differ from the earlier captured type. Older-tree tests
bridge one interior missing internal subtree when two rooted reciprocal
siblings and all descendants agree; missing anchors and a second fault
refuse. Larger structural tests include a physically sparse file over 4 GiB
with 8,200 selected grid slots, a separate 8,200 allocated-chunk trial, and
invalid child-checksum or scan-budget refusals. Run:

```sh
python -m unittest tests.test_header_dimension_trial tests.test_deep_v1 tests.test_large_structural tests.test_annotation_collisions tests.test_v10_cli_routes -q
```

These are targeted generated tests. Test truth is compared after the route
runs, and is not an explicit recovery argument; tests in one workspace do not
form an isolation boundary. They do not estimate how often those faults
occur in real laboratories or establish a field success percentage.

## Version 0.9 focused regression coverage

Run `python benchmarks/run_authentic_v09_routes.py` for a readable controlled
trial on the pinned authentic GWOSC strain file (`--json` prints the complete
evaluator evidence). A prior capsule survives separate root-link, object-header,
and index faults with 64 of 64 chunks exact in each trial. Two same-stripe
payload losses are reconstructed by prior GF(256) parity with 64 of 64 exact;
a physical tail cut yields 63 exact and one unknown chunk. The evaluator
retains the original only to compare every accepted value bitwise and also
checks that a tampered capsule is refused. Run
`python -m unittest benchmarks.test_authentic_v09_routes -q` for its
false-acceptance self-check. These five constructed cases on one source do
not estimate a field recovery rate.

The following generated tests exercise distinct new routes and deliberately
contradictory inputs. They are **development tests**, not an independent
population sample:

```sh
python -m unittest tests.test_chunk_truncation tests.test_metadata_correction tests.test_recovery_capsule tests.test_erasure_sidecar tests.test_fixed_schemas tests.test_large_streaming tests.test_modern_link_repair_cli -q
```

The capsule tests include a pre-incident capture of the pinned authentic
GWOSC strain file, a broken-root disposable copy, and evaluator-only exact
comparison. The restoration receives the damaged bytes and independent
capsule, not the pristine reference. Other cases check physically cut chunks,
one-byte checked metadata trials, changed or forged prospective sidecars,
more than the retained parity shard count, exact HDF5 fixed-record schema,
and sparse current-value streaming. A selected successful trial does not
measure the prevalence of its failure family in scientific practice. See the
[damage taxonomy](../docs/damage-taxonomy.md) for route eligibility and
remaining gaps.

## Independently supplied held-out panel

`python benchmarks/run_heldout_trials.py --manifest PANEL.json --work-dir NEW-DIRECTORY`
evaluates a **predeclared panel** of previously unused, intact HDF5 files.
The source files are used only to choose documented mutation sites and to score
bit-exact values after each public `h5reclaim rescue` subprocess has processed
a disposable damaged copy. The recovery subprocess receives the damaged file,
selected dataset path, and output destinations. The pristine files remain on
the same filesystem and are not hidden from a malicious process with the same
permissions. This protocol separates program inputs, not adversarial access.

The manifest must sit beside the original files and name them through safe
relative paths. Each source is checked against a recorded size and SHA-256
before the trials. Example format:

```json
{
  "schema_version": 1,
  "cohort": "laboratory-2026-independent-panel",
  "entries": [
    {
      "id": "detector_a",
      "path": "files/detector_a.h5",
      "size_bytes": 123456,
      "sha256": "replace-with-64-lowercase-hex-digits",
      "dataset": "/measurements/signal",
      "provenance": "Instrument, acquisition date, producer, and permission to evaluate"
    }
  ]
}
```

The displayed size and hash are placeholders, not a bundled source. A real
held-out result requires a panel selected and pinned before examining the
tool's outcomes. The four bundled scientific originals have been used during
development and cannot count as independent held-out validation. This command
accepts 1 to 128 files of up to 64 MiB each, with one fixed-size numeric
dataset per entry (rank at most four, at most 1,048,576 elements and 16 MiB
decoded truth). More than one dataset from a file can be entered under
different IDs, but their trials remain correlated by source.

By default, one seeded trial per source is planned for each of six classes:
intact control, HDF5 signature byte, selected object-header byte, one payload
bit, an up-to-eight-byte payload burst, and truncation inside the physically
last allocated payload. `--seed N`, `--trials N`, and `--faults
intact,payload_bit,...` change the declared design. Compact and virtual data
have no independently located local payload for this injection; their payload
cases are counted as **excluded**, with reasons, rather than included in a
success rate. A truncation may also remove metadata physically after the
selected payload, which its mutation record makes visible. These chosen
injections do not reproduce all real acquisition and storage failures.

`evaluation.json` records each case's pristine and damaged hashes, mutation
site, validity map comparison, exact accepted elements, wrong accepted
elements, unknown elements, refused elements, and protocol errors. The
readable table shows both **planned** and **eligible** cases for each fault
class. A safe refusal contributes zero exact values. The command exits 1
if any wrong historical value is accepted or evaluation is invalid, but still
writes the complete report for investigation. An unchecksummed payload flip
can trigger that exit intentionally. Run the evaluator's own regression with
`python -m unittest benchmarks.test_heldout_trials -q`.

This panel reports conditional counts and denominators, not a field
probability or a claim of 99% recovery. Repeated injections on one file are
correlated, the fault classes are chosen, and pristine originals are not
naturally damaged files. Real damaged samples without an independent truth
record cannot be scored for exact historical values. The [HDF5 format
specification](https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html)
distinguishes metadata structures and payload layouts. [Fletcher32 and other
filters](https://support.hdfgroup.org/documentation/hdf5/latest/_h5_z__u_g.html)
change which byte edits the library detects. [VDS missing sources can appear
as fill](https://support.hdfgroup.org/documentation/hdf5-docs/advanced_topics/intro_VDS.html),
and [SWMR writes and file locking](https://support.hdfgroup.org/documentation/hdf5/latest/_file_lock.html)
require different acquisition histories from an isolated bit flip. The
[h5clear guide](https://support.hdfgroup.org/documentation/hdf5/latest/_h5_t_o_o_l__c_r__u_g.html)
also warns that clearing a status flag is not general corruption repair.

The next validation gates are to pre-register independent laboratories and
their failure histories, add redirections and stale allocations with repaired
checksums across *every* modern index family, test missing related files and
transitive VDS mappings with pinned bundles, and separately quantify the
benefit of independently retained replicas or parity. Each new fault class
must have a precise eligibility rule, positive and deliberately misleading
cases, independent truth where possible, and a stated denominator.

## Authentic native-readable scientific representatives

Run `python benchmarks/run_real_readable_corpus.py` to evaluate the public
`export-readable` route on the two intact Zenodo representative datasets.
The quantum file contributes a contiguous 1,000 by 2 `uint8` dataset; the
aircraft file contributes 2,088 contiguous records with a 37-field compound
datatype. The evaluator verifies all four original corpus hashes, supplies
only disposable current-file copies to the subprocess, and independently
checks exact current value bits, HDF5 datatype, shape, contiguous physical
range and raw hash, and the embedded/external report. In the observed run,
both exports passed: 2,000 quantum elements and 2,088 aircraft records matched
the intact originals bit for bit. The scorer's tamper test changes an output
value and detects the mismatch. `--json` prints details, and `--work-dir
NEW-OR-EMPTY-DIR` retains evidence. Native-readable export does not repair
damaged metadata and cannot certify an earlier, unrecorded measurement.

## Authentic prospective baseline integrity trials

Run `python benchmarks/run_authentic_baseline_integrity.py` for two controlled
payload mutations on pinned, authentic scientific files. The public capture
command first records coordinate-specific hashes from each pristine source in
a separate sidecar. The trial changes exactly one byte of a disposable copy,
then passes only that copy, the retained baseline, and its separately computed
SHA-256 to the public `rescue` command. The evaluator separately reads the
original to score exact accepted values and the output validity map. Both
original and damaged input hashes must remain unchanged during evaluation.

The Zenodo quantum feedback trial changes one of 2,000 contiguous `uint8`
measurements. Native HDF5 still reads a plausible but wrong value. A prior
element baseline withholds that coordinate: 1,999 exact accepted elements,
one unknown, and zero false accepted. The GWOSC 4 kHz trial changes a byte in
one compressed strain chunk. Its filter rejects that chunk; 63 of 64 chunks
remain exact and one is unknown. This latter case verifies safe handling of a
filtered authentic payload, but does not isolate the extra detection benefit
of a prior hash because the filter already detects the mutation. In each
case, modifying the retained sidecar while supplying its original independent
SHA-256 is refused without publishing output.

Use `--json` for machine-readable scoring or `--work-dir NEW-OR-EMPTY-DIR` to
retain the copies, sidecars, output, and reports. The default output is a
readable summary. These two selected faults were applied to intact sources;
they do not measure natural damage frequency or field recovery success.
Hashes alone cannot restore a missing or changed measurement.

## Intact authentic candidate exports

Run `python benchmarks/run_real_candidate_exports.py` to verify all pinned
scientific original hashes, survey all 251 local datasets, and give each
currently classified structural candidate a disposable source copy. The
public recovery subprocess sees only that copy and a selected path. The
evaluator separately checks every accepted output chunk's exact value bits,
coordinate, datatype, physical source range, the unchanged original/input
hashes, and consistency of the embedded report. `--json` prints the full
evaluation; `--work-dir NEW-OR-EMPTY-DIR` retains trial inputs and outputs.

With the pinned six-candidate baseline, six intact exports passed: 196/196
chunks matched the original at their coordinates, comprising 192 strain
chunks and one quality mask chunk in each of four quality datasets. Neither
the survey nor this evaluator applies damage to those four quality datasets;
these outcomes establish intact export on their particular scientific
layouts, not their ability to repair a broken index. Candidate status alone
does not guarantee an accepted measurement, and 245 other corpus datasets
remain structurally unsupported. These figures do not estimate recovery
probability for damaged files.

## Stratified HDF5 layout and fault matrix

From the repository root, with h5py and NumPy installed, run:

```sh
python benchmarks/run_stratified_layouts.py --seed 20260927 --trials 2
```

This independently generated corpus covers current single-chunk, implicit,
nonpaged fixed-array, paged fixed-array, extensible-array, and version-two
B-tree chunk index layouts; compact and contiguous storage; sparse partial
edge chunks; fixed strings, compound records, and variable-length strings;
external raw and virtual dataset dependencies. Some families are **safe
refusal** cases for structural recovery, although a separate native-readable
export may succeed when the metadata and required payloads are intact. It
also verifies the hashes of all four authentic scientific originals and
scores one intact 4 kHz GWOSC file, whose data were not generated by us.
The [seeded scientific damage matrix](#seeded-scientific-damage-matrix)
separately exercises pointer damage on the authentic 16 kHz GWOSC file.

Each generated file has reproducible values from one seeded NumPy generator.
Faults include signature, superblock and selected-object checksums,
fixed-array data-block checksum, a duplicate payload pointer with its
metadata checksum deliberately repaired, filtered and unfiltered payload
damage, and truncation inside a stored chunk. `--trials N` selects 1 through
10 seeded positions/bits for each fault. The public CLI subprocess sees only
its copied current/damaged input. A separate evaluator compares each accepted
region's exact dtype, coordinate and bit pattern against the unmodified
reference, checks source hashes and the embedded report, and checks unknown
regions separately. The evaluator can read the reference and damaged files
on the same filesystem; this is input separation, not hostile-process
isolation. `--json` prints all results; `--work-dir NEW-OR-EMPTY-DIR`
retains them in `truth/`, `cases/`, and `stratified.json`.

The local `20260927` two-trial run evaluated 36 specified cases. Current
counts are emitted by the command because support may change as parsers are
added. Two intentional bit flips to unfiltered, unchecksummed payloads caused **two
historically wrong accepted regions**. Both are explicitly labeled in the
tool's evidence as lacking independent integrity. An index can justify
ownership and position while being unable to prove that the bytes have not
changed since measurement. The passing matrix counts this limitation rather
than hiding or treating it as checksum-backed recovery. Its exact region count
includes 1,089 one-element chunks from one intact generated paged array, so
the aggregate is not an overall success rate. These fixtures and faults were
constructed and are not representative of naturally damaged files. They
cannot support a near-99% claim.

`tests/test_bounded_parser_fuzz.py` and `tests/test_v2_parser_fuzz.py` make
288 seeded mutations inside a fixed-array data block, extensible-array index
block, selected object header, and version-two B-tree leaf, recomputing each
affected metadata checksum. They check that the parser either refuses or
returns bounded, distinct coordinates and physical extents outside metadata.
`tests/test_modern_end_to_end_matrix.py` additionally checks public recovery
for both newer index trees with filters, partial edges, sparse allocations,
nondefault filter order, per-chunk skipped filters, deeper paths, and corrupted
metadata. It rereads every asserted ledger pointer from source bytes. This is
a small deterministic regression
sampler, not a formal fuzz campaign or proof that all corruptions are safe.

## Seeded scientific damage matrix

From the repository root, with h5py and NumPy installed, run:

```sh
python benchmarks/run_seeded_matrix.py --seed 20260927 --trials 2
```

The default run verifies the pinned SHA-256 of all four unchanged scientific
files and executes 23 cases on disposable copies. Ten fault cases per trial
cover a broken or redirected version-one root link, two broken links, a
compressed payload bit flip, combined index and payload faults, a damaged
selected object header, an invalid HDF5 signature, a lost direct payload
pointer, and truncation through a compressed payload. The 4 kHz and 16 kHz
GWOSC files exercise distinct level-zero and level-one index layouts. Three
additional baselines cover an intact direct index and explicit refusal of
the representative quantum and aircraft scientific datasets.

The seed reproducibly selects payload chunk indices and bits, a direct-index
pointer, and the signature bit. `--trials N` selects 1 through 20 distinct
seeded choices per fault class. Cases share the same original files and are
correlated; more trials do not establish a population success percentage.
The unchanged reference is available only to the evaluator's comparison
code, not as a recovery subprocess argument. This is program-input separation,
not adversarial filesystem isolation. Recovery uses the damaged copy alone.

The evaluator compares each accepted chunk's exact float64 bits and coordinate
with the original, checks its claimed source byte range and checksum route,
and counts native reads that fail or return wrong values. The summary keeps
exact accepted chunks, unresolved chunks, false accepted chunks, and safe
refusals separate. A refusal contributes no recovered measurements. If a
wrong accepted value or an unexpected output appears, the command exits
nonzero and still saves the full evaluation as `matrix.json`. An evaluator
regression deliberately alters a claimed measurement after a valid run and
confirms that the false acceptance is detected. `--json` prints the full
record; `--work-dir NEW-OR-EMPTY-DIR` retains the copies at a chosen path.

The default 20260927 run passed 23 chosen cases: 954 exactly verified accepted
chunks, including 226 that native HDF5 could not read correctly from the
damaged copy; zero false accepted chunks; and 14 safe refusals. The aggregate
954 includes the intact baseline and chunks that native HDF5 could already
read, so it is not a recovery percentage. These are scores of controlled
cases on two structurally supported files plus two refusal-only layouts.
They are not naturally damaged files or a representative real-world failure
distribution, so they do not support a 99% success claim.

## Real-data damage catalog

From the repository root, run:

```sh
python benchmarks/run_damage_catalog.py
```

The command verifies the sizes and SHA-256 hashes of all four bundled,
unchanged scientific files, then runs ten documented cases on disposable
copies. On the 16 kHz GWOSC strain dataset, it tests one missing index link,
one corrupted compressed payload, both together, two missing links, and a
damaged selected object header. On the 4 kHz GWOSC strain dataset it checks
exact export from an intact level-zero root, marks one corrupted payload
unavailable, and safely refuses to guess a missing direct payload pointer.
The two other authentic scientific layouts exercise safe refusal. It calls
the public recovery command with the trial copy alone, checks accepted values
bit for bit at their original coordinates, checks missing-region labels, and
verifies originals and trial inputs were preserved. The original file is
accessible only to the evaluator for scoring; it is not an input to the
recovery subprocess.

The terminal prints a readable ten-case summary. `--json` prints the full
evaluation, and a `catalog.json` is kept in the displayed work directory in
either mode. Use `--work-dir path/to/new-or-empty-directory` to keep trial
files at a chosen location. A refusal is a correct result for cases without
sufficient supported evidence. The intact 4 kHz case verifies value export,
not repair of a damaged index. The fault classes and positions are fixed and
documented; they are neither a random sample of real failures nor a measure
of a 99% recovery rate. The two non-GWOSC cases test safe refusal, not
recovery of their measurements.

## Authentic GWOSC file, controlled damage

The repository includes the untouched 16 kHz Hanford strain file from GWOSC.
From the repository root, with Python, h5py, and NumPy available, run:

```sh
python benchmarks/run_gwosc_recovery.py
```

This one command verifies the original's SHA-256, checks all 128 raw B-tree
chunk records against h5py, creates a separate damaged copy, and changes one
verified interior root child pointer in that copy. It checks that ordinary
HDF5 reads fail or differ for the affected chunks and that unaffected chunks
remain exact. It invokes recovery in a subprocess with the damaged input,
dataset path, and new output paths. The subprocess receives neither the
pristine reference nor the mutation manifest. The evaluator then compares
every output `float64` bit pattern at the corresponding sample
coordinate against the untouched original. It checks output status, evidence
mappings, Fletcher32 verification labels, selected scalar attributes, embedded
and external reports, and both file hashes. The default console output is a
readable pass summary with native-reader failures, recovered and reconstructed
chunks, exact float64 bit matches, and the trial file paths. Add `--json` for
the complete evaluation summary on standard output. Either way, trial files
and JSON evidence are left in the displayed work directory. Use `--work-dir
path/to/new-dir` to choose a new or empty location; the default temporary
directory is retained.

The retained directory contains `inputs/damaged.hdf5`,
`results/recovered.hdf5`, `results/recovery.json`,
`truth/mutation.json`, and `truth/evaluation.json`. The recovered HDF5 file
includes `/_h5reclaim/chunk_status` and an embedded copy of the recovery
report. The independent evaluator's `truth/evaluation.json` records the
scored counts and source hash checks. The `results/recovery.json` file records
the recovery tool's per-chunk provenance. The original research file remains
in `corpus/files/`; the evaluator reads it only as the reference.

The recorded trial had 57 native-unavailable or incorrect chunks. H5Reclaim
reconstructed those 57 and exported all 128 chunks with zero wrong bits. The
scientific file's bytes are authentic; the pointer damage is controlled for
evaluation. This says nothing about unrelated layouts, organically damaged
files, or repairs to the original file. See [corpus sources](../corpus/README.md).

For a quick metadata coverage check across all four original files, run
`python benchmarks/run_real_corpus.py`. Its readable output summarizes the
verified originals and support classifications; `--json` returns every
machine-readable result. This corpus survey does not damage a copy, read
measurements, or attempt recovery. `python -m unittest discover -s tests -q`
runs Python's discovered tests in quiet mode; success does not independently
score a naturally damaged user file.

The GWOSC file has one interior leaf position that can be detached while
leaving two adjacent anchors. `tests/test_mutation_positions.py` separately
damages all four eligible positions in another synthetic layout with random
measurement values, passing only each damaged copy to a public recovery
subprocess. The fixture tool's optional `--child-index N` selects a verified
position; its default remains the first eligible position. This expands
position coverage for one break type, not arbitrary corruption coverage.

## Synthetic random-value trial

Run from the repository root in an environment with h5py and NumPy:

```sh
python benchmarks/run_recovery.py --work-dir work/trial
```

The default terminal output is a readable result with key counts and file
locations. Add `--json` to print the full evaluation, which is always saved
in `truth/evaluation.json`.

The work directory must be new or empty. `truth/pristine.h5` contains the
reference, `truth/challenge.json` records a random value seed, and
`truth/mutation.json` records the controlled change. `inputs/damaged.h5` is
the only source passed to the separate recovery process. Outputs are under
`results/`, and `truth/evaluation.json` holds the evaluation. Use `--seed N`
to reproduce the challenge values. The fixture creator first validates a
healthy round trip; the benchmark then replaces payloads with challenge values
known only to evaluation and checks the round trip again.

The recovery subprocess is not passed the pristine path, mutation manifest,
challenge seed, or expected chunk offsets. The `truth/`, `inputs/`, and
`results/` directories nevertheless share a parent and are accessible to a
program running with the same filesystem permissions. This is separation of
program inputs and responsibilities, not an adversarial filesystem isolation
boundary. The current recovery code does not read the truth directory.

The evaluator compares each chunk marked `recovered` with the pristine values
at the **same coordinate**. A mismatch that uniquely matches a different
reference coordinate counts as wrong placement; other mismatches count as
incorrect values. It counts chunks marked with an uncertainty status separately.
It also measures which chunks a normal
h5py read of the damaged source fails to return correctly and how many of
those H5Reclaim recovers. SHA-256 checks establish that both source and
reference are unchanged after recovery. The report's `complete` field means
all chunks have recovered status; `partial` is a valid completed attempt with
unresolved regions. The process exits nonzero if placement or values are wrong, no native
read was impaired, or the recovery has no demonstrated gain. The end-to-end
test additionally requires full exact recovery for this narrow fixture.

The evaluator never imports the recovery package. It invokes its public CLI
with a damaged path and dataset path. For evaluation against a different
implementation that might inspect neighboring files, give that process access
only to a separate copy of the damaged input, then score its results from the
evaluator's private reference.
