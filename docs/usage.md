# Run H5Reclaim

## Install

Use Python 3.10 or newer. Clone the repository or extract its GitHub ZIP, then open a terminal in the repository root:

```sh
python -m venv .venv
```

Activate `.venv` with `.venv\Scripts\Activate.ps1` in Windows PowerShell, `.venv\Scripts\activate.bat` in Windows Command Prompt, or `. .venv/bin/activate` on Linux/macOS. Then install:

```sh
python -m pip install .
python -m h5reclaim --help
python -m h5reclaim --version
```

Install with `python -m pip install ".[filters]"` to enable installed packaged Zstd, Blosc, Blosc2, Bitshuffle, LZ4, BZip2, ZFP, SZ, SZ3, SPERR, HTJ2K and FCI codecs. Recovery workers register packaged codecs explicitly. Add `applications` to run the MATLAB 7.3, netCDF4 and NWB writer/reader evaluations. Developers can use an editable installation with `python -m pip install -e ".[filters]"`.

The installed `h5reclaim` command and `python -m h5reclaim` are equivalent. Quote paths containing spaces. Keep the source closed during recovery so its bytes remain stable.

## First recovery

```sh
python -m h5reclaim rescue "damaged.h5"
python -m h5reclaim report "damaged.recovered.report.json"
python -m h5reclaim verify-result "damaged.recovered.h5" "damaged.recovered.report.json"
```

The first command creates `damaged.recovered.h5` and `damaged.recovered.report.json` beside the source. The second summarizes the saved report. The third checks that the published output and report agree on embedded evidence, dataset shapes, source annotations, and validity maps. It does not read or hash recovered measurement payloads.

Use `--output rescued.h5` to select a destination; its default report becomes `rescued.report.json`. An explicit `--report evidence.json` overrides that choice. All destinations must be new. If a previous result exists, choose different names, for example:

```sh
python -m h5reclaim rescue damaged.h5 --output rescued-2.h5 --report evidence-2.json
```

| Command | Use |
| --- | --- |
| `rescue` | Recover a whole file, or one dataset with `--dataset` |
| `report` | Summarize a saved JSON evidence report |
| `verify-result` | Check a published output and report for structural and map consistency |
| `diagnose` | Inspect file metadata and conditions before recovery |
| `survey` | List readable dataset metadata and structural-parser classifications |
| `discover` | Find surviving dataset headers when original group links cannot be read |

Use `python -m h5reclaim COMMAND --help` for a command's options. `diagnose` and `survey` describe structural-parser support; an unsupported structural classification can still have a native-readable recovery method. See [coverage](coverage.md) for the evaluated limits.

## Recover a whole file

```sh
python -m h5reclaim rescue damaged.h5
```

Omitting `--dataset` discovers datasets and recovers them independently. `--all` makes the same choice explicit. A failed dataset is listed in the report while the others continue. Available groups, large and null attributes, named datatypes, hard-link aliases, local soft links, object and region references, dimension labels, and dimension scales are rebuilt. Distinct committed datatypes retain their identities even when their definitions match. References are remapped after their targets exist and their logical identities and regions pass readback verification.

The report lists `datasets_discovered`, `datasets_exported`, `datasets_failed`, dataset reports, inventory issues, and restored context. Its `metadata_group` names the recovery metadata location, normally `/_h5reclaim`. Each dataset's evidence is below that group's `datasets/dNNNNNN/`. Existing source names cause a different group name to be chosen. MATLAB 7.3 files keep recovery metadata below `/#refs#` so the application reader does not treat it as a user variable. The original user block is preserved.

When group metadata is unreadable, whole-file rescue also checks surviving legacy and modern dataset object headers. Objects with lost names receive paths such as `/recovered/object_b3`; the report keeps their original names unresolved. Unresolved references, missing related files, and omitted attributes are listed explicitly. A partial result records which data and context are available.

Use a related-file manifest to materialize external raw, virtual and external-link datasets in the same recovery:

```sh
python -m h5reclaim rescue container.h5 --all --related-files related.json --output local.h5 --report evidence.json
python -m h5reclaim rescue container.h5 --related-dir companion-files --output local.h5 --report evidence.json
```

## Resume recovery

```sh
python -m h5reclaim rescue damaged.h5 --resume-dir recovery-progress --output rescued.h5 --report evidence.json
python -m h5reclaim rescue damaged.h5 --dataset /readings --resume-dir selected-progress --output selected.h5 --report selected.json
```

