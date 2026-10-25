# Changelog

## 1.3.1

- Remove HTML report generation from the CLI, batch command and Python API.
  JSON evidence reports, terminal summaries and result verification remain.
- Clarify that example input names must be replaced with existing files.

## 1.3.0

- Repair two damaged child pointers within one independently checked EAIB,
  EASB or BTIN metadata parent when one complete assignment restores the
  parent's original checksum and all child allocations check. Bridge separate
  nonadjacent legacy v1 B-tree gaps with reciprocal rooted sibling evidence.
- Relocate variable-length strings and numbers, nested pointer-bearing records,
  and supported object and region references after checked modern index
  restoration in a disposable source view. Whole-file rescue can remap
  references to independently exported sibling objects.
- Decode N-bit, scale-offset and available SZIP filters through a bounded
  native in-memory dataset. Scale-offset reproduces stored decoded values,
  which may be lossy relative to values before the original write.
- Inspect and repair rooted version-0 and version-1 file-space declarations,
  additional persistent paged section types, and multiple damaged fixed
  header fields where canonical replacements restore the original checksum.
- Reconstruct checked detached group aliases and cycles under synthetic paths;
  report unresolved edges while retaining independently checked siblings.
- Add a disposable `h5clear --image` probe that reports altered-copy metadata
  openability without exporting values or modifying the source.
- Score two additional same-parent cases in the ten-case compressed streaming
  panel against independent generated values.

## 1.2.0

- Recover multiple damaged modern index links in distinct metadata parents,
  each uniquely justified by restoring its own original checksum. Attribute
  repaired links on each recovered chunk's checked path and share one physical
  search budget across all link repairs.
- Raise the structural rank ceiling to the HDF5 format's rank 32 and support
  million-slot sparse fixed-array indexes with caller-selected chunk, grid,
  metadata and worker-memory quotas. Expand bounded fixed compound and array
  records while continuing to reject file-bound heap pointers and references.
- Inspect rooted persistent file-space manager headers and section lists.
  Plan checksum-constrained FSHD/FSSE repairs and optionally reseal them only
  when the surviving structure can be checked independently.
- Restore a missing compact group link to a checked detached dataset header
  when the original parent checksum uniquely proves its address.
- Capture independently checked full-file metadata before damage and restore
  the captured bytes to a new copy if all uncaptured bytes match; independently
  pin the archive digest and recheck the complete original file hash.
- Extend the independent compressed streaming panel with two-link EA and
  B-tree faults scored against separate NumPy truth.

## 1.1.0

- Stream compressed multidimensional fixed-record datasets through checked
  fixed-array, extensible-array and v2 B-tree link repairs. The large route
  supports up to 65,536 grid slots and explicitly bounded decoded chunks up
  to 64 MiB, with unknown and failed chunks retained in validity maps.
- Add independent metadata preflight, marker candidates and byte inspection.
- Add deterministic metadata repair plans, exact regenerated application to
  new copies, optional explicitly requested checksum resealing and audit logs.
- Verify saved accepted-chunk measurement digests and capture or verify
  fingerprints of complete output/report artifacts.
- Add searchable self-contained HTML reports, resumable file batches,
  installation diagnostics, a Python rescue entry point and a capability map.
- Add optional Pyfive cross-reader exports. Values without independent
  metadata coverage and native agreement stay unaccepted inspection candidates.
- Integrate official h5copy and h5repack tools through bounded disposable-copy
  workers when those executables are installed.
- Add a separately scored compressed streaming panel and retain existing
  report operation names for compatible one-dimensional fixed-array exports.

## 1.0.0

- Added `verify-result` to check the saved report against the current output
  structure and validity maps in a bounded worker. It does not read recovered
  values or establish historical authenticity.
- Added a [security reporting policy](SECURITY.md) for sensitive reports and
  guidance for processing untrusted HDF5 inputs.

The [controlled panel](benchmarks/results/v100rc1-release-coverage.json)
recorded 104 useful outputs from 109 damaged-file trials (77 fully exact,
27 partial, 5 refused), including all 15 declared chunk-dimension faults.
No wrong accepted element was found in this generated panel.

## 0.16.0

- Added `read_masked()` for bounded, fixed-size selections from recovered
  outputs using the corresponding reported validity map.
