# Guide to the project files

H5Reclaim is an experimental tool with bounded structural recovery and a separate route for copying current native-readable values. An HDF5 dataset can be split into chunks, much like a large image into tiles. An index tells a reader where each chunk lives. The older tree route can handle one missing leaf pointer with reciprocal neighboring evidence, including below a deeper root. Intact modern single-chunk and implicit layouts have a separate direct-address export route.

This is a data recovery project, not a robotics simulator. An HDF5 file may contain data from a robot, microscope, simulation, or another application. The program cares about the file's internal layout, not the instrument that produced it. See the [usage guide](usage.md) for the current supported layouts.

## How the pieces fit

1. `corpus/files/` provides four unchanged scientific HDF5 files. `benchmarks/run_real_corpus.py` verifies their hashes; `run_gwosc_recovery.py`, `run_damage_catalog.py`, and `run_seeded_matrix.py` create disposable controlled damage and independently score values and refusals. These do not establish a real-world success rate.
2. `h5reclaim diagnose` triages some unreadable files and declared dependencies. `survey` inventories local metadata, `inspect` checks one supported index, and `recover` exports evidence-backed chunks. `export-readable` copies bounded currently native-readable local data with sparse unknowns marked explicitly. `probe-status` tests an eligible status flag only on a disposable copy; `export-fragments` publishes unresolved raw bytes without coordinates. Hints cannot supply missing structural evidence.
3. The new file contains a chunk status map. A separate JSON report and an embedded copy of that report explain which chunks were copied, where they came from, and which regions remain unknown.
4. The two recovery benchmarks compare output with a pristine reference at exact chunk coordinates. This comparison is evaluation work; it is not part of the recovery algorithm.

## Project setup and entry points

| File | What it does |
| --- | --- |
| `.gitignore` | Keeps virtual environments, caches, build products, and generated experiment files out of Git. |
| `AGENTS.md` | Instructions for people and coding assistants changing the project, including evidence and testing rules. |
| `LICENSE` | Apache 2.0 terms for project code; bundled scientific data have separate CC BY 4.0 provenance in the corpus manifest. |
| `README.md` | Main starting point: project goal, principles, current stage, and installation. It links to detailed usage and evaluation guides. |
| `pyproject.toml` | Python package details, dependencies, build settings, and the `h5reclaim` terminal command. |
| `assets/logo-*.svg` | Three matching README logo options: Bridge, Trace, and Monogram. The README currently shows Monogram. |
| `assets/icon-*.svg` | Matching square marks for avatars and compact placements. |
| `assets/README.md` | Explains the logo options, sizes, palette, and how to switch the README image. |

## Recovery program

| File | What it does |
| --- | --- |
| `src/h5reclaim/__init__.py` | Marks the directory as the Python package and describes its scope. |
| `src/h5reclaim/__main__.py` | Parses the seven public commands and prints readable or detailed results. |
| `src/h5reclaim/format.py` | Reads bounded byte ranges from the analysis snapshot and interprets the supported HDF5 superblock, inline or one continued v1 object header, dataset layout, and rank-one or rank-two version-1 B-tree records. It checks neighboring leaves and parent key boundaries before proposing a missing child and records parsed metadata extents. |
| `src/h5reclaim/modern_indexes.py`, `modern_recovery.py`, `modern_evidence_adapter.py` | Validate newer checksummed superblocks/object headers and supported chunk layouts; attribute intact single/implicit chunks to selected metadata and reconcile their ranges. |
| `src/h5reclaim/evidence.py`, `evidence_adapter.py` | Record anchors, pointer paths, physical ranges, checksums, contradictions, accepted/unknown decisions, and bounded raw-fragment export. The adapter independently checks version-1 parser records. |
| `src/h5reclaim/snapshot_io.py` | Streams a quota-bound source snapshot in small blocks after a full logical disk-space preflight. |
| `src/h5reclaim/dependency_routes.py` | Observes raw superblock status, inventories external/VDS declarations, validates explicit related-file manifests, and confines optional h5clear to a disposable status-only trial. |
| `src/h5reclaim/metadata.py` | Uses h5py to find the selected dataset and checks its shape, canonical datatype bit representation, chunks, exact filter pipeline, and other support conditions. It selects bounded primitive scalar attributes for the rank-one path without reading dataset values. |
| `src/h5reclaim/survey.py` | Inventories bounded local dataset metadata and index nodes without reading values. It reports candidate, unsupported, or indeterminate reasons and skips soft and external links. |
| `src/h5reclaim/diagnose.py` | Checks a bounded snapshot's format signature and, when possible, local metadata; records condition, supported candidate, suggested action, and questions without reading values. |
| `src/h5reclaim/hints.py` | Parses size-limited scientist assertions about one dataset and compares them with independently observed metadata and the damaged input hash. Conflicts stop export; unobserved assertions remain unverified. |
| `src/h5reclaim/recovery.py` | Analyzes a private snapshot, combines selected metadata and parsed index, rejects overlapping payload and metadata ranges, decodes bounded DEFLATE and verifies Fletcher32 where applicable, and writes a new dataset, status map, and provenance report. It rechecks source identity and hash before publication and rejects unsafe destination paths. |
| `src/h5reclaim/readable_export.py` | Copies bounded native-readable fixed-size schemas with selected storage rules into a separate file, verifies current bytes, and marks sparse regions unknown. It does not repair structural damage or verify historical measurements. |

