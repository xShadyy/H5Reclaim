# Current usage and supported cases

H5Reclaim is an experimental command-line tool for one structural HDF5 recovery case: a child pointer in a version-1 raw-data chunk B-tree is lost, while the dataset description, neighboring leaves, and their reciprocal sibling links survive. It exports chunks whose coordinates are supported by that evidence into a **new** HDF5 file. The source is opened for reading and its identity and SHA-256 are checked during recovery. A bounded private snapshot lets h5py and the raw parser inspect the same bytes; it temporarily needs disk space up to the source's size, capped at 128 MiB.

This is a narrow implementation, not a general HDF5 repair utility. Structural evidence identifies where bytes belong. The rank-one floating-point path verifies each chunk's stored Fletcher32 checksum; the unfiltered rank-two path has no independent payload checksum. Neither establishes historical authenticity of a scientific measurement.

## Install

Python 3.10 or newer is required. In a terminal at the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
h5reclaim --help
```

`python -m venv .venv` creates an isolated Python environment; the next line activates it. `python -m pip install -e .` installs **this local checkout** in editable mode and makes the `h5reclaim` command available. It does not download H5Reclaim from PyPI. Pip may download the declared h5py/NumPy dependencies and build requirements; with those already installed and `--no-deps --no-build-isolation`, an offline editable installation is possible. On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. The first experiment ran on Python 3.12.14, h5py 3.12.1, HDF5 1.14.4, and NumPy 2.3.5. The current tests were also checked with h5py 3.16.0, HDF5 2.0.0, and NumPy 2.5.3.

Version 0.3.1 corrected a Windows source snapshot check that could reject an
unchanged input before HDF5 inspection. A native Windows PowerShell run supplied
by a user has since completed the controlled GWOSC trial with 128/128 chunks
bit exact, including all 57 behind the lost pointer. The same environment ran
50 discovered tests successfully, with one skipped because that Windows
account lacked symbolic-link privileges. This evidence covers that computer
and controlled input; it is not a claim that all Windows configurations or
damaged HDF5 files work.

## Run the bundled real-data trial

The ZIP includes four untouched, hash-pinned scientific HDF5 files from gravitational-wave, superconducting-qubit, and aircraft cloud experiments. No fixture-generation commands or downloads are required for this trial:

```sh
python benchmarks/run_real_corpus.py
python benchmarks/run_gwosc_recovery.py
```

The first command verifies the files and inventories 251 actual datasets: one is a recovery **candidate** under the current rules, while 250 are explicitly unsupported. It does not read values or claim recovery. The second command makes a damaged **copy** of the original 16 kHz GWOSC file in a temporary trial directory, confirms a native-reader failure, invokes the public recovery CLI on that copy, and independently compares every output float bit at the original sample coordinate with the untouched file. Its readable summary prints the trial directory and important result counts; it keeps detailed JSON evidence and recovered data there. In the recorded native Windows run, one broken root pointer made 57 of 128 chunks unavailable or incorrect to native reads; H5Reclaim reconstructed all 57, and all 128 output chunks were bit exact. This tests one controlled failure in authentic data, not a naturally damaged file or every HDF5 layout. Add `--json` to either command for the complete machine-readable summary on standard output. See [corpus provenance and coverage](../corpus/README.md) and [benchmark details](../benchmarks/README.md).

### What the commands do

| Command | Reads and creates | What success establishes |
| --- | --- | --- |
| `python benchmarks/run_real_corpus.py` | Verifies the pinned size and SHA-256 of four bundled originals, then reads their dataset metadata and chunk-index structures. It makes temporary private snapshots, which are removed; it does not change the originals or read their measurement values. | The 251-dataset support inventory still matches the pinned baseline: 1 candidate and 250 unsupported. No data has been recovered. |
| `python benchmarks/run_gwosc_recovery.py` | Verifies the authentic 16 kHz original and its 128 chunk-index records, makes a separate damaged copy, and changes one verified root child pointer there. It compares ordinary HDF5 reads, runs `python -m h5reclaim recover` on the damaged copy in a subprocess, then checks recovered values, status, evidence, attributes, and hashes against the untouched original. It leaves the recovered HDF5, damage manifest, and detailed JSON reports in a new temporary work directory. | The output of this one controlled trial has exactly matched the reference at every sample coordinate. The number of chunks native HDF5 could not read correctly is an observed result, not a predicted count for other damage. |
| `python -m unittest discover -s tests -q` | Python's standard `unittest` module discovers `test*.py` in `tests` and executes the checks; `-q` suppresses normal per-test names. Individual tests may create disposable files in temporary directories. | `OK (skipped=1)` after `Ran 50 tests` means 49 passed and one was skipped. The skipped case in the reported Windows run needs symbolic-link privileges. This command does not recover a file supplied by the user. |

For a retained GWOSC trial at a location you choose, pass
`--work-dir path/to/new-or-empty-directory`. Do not reuse a populated trial
directory. The default directory is in the operating system's temporary
location but is deliberately kept after the command so the evidence can be
inspected. `truth/evaluation.json` contains the complete independent score;
`results/recovery.json` contains H5Reclaim's own per-chunk recovery report;
`results/recovered.hdf5` contains the selected recovered strain dataset and
`/_h5reclaim/chunk_status`. The trial does not reconstruct the original file's
other datasets, links, and research context.

## Survey a file before recovery

```sh
h5reclaim survey path/to/input.h5
```

The default readable inventory shows support totals, candidates first, and up
to five dataset entries with brief reasons. Add `--json` for the **complete**
inventory of each local dataset's selectable path, shape, datatype, storage
layout, chunk dimensions, filters, and support reasons. It reads metadata and
index nodes, never dataset values or chunk payloads. It does not resolve soft
or external HDF5 links. `candidate` means a recovery attempt fits the observed
metadata, not that its payload is intact or that the attempt will succeed.
Select a candidate with `--dataset /its/path` for `inspect` or `recover`.
`inspect` prints a readable selected-dataset and chunk-index summary; add
`--json` to retain its complete structured summary. Exit code 0 means the
inventory completed, 1 means traversal was partial due to a declared limit or
unreadable link, and 2 means the source could not be surveyed. JSON mode gives
a structured error on failure; ordinary mode prints a concise error to standard
error. `inspect` and `recover` also exit 2 on an unsupported or failed attempt.

Unfamiliar structures are classified with reasons, not silently converted. A contiguous dataset has no chunk index to reconnect; a different tree family, datatype, filter, or partially filled edge chunk needs its own verified decoding and ownership rules. Changing its shape to match the supported case would risk inventing measurements. The GWOSC rank-one adapter is an example of adding a specific real structure after checking its original bytes and surviving index against HDF5's own chunk information.

## Reproduce the synthetic experiment (optional)

The commands below use separate paths for pristine truth, damaged input, recovered output, and the mutation manifest. Keep the pristine file and manifest outside the inputs available to a recovery run.

```sh
mkdir -p work/truth work/inputs work/results
python tools/make_healthy_fixture.py --output work/truth/healthy.h5
python tools/make_broken_link_fixture.py \
  --input work/truth/healthy.h5 \
  --output work/inputs/damaged.h5 \
  --manifest work/truth/mutation.json
