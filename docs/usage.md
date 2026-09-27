# Run H5Reclaim

H5Reclaim works from a damaged HDF5 file. It does not need a healthy original for diagnosis or recovery. The bundled originals are used to make disposable damage copies and independently score controlled experiments. An output file holds one selected dataset and a validity map, not a repair of the source container or proof of historical values.

## Install this checkout

Use Python 3.10 or newer in the extracted repository root. On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m h5reclaim --help
```

On Linux or macOS, activate with `. .venv/bin/activate`. `pip install -e .` installs this local checkout, not a PyPI copy of H5Reclaim. Pip may fetch NumPy, h5py, and build tools if they are absent. The installed `h5reclaim` and `python -m h5reclaim` commands are equivalent.

## Run on supplied scientific data

Four unchanged attributed HDF5 files and pinned hashes are bundled in `corpus/`. No fixture generation or data download is required:

```powershell
python benchmarks/run_real_corpus.py
python benchmarks/run_gwosc_recovery.py
python benchmarks/run_damage_catalog.py
python benchmarks/run_seeded_matrix.py --seed 20260927 --trials 2
python -m unittest discover -s tests -q
```

| Command | Operation |
| --- | --- |
| `run_real_corpus.py` | Verifies four original hashes and inventories 251 local datasets without reading values or damaging files. |
| `run_gwosc_recovery.py` | Breaks one verified index pointer in a disposable GWOSC copy, runs the public recovery CLI on the damaged copy alone, and independently scores output against the untouched original. |
| `run_damage_catalog.py` | Checks ten selected structural, payload, metadata, and refusal cases on copies. |
| `run_seeded_matrix.py` | Varies fault positions using a published seed, separately counts exact accepted chunks, wrong accepted values, unknown regions, and safe refusals. The default has 23 correlated cases. |
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

The validator matches declared names exactly, hashes supplied files, checks fixed external byte ranges, and can inspect local target-object metadata of an explicit HDF5 dependency. It does not infer paths, read dependent values, follow transitive links, or export external/VDS measurements. Dynamic VDS names and unlimited raw ranges stay unresolved. A matching hash confirms identity relative to the supplied hash, not historical correctness. See [dependency bundles](dependency-bundles.md).

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

The destinations must not exist. Recovery reads only the damaged input through a private snapshot. Native HDF5 locates the selected local dataset and schema, and a bounded independent parser validates supported raw indexes and decodes chunks. Version-1 trees can bridge one lost **leaf** pointer only through a unique matching parent interval and reciprocal left/right sibling links. Modern single-chunk and implicit indexes derive addresses from the selected object's checked layout. A narrowly supported nonpaged fixed array follows checked header/data-block pointers and array slots. Broken modern indexes are not reconstructed. The report's `evidence_ledger` records index chains, physical ranges, checksums or their absence, decoding checks, contradictions, and per-region decisions.

| Structural condition | Supported behavior |
| --- | --- |
| Dataset | One local fixed rank-two canonical little-endian `uint32` without filters or rank-one canonical little-endian IEEE `float64` with exactly Fletcher32 followed by DEFLATE, which may be skipped per chunk. Positive dimensions divisible by chunks. |
| Older format | Superblock v0/v1, object header v1 with at most one bounded continuation, layout v3, version-1 B-tree with intact level-zero or deeper tree. One missing leaf link can be bridged even below a deeper root. Internal subtree loss or multiple lost links is refused. |
| Newer format | Checksummed superblock v2/v3, checksummed selected object header v2 with bounded continuations, layout v4/v5 and intact single-chunk, implicit, or nonpaged, fully allocated unfiltered fixed-array index. Paged/filtered/sparse fixed arrays, extensible arrays, and v2 B-trees remain structurally unsupported. |
| Resource bounds | Source snapshot at most 4 GiB, 30-minute 1 MiB-buffer copy, temporary disk for full logical source size plus 32 MiB reserve; structural dataset at most 1,048,576 elements, 4,096 chunks and traversed nodes, 1 MiB decoded chunk. |

The structural decoder accepts only the stated built-in filter order and supported per-chunk DEFLATE skip bit, with a strict expansion bound. Other masks, unknown filters, partial edge chunks, growing/sparse structural indexes, missing metadata anchors, overlapping physical extents, invalid checksums, or destroyed payloads cannot be promoted to measurements.

The output's `/_h5reclaim/chunk_status` is indexed by chunk grid:

| Code | Meaning |
| --- | --- |
| 1 `recovered` | The coordinate and payload passed the documented structural checks. Unfiltered payload bytes still lack an independent checksum. |
| 2 `allocation_unknown` | No accepted assignment; output fill is unknown, not a measurement. |
| 6 `decode_failed` | An anchored chunk failed decoding or its checksum; output fill is unknown. |

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

Logical data is limited to 512 MiB in 1 MiB blocks, with 2 MiB stored chunks and at most 8,192 grid entries. References, variable-length data, external/VDS storage, custom filters, and noncanonical numeric types are refused. Native HDF5 reads happen in this process, without a crash-isolated worker. This route cannot repair an inaccessible index or verify values before damage.

## Interpreting results

Exact restoration is impossible if the only original measurement bytes were overwritten and no independent copy or redundancy survives. Bytes can also survive while their coordinate or dataset ownership is unknowable. See [current status](status.md), [evidence model](evidence-model.md), and [generalization limits](generalization-plan.md) for the measured evidence and remaining cases.
