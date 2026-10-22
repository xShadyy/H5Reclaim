# Changelog

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
