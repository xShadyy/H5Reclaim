# Real-incident intake and independent scoring

This protocol is for **actual damaged files submitted with permission**. The
repository does not contain a naturally damaged scientific incident or a
field success-rate measurement. The tests for this runner create controlled
faults solely to check the evaluator. They are never entered as natural cases.

The run and score steps are deliberately separate. The run manifest contains
only damaged inputs and operational provenance. Recovery runs in a fresh
subprocess on a copy, without a healthy file, earlier hash, or truth manifest.
After it has finished, an independent evaluator can introduce a separately
retained truth manifest and compare accepted values. The subprocess shares OS
filesystem permissions with the evaluator, so this is an **input boundary**, not
a defense against a malicious process. To keep truth genuinely inaccessible,
run recovery on a machine or account that has no access to the reference, then
transfer the hash-pinned output and receipt to an evaluator.

## Run manifest

Save as `intake.json` next to `damaged/case1.h5`. The digest and byte size must
be filled in from the actual immutable source. Document the source, incident,
selected dataset and authorization. One incident may contribute several cases;
they share `incident_id` so they are not mistaken for independent events.

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
`metadata`, `index`, `payload`, `missing_dependency`, or `other`. The runner
does not assume that an operator's diagnosis is correct. The consent fields
record the custodian's declaration; the tool cannot verify their authority.
Do not submit patient, confidential or restricted measurements without the
appropriate authorization. The report omits the descriptive provenance and
custodian name, but the private work directory contains a **full copy** of each
damaged file and its output. Do not publish that directory by default.

Run from the repository root:

```sh
python benchmarks/run_incident_intake.py run --manifest /path/intake.json --work-dir /private/new-work-dir
```

This prints `run.json` and its SHA-256. Store that digest independently. The
manifest is restricted to a safe relative path under its own directory, a
bounded regular input of at most 4 GiB per case, explicit consent, and a
size/SHA-256 pin. The work directory must be new or empty. The runner checks
source and copy hashes after the subprocess. Each source is passed to the
public `h5reclaim rescue` CLI on a disposable copy, without synthetic edits.
Up to 128 declared cases are accepted. Cases can end in output, safe refusal,
timeout, or protocol failure. A refusal is counted in the run denominator.

The report has `case_count`, `distinct_incident_ids`, `run_denominator_cases`,
and all outcomes. It does **not** label any accepted value historically exact.
A selected cohort is not a random draw from all laboratory failures.

## Score after recovery

For an incident with an independently retained, matching healthy source, put
the reference in a separate private directory after the run. Its manifest
uses the same case ID, pins the reference bytes, and declares why it represents
the exact pre-incident values. Different acquisitions or nearby timestamps are
not automatically valid historical truth.

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

Keep the generated manifest and displayed SHA-256 independently. A trusted
scientist can also provide a partial set of previously retained hashes in the
same schema. A hash recorded after an incident cannot establish earlier
values. The evaluator
supports at most 100,000 prior hashes in one truth case, 1,048,576 logical
elements, four axes, and 16 MiB of decoded fixed-size elements. Variable-length
objects, unknown prior shape or HDF5 type, and cases exceeding these scoring
bounds remain unscorable or invalid. Capture refuses virtual data, external
raw storage, and external links, where local bytes alone would give incomplete
truth. A hash verifies a matching value but
does not reconstruct missing bytes.

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
an invalid case. A refusal does not count as successful repair.

## Public natural-damage candidate, not a scored case

The [HDF Group issue #5417](https://github.com/HDFGroup/hdf5/issues/5417)
describes a 620 MB HDF5 file reportedly damaged by a computer crash during
writing and links the broken file. The issue does not supply an independently
verified pre-incident reference or element hashes, nor an explicit data reuse
license. We have not bundled, downloaded, or scored it. Its error report is a
lead for permission and provenance inquiry, **not** evidence that this project
has been validated against that incident. Other publicly discussed HDF5
open failures can be reader-version incompatibilities rather than damage.

To make a field claim, obtain a prospectively declared set of distinct
incidents from independent laboratories, explicit data permission, pinned
files and environment details, preexisting exact truth where possible,
blind recovery runs, and a published denominator that includes unsupported
and unscorable cases. Until then, report conditional counts only.
