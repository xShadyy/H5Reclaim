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

Without additional files, `rescue` first attempts rooted chunked structural recovery. If its schema is unsupported, it tries rooted compact/contiguous numeric recovery; if that cannot justify output, it tries the separate bounded native-readable copy route. An actual structural contradiction is never silently reinterpreted as another layout. The printed route and JSON report distinguish recovery from copying values HDF5 could already read. The original is read-only; output and report must be new paths. Examine `/_h5reclaim/chunk_status`, `/_h5reclaim/element_status`, or `/_h5reclaim/validity`, whichever the report names. Fill at an unknown position is not a measurement.

The compact/contiguous structural route can retain fully present elements at their justified offsets after physical tail truncation, and leaves incomplete elements unknown. It currently supports a rooted local numeric dataset of rank zero through four, canonical fixed-width integers or IEEE floats, and selected layout messages v3–v5. It does not restore missing bytes or claim historical authenticity for unchecksummed values. The readable fallback preserves bounded fixed-size local schema when native HDF5 can already read it; its report says `readable_export`.

Structural routes refuse a selected physical range when another observed rooted local dataset claims those bytes. Reports state when bounded sibling enumeration was incomplete; a complete native inventory is still not a historical ownership certificate. A manifest rejects duplicate keys and a related raw file that aliases the main HDF5 container.
Native-readable, VDS source, Family, and Split value exports also reject observed competing sibling allocations; they refuse if their bounded native sibling inventory cannot complete.

### Supply related files

For external raw storage or a virtual dataset, use the exact-name, pinned related-file manifest described below:

```powershell
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --related-files related.json --output rescued.h5 --report evidence.json
```

The external route maps complete elements through ordered, explicitly supplied raw segments; missing bytes and the portion past physical EOF are unknown. The VDS route maps finite ALL/regular selections from pinned, local, native-readable numeric source datasets and marks missing or unallocated sources unknown instead of accepting virtual fill. It refuses overlapping mappings, dynamic source names, transitive dependencies, and unsupported selections. Both materialize a local dataset rather than pretending to preserve external or virtual storage configuration. Native HDF5 operations in guided rescue run in a deadline-bound child with plugin loading disabled. See [dependency bundles](dependency-bundles.md).

### Use an independently captured baseline and replicas

If a dataset is still intact, record decoded chunk hashes and keep the JSON independently from the acquisition:

```powershell
python -m h5reclaim capture-baseline healthy.h5 --dataset /experiment/readings --output baseline.json
```

Every selected chunk must be allocated and structurally decodable. The JSON records bytes **observed when this command ran**, not evidence that earlier readings were correct. It cannot be created retrospectively from a lost healthy file. An optional later replica manifest has this shape, with actual hashes and absolute paths:

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
| `unittest discover` | Runs parser, output, negative, resource, and end-to-end tests. It does not recover a supplied user file. |

Benchmark readable summaries include paths to retained HDF5 and JSON evidence. Use `--json` for complete output where offered. The original healthy file and mutation manifest are evaluator truth, never arguments to recovery. These constructed experiments cannot measure a real-world success percentage.

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

The destinations must not exist. Recovery reads only the damaged input through a private snapshot. Native HDF5 normally locates the selected local dataset and schema; when native metadata lookup fails, a narrower rooted parser can resolve surviving old symbol-table or modern compact-group links and required messages. It refuses unsupported paths and records this route in `metadata_resolution`. A bounded independent parser validates raw indexes and decodes chunks. Version-1 trees can bridge one lost **leaf** pointer only through a unique matching parent interval and reciprocal left/right sibling links. Modern indexes require intact anchored pointer paths; missing modern index links are not reconstructed. The report's `evidence_ledger` records index chains, physical ranges, checksums or their absence, decoding checks, contradictions, and per-region decisions.

