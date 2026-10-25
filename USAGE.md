# H5Reclaim usage

Start with automatic recovery. Use the workbench commands when you need to
inspect a failure, review a repair, verify a saved output, or process a batch.
All recovery and copy destinations must be new; the input is kept unchanged.

## Installation and basic recovery

From a source checkout:

```sh
python -m pip install .
python -m h5reclaim rescue damaged.h5
```

Defaults are `damaged.recovered.h5` and `damaged.recovered.report.json` beside
the input. Select a dataset when its path is known:

```sh
h5reclaim rescue damaged.h5 --dataset /experiment/measurements --output recovered.h5 --report recovery.json
h5reclaim report recovery.json --html recovery.html
h5reclaim doctor
h5reclaim capabilities
```

The HTML report is self-contained and searchable. It summarizes the saved
JSON evidence without opening the input or rechecking the recovered file.

Install optional codecs or the alternative reader from the checkout:

```sh
python -m pip install ".[filters]"
python -m pip install ".[readers]"
```

The core package uses Python 3.10+, h5py and NumPy. Optional-reader dependency
versions are selected by pip for your Python version. Official HDF5 command
line tools are a separate platform package, such as `hdf5-tools` on Debian
or Ubuntu. `doctor` shows which tools and optional libraries are available.

## Large compressed datasets with broken indexes

Automatic selected-dataset and whole-file rescue try the streaming structural
route when the smaller route cannot cover the dataset. To request it directly:

```sh
h5reclaim rescue damaged.h5 --dataset /experiment/measurements --large-structural --output recovered.h5 --report recovery.json
```

| Property | Large structural recovery |
| --- | --- |
| Index families | Fixed array, extensible array, v2 B-tree |
| Rank | 1 through 4; rank 5 with fixed arrays |
| Records | Bounded fixed-width records; no heap pointers or object references in this route |
| Filters | gzip, shuffle, Fletcher32, LZF and supported installed hdf5plugin codecs |
| Damage | At most one index link justified by a unique restoration of its original parent checksum |
| Chunk grid | At most 65,536 slots, subject to the configured budget |
| Decoded chunk | At most 64 MiB, subject to the configured budget |
| Partial edges | Cropped to the declared dataset shape |
| Damaged payload | Nonaccepted status; other justified chunks continue |
| Unallocated slots | Unknown, even if a reader displays a fill value |

Schema, checked index structures, allocation bounds and competing ownership
are checked before values are accepted. The output stores decoded chunks
without source compression and records physical ranges and payload digests.

Default streaming quotas include a 64 GiB source, 8 GiB of physically copied
source data, 16 GiB of logical selected data, 18 GiB output, and an 8 GiB
physical link-search budget. These are quotas, not measured throughput claims.
Dense inputs count fully toward copy/search budgets. Large sparse containers
can have a much larger address space than their physically allocated bytes.

Provide a JSON file to override caller-controlled limits, for example:

```json
{
  "max_copied_bytes": 17179869184,
  "max_scan_bytes": 17179869184,
  "max_seconds": 1800
}
```

```sh
h5reclaim rescue damaged.h5 --dataset /experiment/measurements --streaming-budget budget.json
```

Other parser-specific limits still apply. `capabilities` prints the supported
envelope and default quota fields. Increasing a quota does not make an
unsupported index or ambiguous ownership recoverable.

## Inspect metadata without reading measurements

```sh
h5reclaim check damaged.h5 --json
h5reclaim check damaged.h5 --profile strict --json
h5reclaim explain damaged.h5 --path /experiment/measurements
h5reclaim explain damaged.h5 --offset 0x200 --length 64
h5reclaim markers damaged.h5 --json
```

The raw preflight checks reachable object headers, local namespaces and
supported chunk indexes without opening an HDF5 file through the native
library, decompressing measurements or following external dependencies.
It reports its coverage and incomplete walks. It is not a complete validator
of every HDF5 semantic constraint or a security sandbox. The strict profile
also rejects declared nonlocal dependencies. Marker matches are unassigned
candidates; they do not prove ownership or surviving measurements.

## Review and apply metadata repairs

```sh
h5reclaim plan-repair damaged.h5 --output repair.plan.json
h5reclaim explain-plan repair.plan.json
h5reclaim apply-repair damaged.h5 repair.plan.json --output repaired-copy.h5 --audit repair.audit.jsonl
```

The default catalog includes original-checksum corrections of modern
signatures/root pointers, selected object layout pointers and dimensions,
checked FA/EA/B-tree child links, stale consistency flags, and a validated
legacy signature restoration. The plan lists exact before/after bytes.
Application hashes the input again and regenerates the entire plan. An
edited or stale plan is refused. The repaired copy must pass the independent
covered-metadata preflight before it and its audit log are published.

For structurally justified resealing, explicitly request:

```sh
h5reclaim plan-repair damaged.h5 --allow-reseal --output repair.plan.json
```

