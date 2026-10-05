# Repository guide

| Path | Role |
| --- | --- |
| `src/h5reclaim/__main__.py` | Public commands and argument validation |
| `src/h5reclaim/rescue.py` | Automatic route selection from file condition and selected schema |
| `src/h5reclaim/whole_file.py` | Dataset discovery, independent recovery, context restoration, consolidated report |
| `src/h5reclaim/route_worker.py`, `worker_limits.py`, `native_worker.py` | Isolated native work, resource control, staging, and publication |
| `src/h5reclaim/recovery.py`, `format.py` | Older chunk-tree parsing, recovery, snapshots, and output |
| `src/h5reclaim/metadata.py`, `metadata_fallback.py` | Rooted dataset paths, datatype, layout, and filter metadata |
| `src/h5reclaim/modern_recovery.py`, `modern_indexes.py` | Modern chunk indexes and recovered output |
| `src/h5reclaim/filtered_fixed_array.py`, `extensible_array.py`, `v2_btree_chunks.py`, `modern_link_repair.py` | Modern index-family parsing and implemented pointer repairs |
| `src/h5reclaim/schema_codec.py` | Numeric and fixed-record schema interpretation; DEFLATE, LZF, shuffle, Fletcher32 |
| `src/h5reclaim/nonchunked_recovery.py`, `chunk_truncation.py` | Compact/contiguous elements and physical tail truncation |
| `src/h5reclaim/readable_export.py`, `native_io.py`, `native_addresses.py` | Native-readable export, allocation checks, exact fixed-record I/O and runtime chunk-address normalization |
| `src/h5reclaim/variable_readable.py` | Element-wise variable strings and ragged primitive numeric export |
| `src/h5reclaim/native_stream.py`, `logical_types.py` | Partial streamed exports, unusual file numeric widths, bounded heap batches and reference tokens |
| `src/h5reclaim/filter_registry.py` | Explicit packaged codec registration and reversible output-filter policy |
| `src/h5reclaim/object_discovery.py`, `checked_view.py` | Legacy and modern detached headers, private root views and checksum-justified root correction |
| `src/h5reclaim/source_session.py`, `checkpoint.py`, `unit_checkpoint.py` | Shared images, locked dataset checkpoints and append-only verified selection caches |
| `src/h5reclaim/large_streaming.py`, `large_structural.py`, `sparse_io.py` | Sparse snapshots, POSIX/Windows allocation queries, configurable streaming budgets, larger exports |
| `src/h5reclaim/ownership_inventory.py`, `evidence.py`, `evidence_adapter.py`, `modern_evidence_adapter.py` | Physical ownership and evidence reconciliation |
| `src/h5reclaim/status_trial_export.py`, `metadata_trial_export.py`, `metadata_correction.py`, `header_dimension_trial.py` | Checked disposable status and metadata correction trials |
| `src/h5reclaim/dependency_routes.py`, `external_raw_export.py`, `external_link_export.py` | Pinned related files and external storage |
| `src/h5reclaim/vds_export.py`, `vds_nested.py`, `family_bundle.py`, `split_bundle.py` | Virtual mappings and driver address spaces |
| `src/h5reclaim/dependency_stream.py`, `related_recovery.py`, `related_manifest.py` | Large/growing dependency graphs, external group trees and automatic pinned manifests |
| `src/h5reclaim/bundle_stream.py`, `native_bindings.py`, `userblock.py` | General native Family/Split streaming, public HDF5 APIs and application header preservation |
| `src/h5reclaim/baseline.py`, `chunk_integrity.py`, `payload_integrity.py`, `replica_recovery.py` | Prior hashes, integrity comparison, and independent replicas |
| `src/h5reclaim/parity_sidecar.py`, `erasure_sidecar.py`, `gf256.py` | XOR and multiple-erasure reconstruction |
| `src/h5reclaim/recovery_capsule.py`, `protection_bundle.py` | Prospective physical maps and bundled protection |
| `src/h5reclaim/historical_integrity.py`, `output_annotations.py`, `scientific_context.py` | History maps, selected-dataset annotations, context inventory |
| `tests/` | Recovery regressions, misleading inputs, source preservation, CLI and publication checks |
| `tests/test_broader_recovery.py` | LZF, exact multidimensional records, variable values, whole-file context and aliases |
| `tests/test_universality.py` | Automatic decoder fallback, partial chunks, logical datatypes, references, optional codecs, discovery, budgets and resume checks |
| `tests/test_completion.py`, `tests/test_completion_streaming.py` | Namespace collisions, large/null attributes, committed identities, legacy roots, growing VDS, file-scoped references, heap batches and selection resumes |
| `tests/test_applications.py`, `benchmarks/run_application_corpus.py` | Independent MATLAB 7.3, netCDF4 and NWB writer/reader checks, intact and controlled damage |
| `tools/make_healthy_fixture.py`, `tools/make_broken_link_fixture.py` | Reproducible healthy and damaged development fixtures |
| `tools/check_installed_package.py` | Console-command recovery check against an installed wheel, outside the source checkout |
| `corpus/` | Unchanged attributed scientific files, hashes, and structural survey baseline |
| `benchmarks/` | Controlled recovery evaluations and independent scoring tools |
| `docs/usage.md` | Operator commands and output interpretation |
| `docs/dependency-bundles.md` | Related-file, Family, and Split manifests |
| `docs/evidence-model.md` | Current placement, values, history, and fragments |

Run commands from the repository root after installation. Generated benchmark work directories and results are disposable; the original corpus is hash-pinned. The Apache 2.0 license applies to source code; corpus attribution is recorded separately.

The wheel contains the runtime package and its license. The source distribution also includes the operator documentation and README banner. Scientific fixtures, tests, and benchmark tools remain in the Git repository, so ordinary installations do not download development data. CI builds the wheel from the source distribution, then checks the installed console command from a temporary directory with numeric data, UTF-8 strings, metadata, an evidence report, and source-preservation checks.
