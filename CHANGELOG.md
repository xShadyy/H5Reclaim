# Changelog

This file records user-visible changes. A release candidate is a preview for
testing, and its interfaces may change before the first stable release.

## Unreleased: 1.0.0rc1 candidate

- Extended automatic, checksum-constrained chunk-dimension correction to
  tested rank-five fixed-array datasets and a scale-offset dataset whose
  selected modern object header uses a checked continuation. The correction
  remains limited to a unique candidate with a valid original checksum and
  consistent rooted ownership, schema, index, and byte ranges.
- Added a bounded native export after a checked dimension correction for
  tested variable strings, ragged values, and compound records containing
  those heap-backed fields. The route requires agreement between the checked
  index and native chunk coordinates and ranges, then uses typed reads and
  output readback. It reports currently recoverable values; it does not
  establish equality to a separately retained earlier acquisition.
- Added a version-tagged release candidate gate: the existing CI checks must
  pass before source and wheel archives, with SHA-256 hashes, are made available
  as a workflow artifact. This does not publish to PyPI or create a GitHub
  Release.
- Documented recovery report fields, validity map interpretation, and the
  boundaries of the public Python interface in
  [the report format guide](docs/report-schema.md).
- Added `verify-result` to check the saved report against the current output
  structure and validity maps in a bounded worker. It does not read recovered
  values or establish historical authenticity.
- Hardened `read_masked()` against linked external data and report-only map
  changes by requiring local hard links and agreement with the embedded report.
  Native streaming exports now annotate their source digest consistently.
- Added a [security reporting policy](SECURITY.md) for sensitive reports and
  guidance for processing untrusted HDF5 inputs.

The [controlled candidate panel](benchmarks/results/v100rc1-release-coverage.json)
recorded 104 useful outputs from 109 damaged-file trials (77 fully exact,
27 partial, 5 refused), including all 15 declared chunk-dimension faults.
No wrong accepted element was found in this generated panel.

The candidate is still being evaluated. Its presence here does not state a
success rate for submitted scientific files or establish that any particular
damaged file can be recovered.

## 0.16.0

- Automatic whole-file rescue can try a uniquely checksum-justified modern
  chunk-dimension correction on a disposable view of the source.
- Retained complete rank-five fixed-array chunks before a physical tail cut,
  with unavailable positions reported as unknown.
- Added `read_masked()` for bounded, fixed-size selections from recovered
  outputs using the corresponding reported validity map.
- Recorded the controlled 109 damaged-file trial panel in
  [the coverage report](docs/coverage.md). Those observations describe the
  declared fixtures and fault classes, not an expected rate on other files.