The progress directory commits completed dataset outputs and reports after verification. Native exports also cache decoded, readback-verified allocated chunks or bounded contiguous blocks, including logical strings, arrays and references. An interrupted selection is retried; completed selections avoid rereading source values. Rerun with the same input, options and progress directory. Source hashes, schema, tool version and cached hashes must match; driver checkpoints pin every member. A lock prevents concurrent use of the same progress directory. Choose new final destinations if a previous run already published its result. Explicit Family and Split exports also accept selection checkpoints.

## Recover one dataset

```sh
python -m h5reclaim diagnose damaged.h5 --dataset /experiment/readings
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --output selected.h5 --report selected.json
```

Automatic selection checks file condition, then chooses structural chunk recovery, compact/contiguous recovery, or a native-readable and streaming route. An unavailable structural decoder can fall through to an installed native decoder before publication. Physical tail truncation and a checked version-3 interrupted-write flag are detected automatically. Whole-file rescue can also recognize a damaged modern chunk dimension for a directly linked dataset when its original object-header checksum identifies a unique correction; the trial runs on a private copy. Structural contradictions remain errors.

Structural routes decode DEFLATE, LZF, shuffle, Fletcher32 and installed packaged codecs. Readable routes also use available native HDF5 filters. Pipelines that can change values are stored without source filters, preserving the currently decoded source values. Streaming supports rank through 32, scalar, empty and null datasets, preserves the declared HDF5 datatype, and verifies fixed record bytes, including compound padding, array records and numeric widths absent from NumPy. Heap-backed records use bounded batches and logical comparisons of strings, ragged sequences, compound fields and references. Failed native batches fall back to individual records. Failed chunks or elements stay unknown while other readable allocations continue. A selected-dataset export can remap self references; references to other objects remain deferred until whole-file recovery creates their targets.

New destinations are required. H5Reclaim snapshots the input and checks that the source has not changed before publishing output.

## Discover surviving dataset headers

```sh
python -m h5reclaim discover damaged.h5 --json
python -m h5reclaim rescue damaged.h5 --object-address 0xb3 --dataset /readings --output selected.h5 --report selected.json
python -m h5reclaim rescue damaged.h5 --dataset /readings --hints expected.json --output selected.h5 --report selected.json
```

Use an address returned by `discover`. Discovery validates modern object-header checksums or legacy header structure, datatype messages and independent native allocation agreement. It checks physical bounds, metadata overlap and competing allocations. Legacy headers have no object-header checksum; the report distinguishes their evidence. If the original root cannot open, a disposable view supplies an empty root outside the original EOF. Recovered allocations must still fit the original source. Automatic rescue can also correct a uniquely identified root-pointer byte while preserving the original superblock checksum. The report identifies view edits, unresolved namespace and detached ownership scope. The supplied output path does not assert an original dataset name.

The `--hints` JSON uses `schema_version: 1` and a `dataset` object with `path` plus optional `shape`, `chunks`, `dtype` and `filters`. Optional `source_sha256` pins the damaged input. Conflicting hints refuse publication; matching hints cannot supply missing measurements or replace observed placement.

## Read results

Use the exact map path and code definitions in each report. Common maps are:

| Map | Unit represented |
| --- | --- |
| `chunk_status` | Structurally attributed chunk |
| `element_status` | Individual stored element, including variable values |
| `validity` | Native-allocated chunk, or the reported nonchunked unit |
| `historical_status` | Equality to a separately supplied prior capture |

An unknown position may display its dataset's fill value. Accepted positions identify exported source values. `complete` describes the route's export coverage. Supply a prior capture to add historical comparisons.

For fixed-size datasets, `read_masked` uses the report's per-dataset status map and returns a NumPy masked array. It masks unresolved positions before you analyze the values:

```python
import numpy as np
from h5reclaim import read_masked

readings = read_masked(
    "damaged.recovered.h5", "damaged.recovered.report.json",
    "/experiment/readings", selection=(slice(0, 1000), Ellipsis),
)
average = np.ma.mean(readings)
```

Choose the dataset path from the recovery report. The selection accepts integers, forward slices, and one ellipsis. The default limits are one million selected elements and 64 MiB of logical data and status; use `max_elements` and `max_bytes` for a different bounded read. Variable-length and null datasets need direct inspection of their reported status maps. The reader refuses absent or inconsistent map metadata. Its mask expresses current-source recovery status, not equality to a historical capture.