h5reclaim inspect work/inputs/damaged.h5 --dataset /measurements
h5reclaim recover work/inputs/damaged.h5 --dataset /measurements \
  --output work/results/recovered.h5 \
  --report work/results/recovery.json
```

In PowerShell, create the directories with `New-Item -ItemType Directory -Force work/truth,work/inputs,work/results | Out-Null`, then run each Python command on a single line without the shell continuation `\` characters.

The fixture tool verifies the selected dataset's layout message, B-tree root, and every healthy chunk against h5py's chunk information. It changes one verified parent pointer to the HDF5 undefined address in a **copy**, then checks that affected standard reads fail or differ from the clean reference and unaffected reads remain exact. The manifest is benchmark truth and must never be passed to H5Reclaim.

For an independent trial with evaluator-only random values and exact placement checks:

```sh
python benchmarks/run_recovery.py --work-dir work/trial
```

The directory must be new or empty. See [benchmark details](../benchmarks/README.md). Tests run with `python -m unittest discover -s tests -v` after installation.

## Supported input

### How recovery works without a healthy reference

`h5reclaim recover DAMAGED --dataset /path --output NEW.h5 --report NEW.json`
receives the damaged file, a selected dataset path, and two new destinations.
It does not receive a healthy original, a mutation manifest, or benchmark
truth. It makes a temporary read-only snapshot of the **damaged** bytes so
h5py and the raw index parser inspect the same source. The damaged file's
surviving dataset metadata supplies the selected object's address, chunk
shape, datatype, filter pipeline, and B-tree root. If the dataset cannot be
resolved through that metadata, this release cannot proceed.

The parser follows intact root pointers to reachable leaves. For one missing
interior root-to-leaf pointer, the reachable leaf on each side must both name
the same detached leaf through their sibling links. The detached leaf must
point back to both neighbors, have the expected tree level, and match the
missing parent's exact key interval. Its entries then give chunk coordinates
and payload addresses within the **damaged file itself**. The program checks
bounds, uniqueness, overlaps, sizes, filter masks, and supported decoding;
on the filtered path it also verifies the stored Fletcher32 checksum. Only
accepted payloads are exported. Missing or undecodable regions retain a
nonrecovered status in `/_h5reclaim/chunk_status` in the new HDF5 output.

The healthy original appears only in controlled experiments: a benchmark
creates a damaged copy from it and independently scores the output after
recovery. In the bundled GWOSC file there is only one interior root child
that has both surviving neighbors for this failure mode. The synthetic
benchmark randomizes measurement values, but its earlier default mutation
selected the first eligible index pointer. Random measurement values are
not randomized corruption. A separate matrix test now selects each of four
eligible interior pointer positions in a synthetic file, one damaged copy per
position, and checks the public recovery command against independently held
random values. The fixture damage tool accepts `--child-index` for that
verified selection. This varies *where* the one supported break occurs, not
the type of damage or the scientific layout. It does not establish success on
unrelated damage or layouts, and missing physical payload bytes cannot be
reconstructed from the damaged file alone. A structurally mapped unfiltered
chunk also has no independent checksum, so its historical value may still be
wrong; read the report's integrity note and chunk status.

| Requirement | Current behavior |
| --- | --- |
| Dataset | One explicitly selected local, fixed-size rank-two canonical little-endian `uint32` dataset **or** rank-one canonical little-endian IEEE `float64` dataset; dimensions divisible by chunks; other local datasets may coexist |
| Storage | Fully aligned chunks; rank two unfiltered, rank one exactly Fletcher32 followed by DEFLATE; no virtual or external storage |
| Index | Superblock v0/v1, v1 object header (inline or one bounded continuation), v3 chunked layout, type-1 version-1 B-tree with a level-one root |
| Damage | Zero or one undefined root-to-interior-leaf pointer; a detached leaf is accepted only with two reachable reciprocal siblings and matching parent key bounds |
| Limits | Source at most 128 MiB, dataset at most 1,048,576 elements, at most 4,096 chunks and 4,096 traversed nodes, chunk at most 1 MiB |

An index that is healthy also works as an inspection and export control case. Other metadata versions, filter orders, datatypes, partial edge chunks, a destroyed root, additional broken links, and arbitrary stale/deallocated structures are outside this release. Files containing multiple datasets are allowed when a supported dataset is explicitly selected and its own object header anchors its chunk index. Detached structures cannot be attributed across datasets by shape or plausible bytes alone. Unsupported or contradictory structures produce an error or an explicit partial result; a plausible `TREE` signature or payload shape alone never assigns ownership.

The damaged file must still permit HDF5 to resolve the selected dataset's metadata by path. H5Reclaim uses h5py for that name and metadata lookup, then reads the chunk index and payload bytes with its own bounded read-only parser. The output and report paths must not exist and must not alias the input or each other.

## Reading an output

The recovered dataset keeps the selected path. `/_h5reclaim/chunk_status` is a two-dimensional map indexed by chunk row and column. The map is embedded in the output file, not only in the JSON report.

| Code | Status | Meaning |
| --- | --- | --- |
| 1 | `recovered` | Bytes exported at a coordinate accepted under the stated structural rules |
| 2 | `allocation_unknown` | No supported mapping; original allocation or fill semantics are unknown |
| 3 | `ambiguous` | Reserved for conflicting candidate evidence |
| 4 | `unavailable` | Reserved for unavailable bytes under a supported procedure |
| 5 | `unsupported` | Reserved for future mixed-layout outputs; this release generally rejects the file |
| 6 | `decode_failed` | An indexed chunk failed supported decompression or its Fletcher32 check; output has no trusted value there |

This release can produce codes 1, 2, and 6 in completed outputs. A chunk without code 1 reads as zero from the new HDF5 dataset because zero is its storage fill value. **That zero is not a recovered measurement.** Check the status map before using values. Dataset attributes point to the map and state whether every chunk was recovered.

The output preserves recovered values, shape, chunking, and canonical datatype for the selected dataset. On the rank-one path it also copies bounded primitive scalar dataset attributes and reports which were copied or omitted; the output is uncompressed. It does not reproduce arbitrary attributes, dimension scales, links, other objects, or the original file's full scientific context. The JSON report and output warning record this limit. Check the original metadata separately before interpreting measurements.

The JSON report includes source hashes, selected object and index addresses, counts, an `execution_state`, unresolved links, and one mapping per accepted chunk with its leaf, payload address, route (`intact_tree` or `reconstructed_link`), and evidence. An identical copy is embedded at `/_h5reclaim/report_json`, so a finalized output retains its provenance if the companion JSON file is separated. `complete` means every chunk has code 1. A finished partial attempt has `execution_state: "finished"` and `complete: false`.

## Project layout and evidence

- `src/h5reclaim/format.py`: bounded superblock, object-header, layout, and v1 B-tree parsing.
- `src/h5reclaim/metadata.py`: selected dataset support checks.
- `src/h5reclaim/survey.py`: bounded local dataset inventory and preflight support reasons.
- `src/h5reclaim/recovery.py`: anchored reconciliation, extraction, output, and report.
- `tools/`: healthy fixture and controlled corruption. These are not imported by recovery.
- `corpus/`: four untouched scientific originals, source records, licenses, and pinned checksums.
- `benchmarks/`: corpus survey, real-file controlled-damage trial, and synthetic exact-placement evaluator.
- `tests/`: parser boundaries, fixture checks, safety cases, and end-to-end recovery.
- `docs/`: [technical brief](project-brief.md), [format experiment](structure-plan.md), [current status](status.md), and decision records.
- [File-by-file guide](file-guide.md): a beginner-friendly map of the repository and its data flow.

The synthetic controlled experiment recovered 1,024 of 1,024 chunks at exact coordinates, including 57 whose reads failed with h5py on the damaged copy. The separate GWOSC trial measured the same failure mode against untouched research bytes with controlled damage. Neither trial proves general recovery reliability, superiority to other tools, or success on a naturally damaged user's file. No external tool comparison or independent user case has been completed.

H5Reclaim is licensed under [Apache License 2.0](../LICENSE). Contributions should include a reproducible input or fixture, the expected behavior, and a check that distinguishes correct placement from merely opening the output.
