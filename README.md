# H5Reclaim

H5Reclaim is an experimental command-line tool for one structural HDF5 recovery case: a child pointer in a version-1 raw-data chunk B-tree is lost, while the dataset description, neighboring leaves, and their reciprocal sibling links survive. It exports chunks whose coordinates are supported by that evidence into a **new** HDF5 file. The source is opened for reading and its identity and SHA-256 are checked during recovery. A bounded private snapshot lets h5py and the raw parser inspect the same bytes; it temporarily needs disk space up to the source's size, capped at 128 MiB.

This is a narrow implementation, not a general HDF5 repair utility. Structural evidence identifies where bytes belong; unfiltered chunk payloads have no independent checksum here, so the tool cannot authenticate historical measurement values.

## Install

Python 3.10 or newer is required. In a terminal at the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
h5reclaim --help
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. The first experiment ran on Python 3.12.14, h5py 3.12.1, HDF5 1.14.4, and NumPy 2.3.5. The current test suite also passed on Python 3.12.14, h5py 3.16.0, HDF5 2.0.0, and NumPy 2.5.3. HDF5 library behavior and fixture bytes may differ on other versions; verify the actual layout and run the tests.

## Survey a file before recovery

```sh
h5reclaim survey path/to/input.h5
```

The JSON inventory lists each local dataset's selectable path, shape, datatype, storage layout, chunk dimensions, filters, and support reasons. It reads metadata and index nodes, never dataset values or chunk payloads. It does not resolve soft or external HDF5 links. `candidate` means a recovery attempt fits the observed metadata, not that its payload is intact or that the attempt will succeed. Select a candidate with `--dataset /its/path` for `inspect` or `recover`; no automatic structure conversion is performed. Exit code 0 means the inventory completed, 1 means traversal was partial due to a declared limit or unreadable link, and 2 means the source could not be surveyed. The output remains JSON in each case.

## Reproduce the experiment

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

The directory must be new or empty. See [benchmark details](benchmarks/README.md). Tests run with `python -m unittest discover -s tests -v` after installation.

## Supported input

| Requirement | Current behavior |
| --- | --- |
| Dataset | One explicitly selected local, fixed-size rank-two dataset; other local datasets may coexist; dimensions divisible by chunks; canonical little-endian `uint32` storage |
| Storage | Fully aligned, unfiltered chunks; no virtual or external storage |
| Index | Superblock v0/v1, inline v1 object header and v3 chunked layout, type-1 version-1 B-tree with a level-one root |
| Damage | Zero or one undefined root-to-interior-leaf pointer; a detached leaf is accepted only with two reachable reciprocal siblings and matching parent key bounds |
| Limits | Source at most 128 MiB, dataset at most 1,048,576 elements, at most 4,096 chunks and 4,096 traversed nodes, chunk at most 1 MiB |

An index that is healthy also works as an inspection and export control case. Other metadata versions, filters, datatypes, partial edge chunks, a destroyed root, additional broken links, and arbitrary stale/deallocated structures are outside this release. Files containing multiple datasets are allowed when a supported dataset is explicitly selected and its own object header anchors its chunk index. Detached structures cannot be attributed across datasets by shape or plausible bytes alone. Unsupported or contradictory structures produce an error or an explicit partial result; a plausible `TREE` signature or payload shape alone never assigns ownership.

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
| 6 | `decode_failed` | Reserved for future filtered layouts |

This release produces codes 1 and 2 in completed outputs. A chunk with code 2 reads as zero from the new HDF5 dataset because zero is its storage fill value. **That zero is not a recovered measurement.** Check the status map before using values. Dataset attributes point to the map and state whether every chunk was recovered.

The output preserves recovered values, shape, chunking, and canonical datatype for the selected dataset. It does not copy the source's attributes, dimension scales, links, other objects, or scientific context. The JSON report and output warning record this limit. Check the original metadata separately before interpreting measurements.

The JSON report includes source hashes, selected object and index addresses, counts, an `execution_state`, unresolved links, and one mapping per accepted chunk with its leaf, payload address, route (`intact_tree` or `reconstructed_link`), and evidence. An identical copy is embedded at `/_h5reclaim/report_json`, so a finalized output retains its provenance if the companion JSON file is separated. `complete` means every chunk has code 1. A finished partial attempt has `execution_state: "finished"` and `complete: false`.

## Project layout and evidence

- `src/h5reclaim/format.py`: bounded superblock, object-header, layout, and v1 B-tree parsing.
- `src/h5reclaim/metadata.py`: selected dataset support checks.
- `src/h5reclaim/survey.py`: bounded local dataset inventory and preflight support reasons.
- `src/h5reclaim/recovery.py`: anchored reconciliation, extraction, output, and report.
- `tools/`: healthy fixture and controlled corruption. These are not imported by recovery.
- `benchmarks/`: independent reference, native-reader baseline, and exact-placement evaluator.
- `tests/`: parser boundaries, fixture checks, safety cases, and end-to-end recovery.
- `docs/`: [technical brief](docs/project-brief.md), [format experiment](docs/structure-plan.md), [current status](docs/status.md), and decision records.
- [File-by-file guide](docs/file-guide.md): a beginner-friendly map of the repository and its data flow.

The controlled experiment recovered 1,024 of 1,024 chunks at exact coordinates, including 57 whose reads failed with h5py on the damaged copy. The benchmark is reproducible; it is **not** evidence of general recovery reliability, superiority to other tools, or usefulness on an independent user's file. No external tool comparison or real-world case has been completed.

H5Reclaim is released under [Apache License 2.0](LICENSE). Contributions should include a reproducible input or fixture, the expected behavior, and a check that distinguishes correct placement from merely opening the output.