Whole-file metadata paths include the per-dataset prefix. Each JSON report names its actual `metadata_group`, source hashes, route, counts, output path, and evidence details. Larger native allocation ledgers are HDF5 datasets named by `source_allocations`; smaller ledgers remain in `source_chunk_records`. `--no-context-audit` skips the additional selected-dataset context inventory; whole-file rescue still rebuilds its inventoried context.

### Verify a published result

```sh
python -m h5reclaim verify-result rescued.h5 evidence.json --source damaged.h5
python -m h5reclaim verify-result rescued.h5 evidence.json --json
```

`verify-result` compares the saved report to its embedded copy, checks source hash annotations, validates each reported dataset shape and status-map layout, scans declared codes, and checks reported accepted and unknown counts where available. `--source` additionally hashes the explicitly supplied damaged source against the report. The check runs in a worker with memory and time limits. An output with no checkable validity map, such as some contiguous or null-dataspace routes, is reported as unsupported.

Exit code `0` means these consistency checks passed; `1` means the output and report disagree; `2` means an unsupported route, exceeded budget, or worker failure. The command does not read or authenticate measurement payloads, prove that a source was genuine, or establish equality to an earlier capture. A jointly modified output and report can still agree with each other. Retain a trusted earlier capture or independent checksum when historical identity matters.

### Outcomes and exit codes

`complete` means the selected recovery method exported its coverage and required context without unresolved items. `partial` means an output was created with unresolved values, datasets, or context. Its maps and failure entries identify the available data for analysis.

By default, `rescue` returns exit code `0` when it publishes an output, including a partial output. Add `--fail-on-partial` when automation should treat a partial result as exit code `1`; the output and report are still created. Invalid arguments or recovery failures return `2`.

```sh
python -m h5reclaim rescue damaged.h5 --fail-on-partial
```

### Troubleshooting

| Message or situation | Next step |
| --- | --- |
| Output or report already exists | Choose fresh `--output` and `--report` paths. Existing files are protected. |
| Missing compression decoder | Install `python -m pip install ".[filters]"` from the source repository and retry. A custom filter may still require its own installed decoder. |
| External or virtual data is unresolved | Supply the required companion files with `--related-dir` or a [pinned manifest](dependency-bundles.md). |
| Resource budget exceeded | Check free disk space and adjust the relevant field in a streaming-budget JSON below. |
| Source changed during recovery | Close the writer or work from a stable copy, then rerun with fresh destinations. |
| Partial recovery | Run `python -m h5reclaim report evidence.json` and use its exact status-map paths to identify accepted values. |
| Dataset names cannot be read | Run `discover --json`; automatic whole-file recovery also checks surviving dataset headers. |

## Configure streaming resources

Supply a JSON object with the budget fields you want to override:

```json
{
  "max_source_bytes": 68719476736,
  "max_copied_bytes": 8589934592,
  "max_logical_bytes": 17179869184,
  "max_output_bytes": 19327352832,
  "max_chunks": 65536,
  "max_grid": 67108864,
  "max_seconds": 850,
  "block_bytes": 1048576,
  "disk_reserve_bytes": 67108864,
  "max_metadata_bytes": 67108864,
  "max_objects": 100000,
  "max_links": 500000,
  "max_chunk_bytes": 67108864,
  "logical_batch_records": 256,
  "max_type_depth": 64,
  "max_type_members": 65536,
  "worker_memory_bytes": 3221225472
}
```

```sh
python -m h5reclaim rescue large.h5 --streaming-budget budget.json --output rescued.h5 --report evidence.json
python -m h5reclaim rescue large.h5 --dataset /readings --large-readable --streaming-budget budget.json --output streamed.h5 --report streamed.json
```

Counts and byte sizes are integers. Duration is a finite positive number. Omitted fields use the defaults above. Increase the relevant budgets for a larger acquisition, metadata inventory or decoded chunk. Sparse snapshots use POSIX allocation ranges or Windows filesystem queries to preserve holes; dense copying checks the copy budget and free disk space. One private source image is shared across automatic routes and whole-file datasets. Configured budgets govern inventory, datatype validation, automatic fallback, output publication, worker memory and deadlines. The report records how many physical bytes were copied.

Worker memory uses an address-space limit on Linux and a Job Object committed-memory limit on Windows. On macOS the parent samples the worker process group's resident memory every 100 milliseconds and stops the group when it exceeds the configured budget. Allocations can exceed that budget between samples. Native-readable reports identify the memory mechanism used.

