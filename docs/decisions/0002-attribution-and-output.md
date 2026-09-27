# 0002: Two-sided attribution and embedded validity

This records the original design. Version 0.5.0 supersedes its 128 MiB
snapshot bound with a streamed, disk-preflighted 4 GiB default; see
[current status](../status.md) for active limits.

Date: 2026-09-27

The first recovery case requires the explicitly selected dataset's own layout message to anchor the v1 B-tree root. Other local datasets may coexist, including ones with the same shape. A lost interior root child is accepted only if both adjacent reachable leaves independently identify the same detached node, its sibling pointers reciprocate, its level is zero, and its first/final key boundaries match the missing parent slot. Every accepted chunk must pass coordinate alignment, bounds, size, filter-mask, uniqueness, and payload-range checks. Payload ranges must not overlap one another or parsed metadata extents. A file-wide `TREE` signature scan or compatible shape is insufficient evidence of ownership.

This rule deliberately leaves regions unresolved if one side of the bridge is missing or contradictory. The output stores a chunk-level status map inside HDF5 so an unknown region cannot be inferred from its fill value. The report gives the accepted evidence route and states that structural attribution does not prove historical integrity of unfiltered bytes.

The implementation handles one selected dataset per recovery invocation, one level-one root, and at most one broken link. It accepts only the canonical on-disk little-endian unsigned 32-bit integer representation because payload bytes are copied directly. Other datatype bit precisions, shifts, or padding could change the measurements during export. The first controlled fixture has one dataset; a separate test covers an identically shaped local distractor. These tests do not establish safe attribution of entirely detached structures without the selected object's anchors.

The input is read into a private, bounded snapshot so h5py metadata lookup and the raw parser inspect the same bytes. This temporarily requires disk space up to the input's size, capped at 128 MiB. Source identity and SHA-256 are checked before output publication; a changed source causes refusal. The output and JSON report are written in private temporary directories and published only after these checks succeed. These directories prevent another user in a shared destination directory from replacing the temporary HDF5 path while h5py opens it. A failed report publication removes the newly published output. The output embeds the full report at `/_h5reclaim/report_json`, so the finalized HDF5 file retains evidence even if the companion JSON is separated. Existing destinations and aliases are rejected. A finished partial output has `complete: false` and unknown chunks marked `allocation_unknown`. Original attributes, dimension scales, links, sibling objects, and scientific context are not copied; the output and report state this limitation.