The recovery program does not import the fixture generator or the benchmark. It receives the damaged file and the dataset path supplied by the user. Other local datasets may coexist, but one is selected per invocation using its own object header as the index anchor. If necessary metadata cannot be read, or if the index differs from the supported case, it stops or marks an unresolved region instead of inventing values. It copies only bounded primitive scalar attributes for rank-one data, listing copied and omitted names. It omits links, dimension scales, sibling objects, and the larger scientific context, so users must consult source metadata separately when it remains readable.

## Controlled experiment and evaluation

| File | What it does |
| --- | --- |
| `tools/make_healthy_fixture.py` | Generates a fully written, uncompressed, two-dimensional `uint32` test dataset and reopens it to check every expected value. |
| `tools/make_broken_link_fixture.py` | Verifies the healthy index, changes one root child pointer in a copy, records that mutation in a separate manifest, and checks which ordinary HDF5 reads were affected. |
| `benchmarks/README.md` | Explains the benchmark command, its output directories, scoring, and pass/fail conditions. |
| `corpus/manifest.json` and `corpus/README.md` | Pin original scientific files with checksums, URLs, licenses, attribution, representative layouts, and expected support classifications. |
| `corpus/files/*.h5` and `corpus/files/*.hdf5` | Original, unmodified research files bundled for offline, real-structure checks. |
| `benchmarks/run_real_corpus.py` | Verifies original file hashes and surveys 251 real datasets without reading their measurements. |
| `benchmarks/run_gwosc_recovery.py` | Cross-checks the 16 kHz GWOSC file's real B-tree against h5py, makes a controlled damaged copy, measures native-read failure, and scores bit-exact recovery in a separate subprocess. |
| `benchmarks/run_damage_catalog.py` | Applies ten controlled damage and refusal cases to verified authentic-file copies and checks exact values, status labels, safe refusals, and source hashes. |
| `benchmarks/run_seeded_matrix.py` | Makes seeded varied controlled mutations of authentic-file copies and separately scores exact accepted chunks, unknowns, wrong acceptance, and safe refusals. |
| `benchmarks/run_recovery.py` | Builds a trial with random reference values, runs recovery in a separate process, and scores exact chunk values and coordinates against the pristine dataset. It also checks native-reader failures and file hashes. |

The benchmark keeps `truth/pristine.h5`, `truth/challenge.json`, and `truth/mutation.json` for evaluation. The recovery subprocess is called with `inputs/damaged.h5`, the dataset path, and output paths. It is not passed the pristine file, random seed, or mutation manifest. These files live under the same trial directory, so this is separation of program inputs and responsibilities, not a security boundary against a program intentionally searching the filesystem.

## Tests