## Select an advanced route

Add one route option to a selected-dataset `rescue` command:

| Option | Purpose |
| --- | --- |
| `--object-address ADDRESS` | Export a checked surviving legacy or modern dataset header under the supplied output path |
| `--truncated-chunks` | Recover physically complete rooted chunks before a tail cut |
| `--status-trial` | Check and clear a version-3 write flag on a private trial copy |
| `--metadata-trial root` | Search a damaged modern root-pointer byte using its original checksum |
| `--metadata-trial layout` | Search a selected modern index-pointer byte using its original checksum |
| `--metadata-trial dimension` | Search a selected modern chunk-dimension byte using its original checksum |
| `--large-readable` | Stream native-readable multidimensional fixed records |
| `--large-structural` | Stream the implemented fixed-array pointer-recovery case in a large sparse file |
| `--related-files related.json` | Materialize external raw, virtual, or external-link values |
| `--related-dir DIRECTORY` | Build a pinned manifest from declared companions inside this directory |
| `--family-members family.json` | Read a pinned Family address space |
| `--split-members split.json` | Read pinned Split metadata and raw members |
| `--chunk-baseline baseline.json` | Compare decoded chunks with a prior pinned baseline |
| `--element-baseline baseline.zip` | Compare compact/contiguous numeric elements with prior hashes |
| `--replicas replicas.json` | Recover baseline-matching chunks from independent copies |
| `--parity parity.json` | Reconstruct a missing chunk using prior XOR parity |
| `--erasure erasure.json` | Reconstruct multiple missing chunks using prior parity shards |
| `--capsule capsule.zip` | Use a prior schema and physical allocation map |
| `--protection-bundle protection.zip` | Use an earlier combined capsule/parity bundle |

Baseline, capsule, and protection options take the matching SHA-256 argument shown in `rescue --help`. Related-file and driver manifests are described in [dependency bundles](dependency-bundles.md). Related files, Family and Split manifests also apply to whole-file rescue.

## Protect an intact acquisition

Capture evidence before an incident, and keep the printed manifest digest independently:

```sh
python -m h5reclaim protect healthy.h5 --dataset /experiment/readings --output protection.zip
python -m h5reclaim verify-protection protection.zip --manifest-sha256 PRIOR_MANIFEST_SHA256 --source healthy.h5
python -m h5reclaim drill-protection protection.zip healthy.h5 --manifest-sha256 PRIOR_MANIFEST_SHA256
```

Later:

```sh
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --protection-bundle protection.zip --protection-manifest-sha256 PRIOR_MANIFEST_SHA256 --strict-history --output verified.h5 --report verified.json
```

`--strict-history` publishes only when the selected route verifies every accepted unit against a separately pinned prior capture. A capsule provides schema, locations, and hashes; parity also supplies replacement information. A hash alone cannot reconstruct missing bytes.

Individual capture commands remain available:

```sh
python -m h5reclaim capture-baseline healthy.h5 --dataset /readings --output baseline.json
python -m h5reclaim capture-element-baseline healthy.h5 --dataset /readings --output elements.zip
python -m h5reclaim capture-capsule healthy.h5 --dataset /readings --output capsule.zip
python -m h5reclaim capture-parity healthy.h5 --dataset /readings --baseline baseline.json --output parity.zip
python -m h5reclaim capture-erasure healthy.h5 --dataset /readings --baseline baseline.json --stripe-width 8 --parity-shards 3 --output erasure.zip
```

Replica and parity manifests contain `schema_version: 1`, `damaged_sha256`, a `baseline` entry, and respectively `replicas`, `parity`, or `erasure`. File entries contain absolute `path` and lowercase `sha256` fields; `replicas` is an array.

## Inspect and investigate

```sh
python -m h5reclaim survey damaged.h5
python -m h5reclaim inspect damaged.h5 --dataset /readings
python -m h5reclaim export-readable readable.h5 --dataset /readings --output values.h5 --report values.json
python -m h5reclaim export-fragments evidence.json damaged.h5 --output fragments.zip
```

`diagnose` and `survey` inspect metadata. Survey candidates describe structural-parser coverage; automatic rescue has additional native, variable, and streaming routes. Fragment export produces coordinate-free raw bytes from unresolved report extents. `probe-status FILE --json` runs an optional installed `h5clear` on a disposable copy.

For attributed sample data and reproducible evaluations, see [the corpus](../corpus/README.md) and [benchmarks](../benchmarks/README.md).
