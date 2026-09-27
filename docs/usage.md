# Run H5Reclaim

H5Reclaim's single-file structural routes work from the damaged HDF5 file without a healthy original. Optional replica or parity recovery needs independent evidence captured before damage. The bundled originals are used to make disposable damage copies and independently score controlled experiments. An output file holds one selected dataset and a validity map, not a repair of the source container or proof of historical values.

## Install this checkout

Use Python 3.10 or newer in the extracted repository root. On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m h5reclaim --help
```

On Linux or macOS, activate with `. .venv/bin/activate`. `pip install -e .` installs this local checkout, not a PyPI copy of H5Reclaim. Pip may fetch NumPy, h5py, and build tools if they are absent. The installed `h5reclaim` and `python -m h5reclaim` commands are equivalent.

## Guided rescue

Select one dataset from the damaged file and choose new destinations:

```powershell
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --output rescued.h5 --report evidence.json
```

Without additional files, `rescue` first attempts rooted chunked structural recovery. If its schema is unsupported, it tries rooted compact/contiguous numeric recovery; if that cannot justify output, it tries the separate bounded native-readable copy route. An actual structural contradiction is never silently reinterpreted as another layout. The printed route and JSON report distinguish recovery from copying values HDF5 could already read. The original is read-only; output and report must be new paths. Examine `/_h5reclaim/chunk_status`, `/_h5reclaim/element_status`, or `/_h5reclaim/validity`, whichever the report names. Fill at an unknown position is not a measurement. Explicit `rescue` options below choose separate routes and cannot be combined with one another.

The compact/contiguous structural route can retain fully present elements at their justified offsets after physical tail truncation, and leaves incomplete elements unknown. It currently supports a rooted local numeric dataset of rank zero through four, canonical fixed-width integers or IEEE floats, and selected layout messages v3–v5. It does not restore missing bytes or claim historical authenticity for unchecksummed values. The readable fallback preserves bounded fixed-size local schema when native HDF5 can already read it; its report says `readable_export`.

### Salvage complete chunks before a physical tail cut

When a chunked file was physically shortened, select the tail route explicitly:

```powershell
python -m h5reclaim rescue shortened.h5 --dataset /experiment/readings --truncated-chunks --output partial.h5 --report tail-evidence.json
```

The file's declared EOF must exceed its physical length by at most 256 MiB.
The rooted selected path, schema, and complete older or newer chunk index must
fit physically inside the shortened source. The route extends only a private
snapshot for parsing. Every accepted stored chunk must end before the actual
EOF; cut chunks are marked unavailable in `/_h5reclaim/chunk_status`, and
unallocated positions stay unknown. The derived dataset's zero fill at those
positions is not recovered science. Missing metadata or index bytes, an
overlap with a known sibling owner, contradictory structure, or a larger tail
cut causes refusal. The ordinary source-size, chunk and grid bounds apply.

### Trial one checked metadata field

If the observed fault is a single byte in a modern superblock root pointer,
a selected modern object header's chunk-index pointer, or a selected chunk
dimension in that header, try one declared kind on a disposable private copy:

```powershell
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --metadata-trial root --output root-trial.h5 --report root-trial.json
python -m h5reclaim rescue damaged.h5 --dataset /readings --metadata-trial layout --output layout-trial.h5 --report layout-trial.json
python -m h5reclaim rescue damaged.h5 --dataset /readings --metadata-trial dimension --output dimension-trial.h5 --report dimension-trial.json
```

`root` requires a modern version-2/3 superblock whose other fields are
valid. `layout` requires a valid superblock, a direct local hard link under
a checksummed compact root, and a selected checksummed first-chunk v2 header
with a supported modern index-pointer field. `dimension` has the same root
and first-header requirements and tries one byte among the selected dataset's
chunk extents in a v4/v5 layout with a fixed-array, extensible-array, or
version-2 B-tree index. It excludes the encoded datatype-size field and
header continuations. The command searches only the declared field, keeps
the **original** checksum, rejects multiple checksum-matching substitutions,
and validates native schema, complete index, rooted ownership and a
native-readable selected dataset. The input limit is 512 MiB. It publishes
only a new selected dataset and evidence report. A checksum match does not
establish the historical value of the measurements or make the corrected
trial copy suitable for continued acquisition.

Structural routes refuse a selected physical range when another observed rooted local dataset claims those bytes. Reports state when bounded sibling enumeration was incomplete; a complete native inventory is still not a historical ownership certificate. A manifest rejects duplicate keys and a related raw file that aliases the main HDF5 container.
Native-readable, VDS source, Family, and Split value exports also reject observed competing sibling allocations; they refuse if their bounded native sibling inventory cannot complete.

### Inspect an interrupted-write status flag

If diagnosis shows a version-3 write flag, the explicit status trial can check
the original superblock checksum and end-of-address, change only the flag and
checksum in a **disposable private copy**, and attempt a bounded native-readable
export of the selected dataset:

```powershell
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --status-trial --output trial-values.h5 --report trial-evidence.json
```

It refuses a bad original checksum, reserved status bits, or end-of-address
past physical EOF. The published file is a derived selected dataset with a
validity map, not a status-cleared copy of the original container. A successful
native read does not show that an interrupted write finished correctly or
that its historical measurements are intact. `probe-status` below is a
separate optional experiment using `h5clear` when installed.

### Supply related files

For external raw storage or a virtual dataset, use the exact-name, pinned related-file manifest described below:

```powershell
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --related-files related.json --output rescued.h5 --report evidence.json
```

The external raw route maps complete elements through ordered, explicitly supplied raw segments; missing bytes and the portion past physical EOF are unknown. The VDS route maps finite ALL/regular selections from pinned, local, native-readable numeric source datasets and marks missing or unallocated sources unknown instead of accepting virtual fill. It refuses overlapping mappings, dynamic source names, transitive dependencies, and unsupported selections. One selected **external link** can also name a pinned HDF5 target. The route traverses only local hard links inside that target, validates its selected native-readable dataset and materializes the values locally. It does not follow target-side soft/external links, recursive or transitive dependencies. Its evidence ranges refer to the target file, not the referring HDF5 container. These routes produce local selected datasets rather than preserving external or virtual storage configuration. Native HDF5 operations in guided rescue run in a deadline-bound child with plugin loading disabled. See [dependency bundles](dependency-bundles.md).

### Use an independently captured baseline and replicas

If a dataset is still intact, record decoded chunk hashes and keep the JSON independently from the acquisition:

```powershell
python -m h5reclaim capture-baseline healthy.h5 --dataset /experiment/readings --output baseline.json
$priorChunkSha = (Get-FileHash .\baseline.json -Algorithm SHA256).Hash.ToLowerInvariant()
```

Retain `$priorChunkSha` independently from the ZIP or file containing the
baseline. Every selected chunk must be allocated and structurally decodable.
The JSON records bytes **observed when this command ran**, not evidence that
earlier readings were correct. It cannot be created retrospectively from a
lost healthy file.

For a later file whose selected chunked dataset still has rooted, parseable
metadata, compare every currently decoded chunk with that prior JSON before
publishing it:

```powershell
$priorChunkSha = '<paste the lowercase SHA-256 retained before damage>'
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --chunk-baseline baseline.json --chunk-baseline-sha256 $priorChunkSha --output verified-chunks.h5 --report chunk-evidence.json
```

The JSON and its SHA-256 must have been retained independently before the
damage. An unchanged chunk may be accepted; changed, missing, undecodable,
or unowned chunks remain unknown in `/_h5reclaim/chunk_status`. A digest does
not supply replacement bytes. The path and lowercase digest are required as
a pair. The separate replica and parity routes below can use an independent
copy or prospective sidecar for replacement under their own limits.

For a complete rooted **compact or contiguous** canonical numeric dataset,
capture per-element hashes while it is still intact:

```powershell
python -m h5reclaim capture-element-baseline healthy.h5 --dataset /experiment/readings --output element-baseline.zip
```

Keep the printed ZIP SHA-256 independently. Later, on a damaged file with
surviving selected schema and physical offsets:

```powershell
$priorElementSha = '<paste the lowercase SHA-256 retained before damage>'
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --element-baseline element-baseline.zip --element-baseline-sha256 $priorElementSha --output verified-elements.h5 --report element-evidence.json
```

Only complete stored elements with matching prior hashes are accepted in
`/_h5reclaim/element_status`. Changed or truncated elements are unknown, even
where the output dataset displays zero. This route does not reconstruct the
old bits. It requires a matching selected shape, type and layout; use a new
destination and keep the original and baseline separate.

An optional later replica manifest has this shape, with actual hashes and absolute paths:

```json
{
  "schema_version": 1,
  "damaged_sha256": "<SHA-256 of the current damaged HDF5 file>",
  "baseline": {"path": "C:\\Lab\\baseline.json", "sha256": "<SHA-256 of the separate baseline JSON>"},
  "replicas": [{"path": "C:\\Lab\\other-copy.h5", "sha256": "<SHA-256 of the supplied copy>"}]
}
```

```powershell
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --replicas replicas.json --output rescued.h5 --report evidence.json
```

Each copy must independently provide a rooted, matching dataset and justified chunk coordinates. A chunk is accepted only if its decoded bytes match the prior baseline; conflicting replicas remain ambiguous. This route cannot use an arbitrary raw fragment as a substitute for an indexed copy. The operator must establish the baseline's provenance.

Alternatively, while the complete acquisition and its prior baseline both still exist, capture a separate XOR parity ZIP:

```powershell
python -m h5reclaim capture-parity healthy.h5 --dataset /experiment/readings --baseline baseline.json --stripe-width 4 --output parity.zip
```

Store the baseline and parity sidecar independently. If future damage affects one chunk in a stripe, the other chunks still match their prior hashes, and the reconstructed bytes match the missing chunk's prior hash, `rescue` can restore that chunk without a full healthy copy. Supply a manifest with `schema_version: 1`, `damaged_sha256`, `baseline: {path, sha256}`, and `parity: {path, sha256}`, using absolute paths:

```powershell
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --parity parity_manifest.json --output rescued.h5 --report evidence.json
```

Two damaged chunks in the same stripe remain unknown. Parity also needs surviving selected metadata and coordinate ownership; it cannot repair arbitrary index loss. The capture, baseline, and sidecar must be independently trustworthy.

### Capture the selected schema and physical map before damage

For a complete, locally stored chunked dataset that is still intact, an
independent capsule records exact schema, allocated chunk offsets, stored
raw and decoded hashes, and per-block hashes for unfiltered chunks. It does
**not** contain a copy of the measurement bytes:

```powershell
python -m h5reclaim capture-capsule healthy.h5 --dataset /experiment/readings --output capsule.zip
```

Keep both `capsule.zip` and its printed SHA-256 independently of the HDF5
acquisition. In a future incident, supply the damaged file, selected path,
and the **pre-incident** digest:

```powershell
$priorCapsuleSha = '<paste the lowercase SHA-256 retained before damage>'
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --capsule capsule.zip --capsule-sha256 $priorCapsuleSha --output capsule-values.h5 --report capsule-evidence.json
```

The route reads raw bytes at the captured offsets even when the damaged file
cannot open its root, selected header, or chunk index. It checks each
complete stored chunk against the prior raw hash. For unfiltered chunks,
matching smaller blocks can be accepted as complete elements while altered
blocks are unknown. If the damaged dataset is still rooted, its exact HDF5
datatype encoding must match the capsule, including enum names that an equal
NumPy storage dtype would miss. The capsule's output preserves same-named
scientific attributes when a tool convenience annotation would collide; the
report lists omitted annotations and `/_h5reclaim/` holds the authoritative
validity map. Filtered chunks need the entire stored stream to match;
the capsule has no replacement payload. Check
`/_h5reclaim/chunk_status` and, where the report provides it,
`/_h5reclaim/element_status`. A capsule for another acquisition, one captured
after damage, a tampered archive, moved chunk bytes, or overwritten unique
bytes cannot justify exact historical recovery. The current route limits
selected logical data to 512 MiB, the grid to 8,192 chunks, and the capsule
archive to 48 MiB.

### Retain multiple parity shards before damage

For an intact, complete, supported **primitive numeric** chunked dataset,
first capture the coordinate baseline above. Then make two to four GF(256)
parity shards for each stripe of two to sixteen chunks:

```powershell
python -m h5reclaim capture-erasure healthy.h5 --dataset /experiment/readings --baseline baseline.json --stripe-width 8 --parity-shards 3 --output erasure.zip
```

Keep the baseline, parity archive, and their separate SHA-256 digests before
an incident. A recovery manifest pins the current damaged file and both
sidecars:

```json
{
  "schema_version": 1,
  "damaged_sha256": "<SHA-256 of the current damaged HDF5 file>",
  "baseline": {"path": "C:\\Lab\\baseline.json", "sha256": "<pre-incident SHA-256 of baseline>"},
  "erasure": {"path": "C:\\Lab\\erasure.zip", "sha256": "<pre-incident SHA-256 of erasure ZIP>"}
}
```

```powershell
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --erasure erasure_manifest.json --output parity-values.h5 --report parity-evidence.json
```

At most the number of retained parity shards can replace unknown chunks in
each stripe. Every accepted survivor and reconstructed chunk must match its
prior coordinate hash. This needs a surviving selected schema and rooted
index; the separate capsule can bypass lost metadata under its own limits.
Nominal chunks are capped at 1 MiB and the parity archive at 256 MiB. A
hash captured after an incident or a stale sidecar is not prior evidence.

### Open a Family driver bundle

An HDF5 Family file spans numbered physical members. A separate manifest gives its actual member size and every member in order:

```json
{
  "schema_version": 1,
  "member_size": 1048576,
  "members": [
    {"index": 0, "path": "C:\\Lab\\run000.h5", "sha256": "<actual SHA-256>"},
    {"index": 1, "path": "C:\\Lab\\run001.h5", "sha256": "<actual SHA-256>"}
  ]
}
```

```powershell
python -m h5reclaim rescue C:\Lab\run000.h5 --dataset /experiment/readings --family-members family.json --output rescued.h5 --report evidence.json
```

The source argument must be member zero. This route performs a bounded **native-readable export** of the pinned Family address space with physical member provenance and sparse validity; it does not reconstruct broken driver metadata. Missing or truncated member bytes are refused. The manifest must reflect the producer's member size, not a guessed value.
Distinct member indices must refer to distinct physical files, including across hard links.

For a two-member HDF5 Split driver, use `--split-members` with the metadata member as the source argument. The manifest lists exactly `schema_version: 1`, `driver: "split"`, and `members` in metadata then raw order, each with `role`, absolute `path`, and lowercase `sha256`. For example:

```powershell
python -m h5reclaim rescue C:\Lab\run-m.h5 --dataset /experiment/readings --split-members split.json --output rescued.h5 --report evidence.json
```

The Split route checks the stored two-member virtual address map against the pinned files and physically present raw byte ranges. It exports current native-readable values with sparse validity. Generic Multi configurations, missing members, and contradicting driver metadata refuse. It cannot reconstruct overwritten measurements or an arbitrary broken Multi address map.
The metadata and raw manifest entries must refer to different physical files.

## Run on supplied scientific data

Four unchanged attributed HDF5 files and pinned hashes are bundled in `corpus/`. No fixture generation or data download is required:

```powershell
python benchmarks/run_real_corpus.py
python benchmarks/run_gwosc_recovery.py
python benchmarks/run_damage_catalog.py
python benchmarks/run_seeded_matrix.py --seed 20260927 --trials 2
python benchmarks/run_stratified_layouts.py --seed 11235813 --trials 1
python benchmarks/run_real_candidate_exports.py
python benchmarks/run_real_readable_corpus.py
python benchmarks/run_authentic_baseline_integrity.py
python benchmarks/run_authentic_v09_routes.py
python -m unittest discover -s tests -q
```

| Command | Operation |
| --- | --- |
| `run_real_corpus.py` | Verifies four original hashes and inventories 251 local datasets without reading values or damaging files. |
| `run_gwosc_recovery.py` | Breaks one verified index pointer in a disposable GWOSC copy, runs the public recovery CLI on the damaged copy alone, and independently scores output against the untouched original. |
| `run_damage_catalog.py` | Checks ten selected structural, payload, metadata, and refusal cases on copies. |
| `run_seeded_matrix.py` | Varies fault positions using a published seed, separately counts exact accepted chunks, wrong accepted values, unknown regions, and safe refusals. The default has 23 correlated cases. |
| `run_stratified_layouts.py` | Exercises generated modern index families, filters, sparse and edge chunks, deliberate false acceptance with absent checksums, and the authentic corpus. The truth file is used only by the evaluator. |
| `run_real_candidate_exports.py` | Tests all six currently eligible GWOSC datasets on intact copies through the structural route; bitwise and physical-range scores are independent. This is intact export, not damaged repair. |
| `run_real_readable_corpus.py` | Tests native-readable export of two structurally unsupported Zenodo originals using independent current-value comparisons. |
| `run_authentic_baseline_integrity.py` | Captures prior hashes from two intact authentic files, changes a payload byte in each disposable copy, then verifies damaged-only rescue with those hashes and refuses tampered sidecars. The original is used by the evaluator to score exactness. |
| `run_authentic_v09_routes.py` | Scores three prospective capsule metadata faults, two same-stripe erasures, and a tail truncation on a pinned authentic GWOSC source. The public commands receive only damaged copies and the previously captured sidecars. It compares every accepted value bitwise against evaluator-only truth and counts unknown regions. |
| `unittest discover` | Runs parser, output, negative, resource, and end-to-end tests. It does not recover a supplied user file. |

Benchmark readable summaries include paths to retained HDF5 and JSON evidence. Use `--json` for complete output where offered. The original healthy file and mutation manifest are evaluator truth, never arguments to recovery. These constructed experiments cannot measure a real-world success percentage.

For a **separately supplied, previously unused** hash-pinned panel, see the
manifest contract in [the benchmark guide](../benchmarks/README.md) and run:

```powershell
python benchmarks/run_heldout_trials.py --manifest C:\Lab\panel.json --work-dir C:\Lab\heldout-evaluation
```

The evaluator records planned, eligible and excluded cases by fault family,
exact/wrong accepted values, unknowns, refusals and protocol failures. It
uses intact originals only for controlled mutation and evaluator-only truth;
the recovery subprocess receives a damaged copy. Exit 1 flags a false accept
or protocol failure, including a silent unchecksummed bit change that passes
without prospective prior evidence. Self-declared provenance and a chosen
fault mix do not yield a general field success percentage.

## Diagnose a damaged file

```powershell
python -m h5reclaim diagnose damaged.h5 --dataset /experiment/readings
python -m h5reclaim survey damaged.h5
python -m h5reclaim inspect damaged.h5 --dataset /experiment/readings
```

`diagnose` reads a stable private copy, reports a recognized signature, raw status/EOA observations, an inventory if metadata opens, declared external dependencies, and a next action. It reads no values. `survey` inventories reachable local hard-linked datasets and their support reasons; a candidate is not a successful recovery. `inspect` checks the selected supported index and chunks without publishing output. Use `--json` for all structured details. An unsupported schema is never converted to a familiar one.

For an observed version-3 write flag with an end-of-address within the physical file, an optional metadata-only trial is:

```powershell
python -m h5reclaim probe-status damaged.h5
```

If the separate `h5clear` utility is installed, this runs `h5clear --status` **only on a disposable second copy**. It publishes no repaired file and does not validate measurements. A raw status flag is not proof of stale status. HDF5's utility is not a general corruption repair tool.

### Related files

For an external raw segment, virtual source, or external link, diagnosis reports declared names without following them. Provide independently known absolute paths and SHA-256 values:

```json
{
  "schema_version": 1,
  "files": [
    {
      "declared_name": "run.raw",
      "path": "C:\\science\\run.raw",
      "sha256": "<64 lowercase hex digits of that file>"
    }
  ]
}
```

```powershell
python -m h5reclaim diagnose damaged.h5 --dataset /measurements --related-files related.json
```

The **diagnosis validator** matches declared names exactly, hashes supplied files, checks fixed external byte ranges, and can inspect local target-object metadata. Diagnosis does not read values. The separate `rescue --related-files` route can export bounded, justified external raw or finite VDS values, including partial external byte ranges. It never infers paths, follows transitive dependencies, or treats a missing source's fill as a measurement. A matching hash pins supplied current bytes, not historical correctness. See [dependency bundles](dependency-bundles.md).

### Optional scientist hints

`--hints hints.json` works with `diagnose`, `inspect`, `recover`, and `export-readable`:

```json
{"schema_version": 1, "dataset": {"path": "/experiment/readings", "shape": [512, 512], "chunks": [16, 16], "dtype": "<u4", "filters": []}}
```

An optional `source_sha256` refers to the **damaged input**. Observed conflicts stop export. Matching hints are operator assertions, never evidence that a detached range belongs at a coordinate. The export commands can take the selected path from hints if `--dataset` is omitted.

## Structurally recover a selected dataset

```powershell
python -m h5reclaim recover damaged.h5 --dataset /experiment/readings --output result.h5 --report result.json
```

The destinations must not exist. Recovery reads only the damaged input through a private snapshot. Native HDF5 normally locates the selected local dataset and schema; when native metadata lookup fails, a narrower rooted parser can resolve surviving old symbol-table or modern compact/dense-group links and required messages. It refuses unsupported paths and records this route in `metadata_resolution`. A bounded independent parser validates raw indexes and decodes chunks. Version-1 trees can bridge one interior lost **leaf or internal subtree** pointer only through an exact parent interval, two rooted reciprocal sibling links and a complete, disjoint candidate subtree. Modern damage repair applies only to documented, bounded pointer positions when the unique substitution restores an original metadata checksum and the child and full traversal agree; see the [damage taxonomy](damage-taxonomy.md) for the implemented links. Other broken links remain unsupported. The report's `evidence_ledger` records index chains, physical ranges, checksums or their absence, decoding checks, contradictions, and per-region decisions.

| Structural condition | Supported behavior |
| --- | --- |
| Dataset | One local rank-one through rank-four chunked dataset with canonical integer (8/16/32/64 bit signed or unsigned), IEEE float32/64, or a bounded self-contained fixed-size compound, enum, array, fixed string or opaque HDF5 type. Exact encoded file type is retained for the fixed records. Positive current dimensions, partial edge chunks, sparse allocation and growing maxima are supported when the index parser validates their mapping. Variable-length and reference members refuse. |
| Older format | Superblock v0/v1, object header v1 with up to eight supported continuations, layout v3, version-1 B-tree with intact level-zero or deeper tree. One interior missing leaf link or one interior root-to-internal subtree link can be bridged if two rooted reciprocal siblings, exact parent interval, and complete disjoint candidate traversal agree. Boundary gaps, a second broken descendant link and ambiguous candidates refuse. Older rooted fallback covers bounded canonical rank-one through four numeric and supported shuffle/DEFLATE/Fletcher32 layouts; old links themselves are unchecksummed. |
| Newer format | Checksummed superblock v2/v3, checksummed selected object header v2 with bounded continuations, layout v4/v5 and intact single-chunk, implicit, filtered/paged/sparse fixed array, bounded extensible array including validated initialized pages, or version-2 B-tree. One FAHD-to-FADB, EAHD-to-EAIB, EAIB-to-EADB/EASB, EASB-to-EADB, BTHD-to-root or BTIN-to-child link can be tried when one pointer replacement restores the original parent checksum, a checked child and full traversal agree, and the 512 MiB scan and ownership bounds hold. Other broken modern links remain unsupported. Raw metadata fallback follows rooted compact links or a bounded checksummed dense-group name index and managed fractal heap; unsupported variants refuse. |
| Selected shared messages | A committed canonical numeric datatype can resolve through a checked v2 object header. Bounded SOHM single-list or type-7 v2 B-tree leaf/one-internal-level managed-heap references can resolve selected shared dataspace, datatype or filter messages. Deeper and unsupported heap/index variants refuse. |
| Filters | Bounded built-in shuffle, DEFLATE, and Fletcher32 in their actual declared order; per-chunk optional skip masks. Missing unknown decoders are reported separately, never treated as verified measurements. |
| Resource bounds | Source snapshot at most 4 GiB, 30-minute 1 MiB-buffer copy, temporary disk for full logical source size plus 32 MiB reserve; structural dataset at most 1,048,576 elements, 8,192 chunks and 4,096 traversed nodes, 1 MiB decoded chunk. |

The structural decoder enforces a strict expansion bound and exact final nominal chunk length. Invalid masks, unknown active filters, missing metadata anchors, overlapping physical extents, invalid checksums, or destroyed payloads cannot be promoted to measurements. An unknown active filter with an otherwise anchored chunk is status 7. A missing index slot remains allocation unknown; output fill is never proof of a measurement.

The output's `/_h5reclaim/chunk_status` is indexed by chunk grid:

| Code | Meaning |
| --- | --- |
| 1 `recovered` | The coordinate and payload passed the documented structural checks. Unfiltered payload bytes still lack an independent checksum. |
| 2 `allocation_unknown` | No accepted assignment; output fill is unknown, not a measurement. |
| 6 `decode_failed` | An anchored chunk failed decoding or its checksum; output fill is unknown. |
| 7 `decoder_unavailable` | An anchored chunk needs an unsupported active filter decoder; output fill is unknown. |

Codes 3 `ambiguous`, 4 `unavailable`, and 5 `unsupported` are reserved in this output. Check the status map even if the HDF5 output opens. `complete` means all grid chunks have status 1, not that original science was independently authenticated. The report is external JSON and embedded at `/_h5reclaim/report_json`. Only the selected dataset and bounded safe attributes are copied; siblings, links, scales, and full scientific context are excluded.

If a copied scientific attribute has a name also used for a convenience tool
annotation, the source attribute retains its value. The report's
`selected_annotation_collisions` lists any skipped annotations. Read the
authoritative validity and provenance under `/_h5reclaim/`; do not infer
status from a dataset attribute that may belong to the scientist. Some
routes cannot copy the source attributes and report that omission.

An anchored decode-failed raw extent may be exported separately as a coordinate-free forensic fragment:

```powershell
python -m h5reclaim export-fragments result.json damaged.h5 --output fragments.zip
```

The source is explicit and its hash plus every fragment hash are verified before publication. A missing link with no justified physical extent yields no fragment.

## Copy current native-readable values

```powershell
python -m h5reclaim export-readable damaged.h5 --dataset /experiment/readings --output readable.h5 --report readable.json
```

This distinct route needs native HDF5 to read the selected local dataset. Bounded compact, contiguous, and chunked layouts of rank one through four can include canonical fixed-width numeric, boolean/enum, complex, fixed strings/opaque bytes, and fixed-size compound/array fields. The selected HDF5 datatype, shape/maxshape, fill rules, chunking, and built-in filter order are copied. Native output readback checks bytes. Sparse chunked input is `partial`, with `/_h5reclaim/validity`: 1 accepted current value, 0 unknown element whose output fill must be ignored. The report records physical extents, masks, and raw hashes when available.

For **structural** chunked recovery, bounded self-contained fixed-size
compound records, fixed strings, enums, arrays and opaque values can retain
their encoded HDF5 file type and exact raw record bytes. The recursive type
check limits nesting to four, compound and enum members to 64, and a record
to 1 MiB; selected chunks remain bounded by the ordinary decoder and grid
quotas. Nested reference or variable-length/heap-backed members are refused.
The broad native-readable route above and the structural route provide
different evidence; check the report `mode` and per-region integrity.

Logical data is limited to 512 MiB in 1 MiB blocks, with 2 MiB stored chunks and at most 8,192 grid entries. Supported compiled-in filters include DEFLATE, shuffle, Fletcher32, NBIT, SCALEOFFSET and SZIP when the linked HDF5 library provides both SZIP directions. SCALEOFFSET can be lossy at acquisition. Bounded top-level object and region references to the selected dataset are remapped and checked by logical target and selection. Outside or dangling references, nested reference graphs, variable-length heap values, plugin filters, and arbitrary bit representations are refused. External/VDS values use the separate pinned-bundle routes above. Native reads run in a child process with a 900-second deadline and disabled dynamic filter plugin loading. POSIX applies a 3 GiB address-space cap and disables core dumps; Windows has the deadline but no enforced worker memory cap. This is process isolation for crashes and resource bounds, not a security sandbox for hostile native code. This route cannot repair an inaccessible index or verify values before damage.

For a larger, **currently native-readable** one-dimensional primitive numeric
dataset, use the separate streaming route:

```powershell
python -m h5reclaim rescue large.h5 --dataset /experiment/readings --large-readable --output large-values.h5 --report large-evidence.json
```

This route accepts chunked or contiguous storage, streams through bounded
blocks, keeps sparse positions unknown, and stores physical evidence and
validity arrays inside the output HDF5 file. The report points to those
datasets instead of embedding a huge per-chunk list. Default limits include
64 GiB physical source, 8 GiB copied snapshot data, 16 GiB logical data,
18 GiB output, 65,536 allocated chunks, a 1,048,576-position grid,
8 MiB decoded chunks, and a deadline. Disk preflight and available space
still determine whether a particular file can run. The source may be larger
than 4 GiB, but no lost index, old value, compound record, other rank, or
nonlocal dependency is reconstructed by this route.

For one large file with a damaged, original-checksummed **fixed-array header
to data-block pointer**, choose the narrower structural streaming route:

```powershell
python -m h5reclaim rescue damaged-large.h5 --dataset /experiment/readings --large-structural --output partial-large.h5 --report large-structural.json
```

This route needs an otherwise rooted, readable selected schema, one-dimensional
unfiltered primitive numeric chunks, a unique pointer candidate restoring
the original FAHD checksum, checked FADB back-pointer and checksum, validated
initialized pages, and consistent chunk slots. It reads raw chunks from their
checked physical ranges, even if native HDF5 cannot read through the damaged
index. Sparse or unallocated slots remain unknown in
`/_h5reclaim/validity`; checked physical ranges and raw hashes are in
`/_h5reclaim/physical_evidence`. It does not reconstruct other index links or
authenticate the pre-damage value of unchecksummed measurements. The route
accepts at most 64 GiB physical source and 8 GiB copied snapshot data,
65,536 grid/allocated chunks, 1,048,576 selected elements and 1 MiB nominal
chunks, subject to disk, output and deadline quotas. On a filesystem with
sparse extent enumeration, candidate search is capped at 512 MiB of
allocated extents; where it is unavailable, a complete dense search is
capped at 8 GiB. A dense file may need enough space for a full copy or
refuse the copied-byte limit. Use `--large-readable` only for current values that
native HDF5 can already read; the reports name which route produced output.

## Interpreting results

Exact restoration is impossible if the only original measurement bytes were overwritten and no independent copy or redundancy survives. Bytes can also survive while their coordinate or dataset ownership is unknowable. See [current status](status.md), [evidence model](evidence-model.md), and [generalization limits](generalization-plan.md) for the measured evidence and remaining cases.