| Structural condition | Supported behavior |
| --- | --- |
| Dataset | One local rank-one through rank-four chunked canonical integer (8/16/32/64 bit signed or unsigned) or IEEE float32/64 dataset, either byte order. Positive current dimensions, partial edge chunks, sparse allocation, and growing maxima are supported when the index parser validates their mapping. |
| Older format | Superblock v0/v1, object header v1 with bounded continuation, layout v3, version-1 B-tree with intact level-zero or deeper tree. One missing leaf link can be bridged even below a deeper root. Internal subtree loss or multiple lost links is refused. Older raw metadata fallback has a narrower original numeric/filter envelope and unchecksummed link ownership. |
| Newer format | Checksummed superblock v2/v3, checksummed selected object header v2 with bounded continuations, layout v4/v5 and intact single-chunk, implicit, filtered/paged/sparse fixed array, bounded extensible array including validated initialized pages, or version-2 B-tree. Missing modern index links remain unsupported. Raw metadata fallback follows rooted compact links or a bounded checksummed dense-group name index and managed fractal heap; unsupported variants refuse. |
| Selected shared messages | A committed canonical numeric datatype can resolve through a checked v2 object header. Bounded SOHM single-list managed-heap references can resolve selected shared dataspace or datatype messages. Unsupported SOHM index and heap variants refuse. |
| Filters | Bounded built-in shuffle, DEFLATE, and Fletcher32 in their actual declared order; per-chunk optional skip masks. Missing unknown decoders are reported separately, never treated as verified measurements. |
| Resource bounds | Source snapshot at most 4 GiB, 30-minute 1 MiB-buffer copy, temporary disk for full logical source size plus 32 MiB reserve; structural dataset at most 1,048,576 elements, 4,096 chunks and traversed nodes, 1 MiB decoded chunk. |

The structural decoder enforces a strict expansion bound and exact final nominal chunk length. Invalid masks, unknown active filters, missing metadata anchors, overlapping physical extents, invalid checksums, or destroyed payloads cannot be promoted to measurements. An unknown active filter with an otherwise anchored chunk is status 7. A missing index slot remains allocation unknown; output fill is never proof of a measurement.

The output's `/_h5reclaim/chunk_status` is indexed by chunk grid:

| Code | Meaning |
| --- | --- |
| 1 `recovered` | The coordinate and payload passed the documented structural checks. Unfiltered payload bytes still lack an independent checksum. |
| 2 `allocation_unknown` | No accepted assignment; output fill is unknown, not a measurement. |
| 6 `decode_failed` | An anchored chunk failed decoding or its checksum; output fill is unknown. |
| 7 `decoder_unavailable` | An anchored chunk needs an unsupported active filter decoder; output fill is unknown. |

Codes 3 `ambiguous`, 4 `unavailable`, and 5 `unsupported` are reserved in this output. Check the status map even if the HDF5 output opens. `complete` means all grid chunks have status 1, not that original science was independently authenticated. The report is external JSON and embedded at `/_h5reclaim/report_json`. Only the selected dataset and bounded safe attributes are copied; siblings, links, scales, and full scientific context are excluded.

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

Logical data is limited to 512 MiB in 1 MiB blocks, with 2 MiB stored chunks and at most 8,192 grid entries. Supported compiled-in filters include DEFLATE, shuffle, Fletcher32, NBIT, SCALEOFFSET and SZIP when the linked HDF5 library provides both SZIP directions. SCALEOFFSET can be lossy at acquisition. Bounded top-level object and region references to the selected dataset are remapped and checked by logical target and selection. Outside or dangling references, nested reference graphs, variable-length heap values, plugin filters, and arbitrary bit representations are refused. External/VDS values use the separate pinned-bundle routes above. Native reads run in a child process with a 900-second deadline and disabled dynamic filter plugin loading. POSIX applies a 3 GiB address-space cap and disables core dumps; Windows has the deadline but no enforced worker memory cap. This is process isolation for crashes and resource bounds, not a security sandbox for hostile native code. This route cannot repair an inaccessible index or verify values before damage.

## Interpreting results

Exact restoration is impossible if the only original measurement bytes were overwritten and no independent copy or redundancy survives. Bytes can also survive while their coordinate or dataset ownership is unknowable. See [current status](status.md), [evidence model](evidence-model.md), and [generalization limits](generalization-plan.md) for the measured evidence and remaining cases.