This can also consider modern superblock base/EOF normalization, reachable v2
object-header checksums, layout-owned FA/EA/B-tree header checksums, typed
chunk-layout element widths and depth-zero B-tree record totals. Resealing
establishes current structural coherence. It cannot establish that the bytes
match their historical values. Unresolved plans are saved for inspection
with `executable: false`; they cannot be applied. Arbitrary missing metadata,
general free-space repairs and unconstrained checksum rewriting are outside
the implemented catalog. Use `capabilities` for the exact list.

## Verify results and use accepted measurements

```sh
h5reclaim verify-result recovered.h5 recovery.json
h5reclaim verify-result recovered.h5 recovery.json --values
```

The first command checks report/schema/map consistency. `--values` additionally
rehashes every accepted fixed-record chunk against saved publication digests
when the route provides complete digest coverage. Older artifacts and some
logical/native routes do not carry these digests; the command reports that
limitation instead of claiming a complete value check.

For any closed output/report pair, keep an independent artifact fingerprint:

```sh
h5reclaim fingerprint-result recovered.h5 recovery.json --output recovery.fingerprint.json
h5reclaim verify-fingerprint recovered.h5 recovery.json recovery.fingerprint.json
```

This hashes complete artifact bytes, including output measurements. Keep the
fingerprint separately if you want to detect later edits to both artifacts.
It records the bytes at capture and does not authenticate historical data.

For fixed-size selections, use the reported map through the masked reader:

```python
from h5reclaim import read_masked

values = read_masked(
    "recovered.h5", "recovery.json", "/experiment/measurements",
    selection=(slice(0, 100), slice(None)),
)
```

Nonaccepted positions are masked. Do not substitute missing positions with
ordinary measurements. Some routes have no checkable map or a variable-width
representation; follow their route-specific evidence instead.

## Batches and Python integration

```sh
h5reclaim batch-rescue first.h5 second.h5 --output-dir recovered --summary batch.json --jobs 2 --html
h5reclaim batch-rescue first.h5 second.h5 --output-dir recovered --summary batch.json --resume
```

Jobs are bounded to 1 through 4. Each file has a distinct output folder.
Failures are recorded without discarding other results. Completed results
are reused only when their input, output/report fingerprints, tool version
and recovery budgets still match. Failed files with untracked existing
artifacts need a new output directory. Use `--fail-on-partial` when partial
results should make the batch return a failure status.

```python
from h5reclaim.api import rescue

report = rescue("damaged.h5", dataset="/experiment/measurements")
# Omit dataset to discover and recover the whole file.
```

The API accepts explicit output/report paths, a budget dictionary or
`LargeBudget`, a resume directory and an optional HTML destination.

## Alternative-reader exports

```sh
h5reclaim export-alternative damaged.h5 --dataset /experiment/measurements --output alternative.h5 --report alternative.json
```

This opt-in path uses Pyfive on a raw-checked local path. It does not replace
structural recovery automatically. Fixed values are accepted only when native
HDF5 and Pyfive agree and the independent preflight completed successfully.
Other copied values remain status-2 inspection candidates and are masked by
`read_masked()`. Pyfive has its own format limitations and may not support
newer chunk-layout versions that H5Reclaim's structural parsers support.
Declared soft/external/virtual dependencies are not followed by this route.

## Official HDF5 copy/repack integration

When `h5copy` or `h5repack` is installed:

```sh
h5reclaim copy readable.h5 --dataset /science --destination-path /copied/science --output copied.h5 --report copy.json
h5reclaim repack readable.h5 --compression gzip --level 6 --output repacked.h5 --report repack.json
```

These commands run the official executables in limited workers against
private copies. Source mutations, failed tools and output-budget overruns
prevent publication. The report identifies the operation as a readable-file
tool export, distinct from a measurement recovery verdict. Tool compatibility
with a particular file version depends on the installed HDF5 tools.

## Comparable tools and implementation boundaries

The workbench addresses capabilities also found in The HDF Group's
[h5clear](https://support.hdfgroup.org/documentation/hdf5/latest/_h5_t_o_o_l__c_r__u_g.html),
[h5repack](https://support.hdfgroup.org/documentation/hdf5/latest/_h5_t_o_o_l__r_p__u_g.html),
[H5Lens/h5patch](https://github.com/HDFGroup/hdf5-pickles), and
[ESRF h5recover](https://gitlab.esrf.fr/hdf5/h5recover). Their complete
implementations and feature catalogs are not bundled or claimed as identical.
The H5Reclaim additions use its own existing parsers and new integration code;
Pyfive and the official command-line tools remain explicitly identified
optional dependencies.

## Development checks

```sh
python -m pip install -e ".[filters,readers,applications]"
python -m unittest discover -s tests -q
python benchmarks/run_streaming_recovery.py
python benchmarks/run_release_coverage.py
python -m build
```

The streaming benchmark injects known link/payload faults into generated
HDF5-written data and scores accepted coordinates against separate NumPy
truth. Its sparse >4 GiB case checks address handling, not dense-file speed.
