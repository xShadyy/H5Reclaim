# Recovery report format (candidate)

This guide describes JSON reports written by `rescue`, `recover`, and related
export routes in the first 1.0 release candidate. The format is
`schema_version: 1`, but route-specific sections differ. It is not a formal
JSON Schema or a promise that every optional field appears in every route.
`diagnose`, `survey`, and benchmark JSON are different documents.

## Identify the report and source

Published reports use these common fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Report format discriminator; the current value is `1`. |
| `tool` and `tool_version` | `h5reclaim` and the version that produced the report. |
| `outcome` | `complete` or `partial` for a published export. |
| `source.path`, `source.size_bytes` | Input name and byte count observed during recovery. |
| `source.sha256_before`, `source.sha256_after` | Hashes used to check that the inspected source bytes stayed stable. |
| `output_path` | Published HDF5 destination. |
| `metadata_group` | Actual HDF5 path used for embedded evidence and status maps, when present. |

The report is written beside the output unless `--report` selects another
path. An embedded `report_json` dataset is also written into the output's
metadata group. For a whole-file export, the outer report's `mode` is
`whole_file_recovery`, `datasets` contains entries with `path`, `aliases`,
and a nested `report`, and `failures` lists dataset paths that could not be
exported. Counts such as `datasets_discovered`, `datasets_exported`, and
`datasets_failed` refer to datasets, not elements. The outer `inventory` and
`scientific_context` sections record incomplete discovery and omitted
groups, attributes, links, or scales. A nested dataset can have its own
source identity when a companion file is involved.

A selected-dataset export places its `dataset` and status information at the
top level. `dataset.path` identifies the path in the output. `dataset.shape`
may be `null` for a null dataspace. A recovered dataset may have an
`accepted_elements` and `unknown_elements` pair, or a `counts` object whose
units are chunks. Read the route's map and counts rather than assuming all
counts measure the same thing. Fields such as `mappings`, `failed_chunks`,
`failed_elements`, `evidence_ledger`, `ownership_inventory`,
`historical_integrity`, and `scientific_context` add route-specific evidence.

`complete` means the chosen export route and required context had no
unresolved items under its rules. It does not prove that the source matched an
earlier acquisition or that a measurement is scientifically correct.
`partial` can still publish accepted values; inspect the unknown positions
and omitted context before analysis. With `rescue --fail-on-partial`, an
output that was published as partial returns exit code `1`. Normal
publication returns `0`; invalid arguments and recovery failures return `2`.

To check a saved output against its report after copying or storage, run
`h5reclaim verify-result OUTPUT.h5 REPORT.json`. Add `--source SOURCE.h5`
to hash an explicitly supplied source against the report. This checks the
current output's schema, source annotation, and validity maps in a bounded
worker. It does not read or hash recovered values, authenticate jointly
modified artifacts, or establish historical measurements. Its exit codes
are `0` for a passed check, `1` for a detected inconsistency, and `2` for an
unsupported check or other error. `h5reclaim report` only summarizes the
saved JSON; it does not perform this consistency check.

## Interpret status maps

Status maps are unsigned one-byte HDF5 datasets. Their actual paths are in
the report, typically in `validity.dataset`, `validity.chunk_status`,
`validity.element_status`, `element_status`, or `validity_map`. Names under
`/_h5reclaim` are common but can change to avoid collisions with source
objects, especially in whole-file exports. Do not construct a map path from
a dataset name.

The map's unit is given by `validity.granularity` or by its reported path
and shape. A chunk map has one code per chunk grid position. An element map
has the same shape as the exported dataset. Read the adjacent `codes`,
`element_codes`, or `validity_codes` definition. Some routes use names mapped
to integers, for example `{"recovered": 1, "allocation_unknown": 2}`;
others use stringified integer keys mapped to descriptions. In the current
value map, **code `1` means accepted from the inspected source or a pinned
reconstruction route; every other code is unknown for analysis**. The other
codes have route-specific meanings such as unallocated, ambiguous,
unavailable, unsupported, or failed decode. A displayed HDF5 fill value at
an unknown position is not an accepted measurement.

`historical_status`, when present, is a separate comparison map. Its code
`1` means equality to an operator-supplied prior capture for that unit; `0`
means no such match was established. Current-source acceptance alone does not
imply historical equality. The report identifies the prior artifact and
comparison route when used. See [recovery evidence](evidence-model.md) for
the distinction.

## Read through the public Python interface

`h5reclaim` exports `read_masked` and `MaskedReadError`. For a fixed-size
dataset, `read_masked(output_path, report_path, dataset_path, *, selection=None,
max_bytes=67108864, max_elements=1000000)` returns a NumPy masked array;
positions whose reported current-value status is not `1` are masked. It
accepts integers, forward unit-step slices, and one ellipsis, and applies
the selection before reading the data and map. The output and report must
agree on source identity, paths, shape, map type, and declared status codes;
the selected saved report must also match its embedded copy. Reported paths
must resolve through local hard links, not soft or external links.
Null and variable-length datasets do not have a bounded fixed-size read
through this helper.

The byte limit bounds the selected logical data, map, and declared chunk
sizes; native libraries may use additional memory. The helper validates
report and map consistency, but does not authenticate the file or prove a
historical value. It may raise `MaskedReadError` for inconsistent evidence,
or underlying path, HDF5, and selection exceptions. For data processing,
prefer this helper or inspect the exact reported map rather than relying on
HDF5 fill values.

The CLI and these two exported Python names are the intended integration
surface for this candidate. Modules under `h5reclaim` used by the CLI are
implementation details. Consumers should check `schema_version` and `tool`,
select the exact dataset record, read reported map paths and codes, and allow
unrecognized optional fields. The candidate may change before stable 1.0;
compatibility promises for later releases have not been finalized.
