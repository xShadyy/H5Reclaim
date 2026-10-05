# Evaluate submitted datasets

Use this workflow to recover submitted damaged files and measure the result
against independently retained originals or earlier element hashes.

The **run** step calls H5Reclaim on a source copy and records its output, report,
and file hashes. The **score** step then compares accepted values with a
separately supplied reference. This keeps recovery and evaluation distinct
while preserving a reproducible record for each dataset.

## Run manifest

Save as `intake.json` next to `damaged/case1.h5`. The digest and byte size must
be filled in from the actual immutable source. Document the source, incident,
selected dataset and authorization. Group datasets from the same incident
under a shared `incident_id`.

```json
{
  "schema_version": 1,
  "cohort": "lab-submissions-2026",
  "cases": [
    {
      "id": "case1-signal",
      "incident_id": "instrument-outage-001",
      "path": "damaged/case1.h5",
      "size_bytes": 123456,
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "dataset": "/experiment/signal",
      "declared_cause": "interrupted_write",
      "provenance": "Instrument and acquisition record; interruption observed by operator; no modifications after capture",
      "consent": {
        "local_processing": true,
        "authorized_by": "laboratory data custodian",
        "recorded_on": "2026-09-27"
      }
    }
  ]
}
```

`declared_cause` is one of `unknown`, `interrupted_write`, `truncation`,
`metadata`, `index`, `payload`, `missing_dependency`, or `other`. It records
the reported cause; H5Reclaim determines recovery methods from the file.
Include the data custodian's local-processing approval in `consent`.
The report omits descriptive provenance and custodian names. The private work
directory retains source copies and recovered outputs for evaluation.

Run from the repository root:

```sh
python benchmarks/run_incident_intake.py run --manifest /path/intake.json --work-dir /private/new-work-dir
```

This prints `run.json` and its SHA-256. Store that digest independently. The
manifest accepts relative source paths under its directory, regular files of
at most 4 GiB per case, recorded consent, and matching size/SHA-256 values.
The work directory must be new or empty. The runner checks source and copy
hashes after each recovery process. Each source is passed to the public
`h5reclaim rescue` CLI on a disposable copy. Up to 128 declared cases are
accepted, with outputs, refusals, timeouts, and protocol failures recorded.

The report has `case_count`, `distinct_incident_ids`, `run_denominator_cases`,
and all outcomes. The score step adds exact-value comparisons.

## Score after recovery

For an incident with an independently retained, matching healthy source, put
the reference in a separate private directory after the run. Its manifest
uses the same case ID, pins the reference bytes, and declares why it represents
the exact acquisition being evaluated.

```json
{
  "schema_version": 1,
  "cases": [
    {
      "id": "case1-signal",
      "kind": "healthy_file",
      "path": "originals/case1-prior.h5",
      "size_bytes": 123456,
      "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "dataset": "/experiment/signal",
      "provenance": "Independent archival snapshot of this exact acquisition, retained before the outage"
    }
  ]
}
```

If only some per-element SHA-256 hashes were retained **before** an incident,
use a private `element_hashes` entry with `shape`, `hdf5_type_sha256` of the
encoded HDF5 datatype, and an `element_sha256` object mapping zero-based
C-order flat positions to hashes of each decoded fixed-size element. Example:

```json
{
  "id": "case1-signal",
  "kind": "element_hashes",
  "shape": [100],
  "hdf5_type_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "element_sha256": {
    "0": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  },
  "provenance": "Capture time, independent storage, and identity of the exact measurement"
}
```

The snippet above is one entry in the `cases` array, not a complete manifest.
The runner can generate a full private truth manifest for a currently healthy,
locally stored fixed-size dataset **before** an incident:

```sh
python benchmarks/run_incident_intake.py capture-hashes \
  --source /path/healthy.h5 --dataset /experiment/signal \
  --id case1-signal --provenance "Acquisition ID, time, operator and separate retention" \
  --output /private/truth.json
```

Keep the generated manifest and displayed SHA-256 independently. Previously
retained partial hashes can also use this schema.

This scorer supports local fixed-size datasets with a known prior shape and
HDF5 datatype. Its per-case bounds are 100,000 prior hashes, 1,048,576 logical
elements, four axes, and 16 MiB of decoded values. Hash capture uses local
storage; virtual datasets, external raw storage, and external links require a
separately assembled reference. Cases outside the scoring bounds retain their
unscorable or invalid classification in the report.

Pin `run.json` and `truth.json` SHA-256 **outside** either manifest, then run:

```sh
python benchmarks/run_incident_intake.py score \
  --run /private/new-work-dir/run.json --run-sha256 RUN_SHA256 \
  --truth /private/truth.json --truth-sha256 TRUTH_SHA256 \
  --output /private/incident-score.json
```

The evaluator validates the unchanged damaged copy, output, and report hashes;
checks the embedded report; compares exact HDF5 datatype and shape for a
healthy reference; and applies the output validity map. It reports matching
accepted elements, wrong accepted elements, verified unknown elements,
refused elements, accepted elements without truth, cases without any truth,
and invalid cases separately. A partially hashed dataset has a partial truth
denominator. The score command exits 1 when it finds wrong accepted values or
an invalid case. Refusals remain in the case counts with zero recovered values.

For generated layouts and controlled faults, use the
[release coverage benchmark](../docs/coverage.md). For another supplied panel,
see `run_heldout_trials.py --help`.
