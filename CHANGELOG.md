# Changelog

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