| File | What it checks |
| --- | --- |
| `tests/test_fixture.py` | The healthy fixture command creates allocated chunks with the expected values and metadata. |
| `tests/test_damage.py` | The damage tool changes one verified pointer, demonstrates a standard-reader failure, and rejects unsuitable fixture structures. |
| `tests/test_format.py` | Small constructed HDF5 byte examples test parser bounds, supported layouts, B-tree traversal, and the two-sided link rule. |
| `tests/test_gwosc_format.py` | Checks the original 16 kHz file's continued object header, rank-one index, anchored missing child, and parser bounds. |
| `tests/test_gwosc_recovery.py` | Checks rank-one filtered decoding, checksum failure behavior, and selected attribute handling. |
| `tests/test_support.py` | Filters, partial edge chunks, wrong datatypes, and a newer layout are refused; a selected nested dataset recovers correctly with an identically shaped local distractor present. |
| `tests/test_datatype.py` | Canonical little-endian `uint32` is accepted while reduced precision, shifted bits, and nonstandard padding are refused. |
| `tests/test_survey.py` | Survey candidates, independent local datasets, skipped links, support reasons, traversal limits, and CLI text and JSON output. |
| `tests/test_diagnose.py` | Read-only triage with openable, unsupported, signature-damaged, and metadata-damaged inputs. |
| `tests/test_hints.py` | Strict JSON parsing and comparison of operator assertions with file evidence, including conflicts and unobserved fields. |
| `tests/test_readable_export.py` | Native copy, bitwise verification, allocated-storage checks, unsupported layouts/types, path safety, and source preservation. |
| `tests/test_triage_cli.py` | Public diagnosis, hint-conflict refusal, and native-readable export commands. |
| `tests/test_damage_catalog.py` | Runs the ten-case real-data catalog and checks the readable and JSON command results. |
| `tests/test_level_zero.py` | Verifies authentic 4 kHz direct-index export against exact independent values and refusal of a missing payload pointer. |
| `tests/test_corpus_cli.py` | Corpus survey command's readable summary and opt-in JSON output. |
| `tests/test_mutation_positions.py` | Exercises every eligible broken child position in one synthetic index, using damaged-only recovery subprocesses and exact value comparisons against independent test truth. |
| `tests/test_recovery_safety.py` | Destination aliases, contradictory or out-of-bounds metadata, missing sibling evidence, and failures while publishing output do not produce falsely trusted results. |
| `tests/test_integrity_limits.py` | Deliberately changed payload bytes can still be copied under a structurally valid mapping, so the report must not claim a historical integrity check. |
| `tests/test_end_to_end.py` | Runs the recovery path on damaged input and checks exact placement, output status, source preservation, and benchmark error categories. |

## Documentation and decisions

| File | What it records |
| --- | --- |
| `docs/project-brief.md` | Public technical goal, first supported case, and unproven claims. |
| `docs/status.md` | What was actually implemented and tested, with environment details, measured results, limits, and next work. |
| `docs/usage.md` | Current command examples, supported structures, output status, and cautions moved from the former long root README. |
| `docs/generalization-plan.md` | Format and fault-family coverage plan, evidence requirements, hard limits, and conditions for a statistically defensible coverage claim. |
| `docs/evidence-model.md`, `docs/dependency-bundles.md` | Explain provenance and conservative reconciliation, plus the explicit manifest contract and external/VDS limits. |
| `docs/structure-plan.md` | How to verify the generated HDF5 index and derive the location of the pointer changed in the experiment. |
| `docs/existing-work.md` | Other relevant HDF5 tools and which comparisons have yet to be performed. |
| `docs/file-guide.md` | This map of the repository and the distinction between recovery and evaluation. |
| `docs/decisions/0001-first-fixture.md` | Why the first fixture has one simple, fully written dataset and why its actual index must be verified. |
| `docs/decisions/0002-attribution-and-output.md` | Why a detached leaf needs two-sided evidence, and how output validity and publication are handled. |
| `docs/decisions/0003-license.md` | The reasoning behind the repository's current license choice. |

The project handoff supplied privately to start the work is not part of this public repository. It described a proposed project before implementation; `docs/status.md` records the current state.
