# Recovery evidence

Reports connect exported values to the source bytes, coordinates, decoding, and comparisons used to accept them. Source size and SHA-256 identify the bytes inspected. The input is verified again before publication.

## Placement and current values

Structural routes derive a dataset anchor from rooted metadata, interpret its schema and index, and validate payload extents and coordinates. Implemented link repairs record the sibling or checksum observations used to reconstruct an index link. The evidence ledger under `evidence_ledger` records anchors, links, payload hashes, checks, accepted assignments, unresolved proposals, and contradictions.

`evidence.py` reconciles the recorded observations. Its format-specific adapters reread accepted payloads and provide the relevant metadata ranges. Coordinate alignment, decoded size, link continuity, physical bounds, checksum results, and competing owners determine acceptance. Signature matches and operator hints can guide inspection but do not establish coordinates by themselves.

Native-readable routes enumerate allocation and check competing owners before reading values. Fixed-record streaming uses the declared HDF5 datatype to preserve raw records, including padding. Heap-backed strings, ragged arrays and compound fields are compared as logical records because source heap descriptors cannot be copied between files. References use source object identities and encoded region selections, then are remapped and checked after the recovered targets exist. Every accepted output is read back and compared with its source interpretation.

Detached discovery checks modern object headers independently of unreadable group links. Each candidate supplies its own type, extent and storage metadata, and observed candidate allocations must not overlap. The report explicitly leaves the complete namespace and unreadable owners unresolved. Original paths are never inferred from signatures or hints. A disposable empty-root view only makes native object-address access possible; it supplies no recovered values.

## Validity maps

Each route names its map and defines the status codes in its report. A chunk map describes a complete chunk; an element map can retain individual complete elements. Unallocated, missing, unreadable, or unresolved positions remain unknown even when HDF5 displays fill values.

Whole-file recovery retains each dataset's map and evidence under its own metadata group and embeds a consolidated report. Dataset failures and omitted groups, attributes, links, or scales are listed separately from accepted values.

## Historical equality

`current_value_evidence` describes what the present bytes support. `historical_integrity` and `historical_status` describe equality to a separately supplied prior capture. A current checksum or a successful readback does not establish what the acquisition contained before damage.

A prior baseline can verify a matching value. Replicas or parity can also supply missing information. A capsule supplies earlier schema, physical locations, and hashes. `--strict-history` requires the selected route to compare every accepted unit with a pinned prior capture before publishing.

Historical equality establishes equality to that capture. Capture provenance and scientific interpretation remain separate questions.

## Unresolved fragments

```sh
python -m h5reclaim export-fragments REPORT.json DAMAGED.h5 --output fragments.zip
```

Fragment export validates the supplied source identity and recorded fragment hashes, then creates a ZIP of unresolved raw `.bin` extents and a manifest. Proposed coordinates are marked unverified. Accepted chunks are excluded. The archive supports further investigation without assigning unsupported measurements to an HDF5 dataset.
