# Guide to the project files

H5Reclaim is a small research prototype for one kind of damaged HDF5 file. An HDF5 dataset can be split into chunks, much like a large image is split into tiles. An index tells a reader where each chunk lives inside the file. This prototype handles a particular case where one pointer from that index is lost, but neighboring index nodes still provide enough evidence to locate the missing branch.

This is a data recovery project, not a robotics simulator. An HDF5 file may contain data from a robot, microscope, simulation, or another application. The program cares about the file's internal layout, not the instrument that produced it. See the [README](../README.md) for the exact supported layout.

## How the pieces fit

1. `tools/make_healthy_fixture.py` creates a known-good HDF5 dataset. `tools/make_broken_link_fixture.py` makes a damaged copy and records the deliberate change separately.
2. `h5reclaim inspect` checks the damaged file's selected dataset and index. `h5reclaim recover` uses surviving structural links to copy justified chunks to a new HDF5 file.
3. The new file contains a chunk status map. A separate JSON report and an embedded copy of that report explain which chunks were copied, where they came from, and which regions remain unknown.
4. `benchmarks/run_recovery.py` compares that result with the pristine dataset. This comparison is evaluation work; it is not part of the recovery algorithm.

## Project setup and entry points

| File | What it does |
| --- | --- |
| `.gitignore` | Keeps virtual environments, caches, build products, and generated experiment files out of Git. |
| `AGENTS.md` | Instructions for people and coding assistants changing the project, including evidence and testing rules. |
| `LICENSE` | The full legal text governing reuse of this repository. |
| `README.md` | Main starting point: installation, commands, supported inputs, output meanings, and measured results. |
| `pyproject.toml` | Python package details, dependencies, build settings, and the `h5reclaim` terminal command. |

## Recovery program

| File | What it does |
| --- | --- |
| `src/h5reclaim/__init__.py` | Marks the directory as the Python package and describes its scope. |
| `src/h5reclaim/__main__.py` | Reads `inspect` and `recover` command-line arguments, prints results, and displays supported errors. |
| `src/h5reclaim/format.py` | Reads bounded byte ranges from the source and interprets the supported HDF5 superblock, dataset layout, and version-1 B-tree records. It checks the two neighboring leaves and parent key boundaries before proposing a missing child. |
| `src/h5reclaim/metadata.py` | Uses h5py to find the selected dataset and checks its shape, datatype, chunks, filters, and other support conditions. It does not read the dataset's chunk values. |
| `src/h5reclaim/recovery.py` | Combines the metadata and parsed index, checks chunk coordinates and byte ranges, reads accepted payloads, and writes the new dataset, status map, and provenance report. It checks that the source has not changed and rejects unsafe destination paths. |

The recovery program does not import the fixture generator or the benchmark. It receives the damaged file and the dataset path supplied by the user. If necessary metadata cannot be read, or if the index differs from the supported case, it stops or marks an unresolved region instead of inventing values.

## Controlled experiment and evaluation

| File | What it does |
| --- | --- |
| `tools/make_healthy_fixture.py` | Generates a fully written, uncompressed, two-dimensional `uint32` test dataset and reopens it to check every expected value. |
| `tools/make_broken_link_fixture.py` | Verifies the healthy index, changes one root child pointer in a copy, records that mutation in a separate manifest, and checks which ordinary HDF5 reads were affected. |
| `benchmarks/README.md` | Explains the benchmark command, its output directories, scoring, and pass/fail conditions. |
| `benchmarks/run_recovery.py` | Builds a trial with random reference values, runs recovery in a separate process, and scores exact chunk values and coordinates against the pristine dataset. It also checks native-reader failures and file hashes. |

The benchmark keeps `truth/pristine.h5`, `truth/challenge.json`, and `truth/mutation.json` for evaluation. The recovery subprocess is called with `inputs/damaged.h5`, the dataset path, and output paths. It is not passed the pristine file, random seed, or mutation manifest. These files live under the same trial directory, so this is separation of program inputs and responsibilities, not a security boundary against a program intentionally searching the filesystem.

## Tests

| File | What it checks |
| --- | --- |
| `tests/test_fixture.py` | The healthy fixture command creates allocated chunks with the expected values and metadata. |
| `tests/test_damage.py` | The damage tool changes one verified pointer, demonstrates a standard-reader failure, and rejects unsuitable fixture structures. |
| `tests/test_format.py` | Small constructed HDF5 byte examples test parser bounds, supported layouts, B-tree traversal, and the two-sided link rule. |
| `tests/test_support.py` | Unsupported datasets, including filters, partial edge chunks, wrong datatypes, multiple datasets, and a newer layout, are refused. |
| `tests/test_recovery_safety.py` | Destination aliases, contradictory or out-of-bounds metadata, missing sibling evidence, and failures while publishing output do not produce falsely trusted results. |
| `tests/test_integrity_limits.py` | Deliberately changed payload bytes can still be copied under a structurally valid mapping, so the report must not claim a historical integrity check. |
| `tests/test_end_to_end.py` | Runs the recovery path on damaged input and checks exact placement, output status, source preservation, and benchmark error categories. |

## Documentation and decisions

| File | What it records |
| --- | --- |
| `docs/project-brief.md` | Public technical goal, first supported case, and unproven claims. |
| `docs/status.md` | What was actually implemented and tested, with environment details, measured results, limits, and next work. |
| `docs/structure-plan.md` | How to verify the generated HDF5 index and derive the location of the pointer changed in the experiment. |
| `docs/existing-work.md` | Other relevant HDF5 tools and which comparisons have yet to be performed. |
| `docs/file-guide.md` | This map of the repository and the distinction between recovery and evaluation. |
| `docs/decisions/0001-first-fixture.md` | Why the first fixture has one simple, fully written dataset and why its actual index must be verified. |
| `docs/decisions/0002-attribution-and-output.md` | Why a detached leaf needs two-sided evidence, and how output validity and publication are handled. |
| `docs/decisions/0003-license.md` | The reasoning behind the repository's current license choice. |

The project handoff supplied privately to start the work is not part of this public repository. It described a proposed project before implementation; `docs/status.md` records the current state.
